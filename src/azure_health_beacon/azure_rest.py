from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from .identity import (
    ARM_SCOPE,
    LOG_ANALYTICS_SCOPE,
    IdentityUnavailableError,
    get_access_token,
    home_tenant_id,
)

ARM_ENDPOINT = "https://management.azure.com"
LOG_ENDPOINT = "https://api.loganalytics.io"
_SUBSCRIPTION_TENANTS: dict[str, str] = {}
_RESOURCE_API_VERSIONS: dict[tuple[str, str, str], str] = {}
_CACHE_LOCK = threading.RLock()
_NOT_FOUND = object()


def _validate_endpoint(url: str, scope: str) -> None:
    parsed = urlsplit(url)
    expected_host = "api.loganalytics.io" if scope == LOG_ANALYTICS_SCOPE else "management.azure.com"
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise OSError("Refusing to send Azure authorization to an unexpected endpoint.")


def _option(arguments: list[str], name: str, default: str = "") -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return default


def _safe_error(response: requests.Response) -> str:
    message = f"Azure returned HTTP {response.status_code}."
    try:
        payload = response.json()
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            code = str(error.get("code", "")).strip()
            detail = str(error.get("message", "")).strip()
            if code or detail:
                message = f"{code}: {detail}".strip(": ")
    except (ValueError, TypeError, AttributeError):
        pass
    message = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", message)
    message = re.sub(
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+",
        "[REDACTED]",
        message,
    )
    return " ".join(message.split())[:500]


def _request_json(
    method: str,
    url: str,
    tenant_id: str,
    timeout: int,
    *,
    scope: str = ARM_SCOPE,
    params: dict[str, str] | None = None,
    body: object | None = None,
) -> object:
    _validate_endpoint(url, scope)
    token = get_access_token(tenant_id, scope)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=body,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.Timeout as error:
        raise subprocess.TimeoutExpired("Azure REST request", timeout) from error
    except requests.RequestException as error:
        raise OSError("Azure could not be reached securely.") from error
    finally:
        token = ""
        headers.clear()
    if not response.ok or 300 <= response.status_code < 400:
        raise OSError(_safe_error(response))
    if not response.content:
        return {}
    try:
        return response.json()
    except requests.JSONDecodeError as error:
        raise OSError("Azure returned an unreadable response.") from error


def _paged_values(
    url: str,
    tenant_id: str,
    timeout: int,
    *,
    params: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    next_url = url
    next_params = params
    while next_url:
        payload = _request_json(
            "GET", next_url, tenant_id, timeout, params=next_params
        )
        next_params = None
        if not isinstance(payload, dict):
            raise OSError("Azure returned an unreadable paged response.")
        rows = payload.get("value", [])
        if not isinstance(rows, list):
            raise OSError("Azure returned an unreadable paged response.")
        values.extend(item for item in rows if isinstance(item, dict))
        next_link = payload.get("nextLink")
        next_url = str(next_link) if next_link else ""
    return values


def _subscriptions(timeout: int) -> list[dict[str, object]]:
    home = home_tenant_id()
    tenant_ids = [home]
    try:
        tenant_rows = _paged_values(
            f"{ARM_ENDPOINT}/tenants",
            home,
            timeout,
            params={"api-version": "2020-01-01"},
        )
        tenant_ids.extend(
            str(row.get("tenantId", "")) for row in tenant_rows if row.get("tenantId")
        )
    except (IdentityUnavailableError, OSError, subprocess.TimeoutExpired):
        pass
    subscriptions: list[dict[str, object]] = []
    errors: list[Exception] = []
    for tenant_id in dict.fromkeys(item for item in tenant_ids if item):
        try:
            rows = _paged_values(
                f"{ARM_ENDPOINT}/subscriptions",
                tenant_id,
                timeout,
                params={"api-version": "2020-01-01"},
            )
        except (IdentityUnavailableError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(error)
            continue
        for row in rows:
            if str(row.get("state", "Enabled")).casefold() != "enabled":
                continue
            subscription_id = str(row.get("subscriptionId", row.get("id", "")))
            if not subscription_id:
                continue
            normalized = {
                "id": subscription_id,
                "name": str(row.get("displayName", row.get("name", ""))),
                "tenantId": tenant_id,
                "state": "Enabled",
            }
            subscriptions.append(normalized)
            with _CACHE_LOCK:
                _SUBSCRIPTION_TENANTS[subscription_id.casefold()] = tenant_id
    if not subscriptions and errors:
        raise errors[0]
    unique = {str(item["id"]).casefold(): item for item in subscriptions}
    return list(unique.values())


def _tenant_for_subscription(subscription_id: str, timeout: int) -> str:
    with _CACHE_LOCK:
        cached = _SUBSCRIPTION_TENANTS.get(subscription_id.casefold())
    if cached:
        return cached
    for subscription in _subscriptions(timeout):
        if str(subscription["id"]).casefold() == subscription_id.casefold():
            return str(subscription["tenantId"])
    raise OSError("The selected Azure subscription is not available to this connection.")


def _resource_shape(resource_id: str) -> tuple[str, str, str]:
    parts = [item for item in resource_id.strip("/").split("/") if item]
    lowered = [item.casefold() for item in parts]
    try:
        subscription_id = parts[lowered.index("subscriptions") + 1]
        provider_index = lowered.index("providers")
        namespace = parts[provider_index + 1]
    except (ValueError, IndexError) as error:
        raise OSError("The Azure resource ID is incomplete.") from error
    provider_parts = parts[provider_index + 2 :]
    resource_type = "/".join(provider_parts[0::2])
    if not resource_type:
        raise OSError("The Azure resource type is missing.")
    return subscription_id, namespace, resource_type


def _resource_api_version(resource_id: str, tenant_id: str, timeout: int) -> str:
    subscription_id, namespace, resource_type = _resource_shape(resource_id)
    key = (subscription_id.casefold(), namespace.casefold(), resource_type.casefold())
    with _CACHE_LOCK:
        cached = _RESOURCE_API_VERSIONS.get(key)
    if cached:
        return cached
    payload = _request_json(
        "GET",
        f"{ARM_ENDPOINT}/subscriptions/{quote(subscription_id)}/providers/{quote(namespace)}",
        tenant_id,
        timeout,
        params={"api-version": "2021-04-01"},
    )
    if not isinstance(payload, dict):
        raise OSError("Azure returned unreadable provider metadata.")
    versions: list[str] = []
    for item in payload.get("resourceTypes", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("resourceType", "")).casefold() == resource_type.casefold():
            versions = [str(value) for value in item.get("apiVersions", []) if value]
            break
    stable = sorted(
        (value for value in versions if "preview" not in value.casefold()), reverse=True
    )
    selected = stable[0] if stable else (sorted(versions, reverse=True)[0] if versions else "")
    if not selected:
        raise OSError("Azure did not advertise an API version for this resource type.")
    with _CACHE_LOCK:
        _RESOURCE_API_VERSIONS[key] = selected
    return selected


def _resource_document(resource_id: str, tenant_id: str, timeout: int) -> object:
    version = _resource_api_version(resource_id, tenant_id, timeout)
    return _request_json(
        "GET",
        f"{ARM_ENDPOINT}{resource_id}",
        tenant_id,
        timeout,
        params={"api-version": version},
    )


def _property(payload: object, path: str) -> object:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _NOT_FOUND
        current = current[part]
    return current


def _resource_group(resource_id: str) -> str:
    parts = [item for item in resource_id.strip("/").split("/") if item]
    lowered = [item.casefold() for item in parts]
    try:
        return parts[lowered.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return ""


def _success(arguments: list[str], payload: object, *, text: bool = False):
    stdout = str(payload) if text else json.dumps(payload, separators=(",", ":"))
    return subprocess.CompletedProcess(arguments, 0, stdout, "")


def execute_azure_operation(
    arguments: list[str], timeout: int
) -> subprocess.CompletedProcess[str]:
    """Execute the Beacon's fixed Azure operation set through OAuth + HTTPS."""
    try:
        prefix = tuple(arguments[:4])
        if arguments[:2] == ["account", "list"]:
            return _success(arguments, _subscriptions(timeout))

        if arguments[:2] == ["account", "show"]:
            subscription_id = _option(arguments, "--subscription")
            return _success(
                arguments,
                _tenant_for_subscription(subscription_id, timeout),
                text=True,
            )

        if arguments[:2] == ["group", "list"]:
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            rows = _paged_values(
                f"{ARM_ENDPOINT}/subscriptions/{quote(subscription_id)}/resourcegroups",
                tenant,
                timeout,
                params={"api-version": "2021-04-01"},
            )
            return _success(arguments, len(rows), text=True)

        if prefix == ("monitor", "log-analytics", "workspace", "list"):
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            rows = _paged_values(
                f"{ARM_ENDPOINT}/subscriptions/{quote(subscription_id)}/providers/Microsoft.OperationalInsights/workspaces",
                tenant,
                timeout,
                params={"api-version": "2022-10-01"},
            )
            normalized = []
            for row in rows:
                properties = row.get("properties", {})
                properties = properties if isinstance(properties, dict) else {}
                resource_id = str(row.get("id", ""))
                normalized.append(
                    {
                        **row,
                        "customerId": properties.get("customerId", ""),
                        "resourceGroup": _resource_group(resource_id),
                    }
                )
            return _success(arguments, normalized)

        if prefix == ("monitor", "log-analytics", "workspace", "get-schema"):
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            name = _option(arguments, "--workspace-name")
            group = _option(arguments, "--resource-group")
            workspaces = _paged_values(
                f"{ARM_ENDPOINT}/subscriptions/{quote(subscription_id)}/resourceGroups/{quote(group)}/providers/Microsoft.OperationalInsights/workspaces",
                tenant,
                timeout,
                params={"api-version": "2022-10-01"},
            )
            workspace = next(
                (row for row in workspaces if str(row.get("name", "")).casefold() == name.casefold()),
                None,
            )
            if workspace is None:
                raise OSError("The selected Log Analytics workspace was not found.")
            customer_id = str((workspace.get("properties") or {}).get("customerId", ""))
            payload = _request_json(
                "GET",
                f"{LOG_ENDPOINT}/v1/workspaces/{quote(customer_id)}/metadata",
                tenant,
                timeout,
                scope=LOG_ANALYTICS_SCOPE,
            )
            return _success(arguments, payload)

        if arguments[:2] == ["resource", "list"]:
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            rows = _paged_values(
                f"{ARM_ENDPOINT}/subscriptions/{quote(subscription_id)}/resources",
                tenant,
                timeout,
                params={"api-version": "2021-04-01"},
            )
            match = re.search(r"\[:(\d+)\]", _option(arguments, "--query"))
            if match:
                rows = rows[: int(match.group(1))]
            for row in rows:
                if not row.get("resourceGroup"):
                    row["resourceGroup"] = _resource_group(str(row.get("id", "")))
            return _success(arguments, rows)

        if arguments[:3] == ["monitor", "metrics", "list-definitions"]:
            resource_id = _option(arguments, "--resource")
            subscription_id, _, _ = _resource_shape(resource_id)
            tenant = _tenant_for_subscription(subscription_id, timeout)
            payload = _request_json(
                "GET",
                f"{ARM_ENDPOINT}{resource_id}/providers/microsoft.insights/metricDefinitions",
                tenant,
                timeout,
                params={"api-version": "2018-01-01"},
            )
            rows = payload.get("value", []) if isinstance(payload, dict) else []
            return _success(arguments, rows)

        if arguments[:2] == ["resource", "show"]:
            resource_id = _option(arguments, "--ids")
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            payload = _resource_document(resource_id, tenant, timeout)
            query = _option(arguments, "--query")
            value = _property(payload, query) if query else payload
            if value is _NOT_FOUND:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            if "--output" in arguments and _option(arguments, "--output") == "tsv":
                return _success(arguments, "" if value is None else value, text=True)
            return _success(arguments, value)

        if arguments[:2] == ["vm", "get-instance-view"]:
            resource_id = _option(arguments, "--ids")
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            version = _resource_api_version(resource_id, tenant, timeout)
            payload = _request_json(
                "GET",
                f"{ARM_ENDPOINT}{resource_id}/instanceView",
                tenant,
                timeout,
                params={"api-version": version},
            )
            return _success(arguments, payload)

        if arguments[:1] == ["rest"]:
            url = _option(arguments, "--url")
            subscription_id = _option(arguments, "--subscription")
            tenant = _tenant_for_subscription(subscription_id, timeout)
            body_option = _option(arguments, "--body")
            body = json.loads(Path(body_option[1:]).read_text(encoding="utf-8"))
            payload = _request_json("POST", url, tenant, timeout, body=body)
            return _success(arguments, payload)

        if arguments[:3] == ["monitor", "log-analytics", "query"]:
            workspace_id = _option(arguments, "--workspace")
            tenant = _option(arguments, "--tenant") or home_tenant_id()
            payload = _request_json(
                "POST",
                f"{LOG_ENDPOINT}/v1/workspaces/{quote(workspace_id)}/query",
                tenant,
                timeout,
                scope=LOG_ANALYTICS_SCOPE,
                body={
                    "query": _option(arguments, "--analytics-query"),
                    "timespan": _option(arguments, "--timespan"),
                },
            )
            return _success(arguments, payload)

        if arguments[:3] == ["monitor", "metrics", "list"]:
            resource_id = _option(arguments, "--resource")
            subscription_id, _, _ = _resource_shape(resource_id)
            tenant = _tenant_for_subscription(subscription_id, timeout)
            lookback = int(_option(arguments, "--offset", "5m").rstrip("m"))
            end = datetime.now(UTC)
            start = end - timedelta(minutes=lookback)
            params = {
                "api-version": "2018-01-01",
                "metricnames": _option(arguments, "--metric"),
                "aggregation": _option(arguments, "--aggregation"),
                "timespan": f"{start.isoformat()}/{end.isoformat()}",
                "interval": "PT1M",
            }
            namespace = _option(arguments, "--namespace")
            metric_filter = _option(arguments, "--filter")
            if namespace:
                params["metricnamespace"] = namespace
            if metric_filter:
                params["$filter"] = metric_filter
            payload = _request_json(
                "GET",
                f"{ARM_ENDPOINT}{resource_id}/providers/microsoft.insights/metrics",
                tenant,
                timeout,
                params=params,
            )
            return _success(arguments, payload)

        raise ValueError("Unsupported internal Azure operation")
    except subprocess.TimeoutExpired:
        raise
    except (IdentityUnavailableError, OSError, ValueError, TypeError) as error:
        return subprocess.CompletedProcess(arguments, 1, "", str(error)[:500])
