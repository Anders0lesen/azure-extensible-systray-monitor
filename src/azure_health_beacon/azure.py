from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import fmean

from .config import app_data_dir
from .model import CheckDefinition, CheckFinding, CheckResult, CheckState

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RESOURCE_GRAPH_URL = (
    "https://management.azure.com/providers/Microsoft.ResourceGraph/resources"
    "?api-version=2022-10-01"
)
RESOURCE_GRAPH_BATCH_SIZE = 1000
MAX_FINDINGS_TO_DISPLAY = 25


@dataclass(frozen=True, slots=True)
class AzureSubscription:
    id: str
    name: str
    tenant_id: str


@dataclass(frozen=True, slots=True)
class AzureWorkspace:
    name: str
    customer_id: str
    resource_id: str
    subscription_id: str
    resource_group: str


@dataclass(frozen=True, slots=True)
class AzureResource:
    name: str
    resource_id: str
    resource_type: str
    subscription_id: str
    resource_group: str


@dataclass(frozen=True, slots=True)
class AzureMetricDefinition:
    name: str
    display_name: str
    namespace: str
    unit: str
    aggregations: tuple[str, ...]
    dimensions: tuple[str, ...]


def _redact(text: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/]+=*", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(access[_ -]?token[\"'=:\s]+)[^\s,}\"]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)([?&]sig=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+", "[REDACTED]", text
    )
    return " ".join(text.strip().split())[:500]


def _azure_cli_command() -> list[str] | None:
    # Prefer the machine-wide Azure CLI install over a same-named executable
    # supplied by the working directory or an earlier, user-writable PATH entry.
    candidates: list[tuple[str, str]] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            root = os.path.join(base, "Microsoft SDKs", "Azure", "CLI2")
            candidates.append(
                (os.path.join(root, "wbin", "az.cmd"), os.path.join(root, "python.exe"))
            )
    for batch_file, python_executable in candidates:
        if os.path.isfile(batch_file) and os.path.isfile(python_executable):
            # Calling the CLI module directly avoids passing imported rule data
            # through cmd.exe/batch-file expansion.
            return [os.path.abspath(python_executable), "-IBm", "azure.cli"]
    discovered = shutil.which("az.exe")
    if not discovered:
        return None
    resolved = os.path.abspath(discovered)
    if os.path.dirname(resolved).casefold() == os.getcwd().casefold():
        return None
    return [resolved]


def _run_az(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    prefix = _azure_cli_command()
    if not prefix:
        raise FileNotFoundError(
            "A trusted machine-wide Azure CLI installation was not found"
        )
    command = [*prefix, *arguments]
    environment = os.environ.copy()
    isolated_dir = isolated_azure_config_dir()
    isolated_dir.mkdir(parents=True, exist_ok=True)
    environment["AZURE_CONFIG_DIR"] = str(isolated_dir)
    environment["AZURE_CORE_COLLECT_TELEMETRY"] = "false"
    environment["AZURE_CORE_ONLY_SHOW_ERRORS"] = "true"
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        env=environment,
        check=False,
    )


def isolated_azure_config_dir() -> Path:
    return app_data_dir() / "azure-cli"


def delete_isolated_azure_state() -> None:
    """Delete only the Beacon-owned Azure CLI profile, never the user's normal CLI profile."""
    parent = app_data_dir().resolve()
    target = isolated_azure_config_dir().resolve()
    if target.parent != parent or target.name != "azure-cli":
        raise RuntimeError("Refusing to delete an unexpected Azure CLI profile path")
    if target.exists():
        shutil.rmtree(target)


def interactive_login(
    tenant_hint: str = "", timeout_seconds: int = 300
) -> tuple[bool, str]:
    """Open Microsoft's interactive sign-in; no password or token is returned to the Beacon."""
    arguments = ["login", "--output", "none", "--only-show-errors"]
    if tenant_hint.strip():
        arguments.extend(["--tenant", tenant_hint.strip()])
    try:
        completed = _run_az(arguments, timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, "Microsoft sign-in did not complete within five minutes."
    except (FileNotFoundError, OSError) as error:
        return False, _redact(str(error))
    if completed.returncode != 0:
        return False, _redact(
            completed.stderr or "Microsoft sign-in was not completed."
        )
    return True, "Microsoft sign-in completed."


def list_subscriptions(
    timeout_seconds: int = 30,
) -> tuple[list[AzureSubscription], str]:
    try:
        completed = _run_az(
            [
                "account",
                "list",
                "--all",
                "--query",
                "[?state=='Enabled'].{id:id,name:name,tenantId:tenantId}",
                "--output",
                "json",
                "--only-show-errors",
            ],
            timeout_seconds,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        return [], _redact(str(error))
    if completed.returncode != 0:
        return [], _redact(completed.stderr or "Could not read Azure subscriptions.")
    try:
        raw_items = json.loads(completed.stdout)
        subscriptions = [
            AzureSubscription(str(item["id"]), str(item["name"]), str(item["tenantId"]))
            for item in raw_items
            if item.get("id") and item.get("tenantId")
        ]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return [], "Azure CLI returned an unreadable subscription list."
    subscriptions.sort(key=lambda item: (item.name.casefold(), item.id))
    if not subscriptions:
        return (
            [],
            "The sign-in succeeded, but no enabled Azure subscriptions were found.",
        )
    return subscriptions, ""


def validate_subscription_access(
    subscription: AzureSubscription, timeout_seconds: int = 60
) -> tuple[bool, str]:
    """Make a live, read-only ARM request without retrieving a credential."""
    try:
        completed = _run_az(
            [
                "group",
                "list",
                "--subscription",
                subscription.id,
                "--query",
                "length(@)",
                "--output",
                "tsv",
                "--only-show-errors",
            ],
            timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, "Azure access validation timed out."
    except (FileNotFoundError, OSError) as error:
        return False, _redact(str(error))
    if completed.returncode != 0:
        return False, _redact(
            completed.stderr or "Azure rejected the validation request."
        )
    count = completed.stdout.strip() or "0"
    return (
        True,
        f"Azure access verified. The subscription contains {count} resource groups.",
    )


def discover_workspaces(
    timeout_seconds: int = 45,
) -> tuple[list[AzureWorkspace], list[str]]:
    """Return readable Log Analytics workspaces without changing CLI context."""
    subscriptions, error = list_subscriptions(timeout_seconds)
    if error:
        return [], [error]
    workspaces: list[AzureWorkspace] = []
    errors: list[str] = []
    for subscription in subscriptions:
        try:
            completed = _run_az(
                [
                    "monitor",
                    "log-analytics",
                    "workspace",
                    "list",
                    "--subscription",
                    subscription.id,
                    "--output",
                    "json",
                    "--only-show-errors",
                ],
                timeout_seconds,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(_redact(str(exc)))
            continue
        if completed.returncode != 0:
            errors.append(_redact(completed.stderr or "Workspace discovery failed."))
            continue
        try:
            rows = json.loads(completed.stdout)
            for row in rows:
                customer_id = str(row.get("customerId", ""))
                resource_id = str(row.get("id", ""))
                if customer_id and resource_id:
                    workspaces.append(
                        AzureWorkspace(
                            name=str(row.get("name", "Unnamed workspace")),
                            customer_id=customer_id,
                            resource_id=resource_id,
                            subscription_id=subscription.id,
                            resource_group=str(row.get("resourceGroup", "")),
                        )
                    )
        except (json.JSONDecodeError, TypeError, AttributeError):
            errors.append("Azure returned an unreadable workspace list.")
    workspaces.sort(key=lambda item: (item.name.casefold(), item.subscription_id))
    return workspaces, errors


def discover_workspace_tables(
    workspace: AzureWorkspace, timeout_seconds: int = 45
) -> tuple[list[str], str]:
    """Read the workspace schema. This does not query or retain table rows."""
    arguments = [
        "monitor",
        "log-analytics",
        "workspace",
        "get-schema",
        "--resource-group",
        workspace.resource_group,
        "--workspace-name",
        workspace.name,
        "--subscription",
        workspace.subscription_id,
        "--output",
        "json",
        "--only-show-errors",
    ]
    try:
        completed = _run_az(arguments, timeout_seconds)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return [], _redact(str(exc))
    if completed.returncode != 0:
        return [], _redact(completed.stderr or "Workspace schema discovery failed.")
    try:
        payload = json.loads(completed.stdout)
        raw_tables = payload.get("tables", payload.get("value", []))
        names = sorted(
            {
                str(item.get("name", "")).strip()
                for item in raw_tables
                if isinstance(item, dict) and item.get("name")
            },
            key=str.casefold,
        )
    except (json.JSONDecodeError, TypeError, AttributeError):
        return [], "Azure returned an unreadable workspace schema."
    return names, ""


def discover_resources(
    timeout_seconds: int = 45, limit: int = 1000
) -> tuple[list[AzureResource], list[str]]:
    """List readable ARM resources across the signed-in subscriptions."""
    subscriptions, error = list_subscriptions(timeout_seconds)
    if error:
        return [], [error]
    resources: list[AzureResource] = []
    errors: list[str] = []
    remaining = limit
    for subscription in subscriptions:
        if remaining <= 0:
            break
        try:
            completed = _run_az(
                [
                    "resource",
                    "list",
                    "--subscription",
                    subscription.id,
                    "--query",
                    f"[:{remaining}].{{name:name,id:id,type:type,resourceGroup:resourceGroup}}",
                    "--output",
                    "json",
                    "--only-show-errors",
                ],
                timeout_seconds,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(_redact(str(exc)))
            continue
        if completed.returncode != 0:
            errors.append(_redact(completed.stderr or "Resource discovery failed."))
            continue
        try:
            rows = json.loads(completed.stdout)
            for row in rows:
                resource_id = str(row.get("id", ""))
                if resource_id:
                    resources.append(
                        AzureResource(
                            name=str(row.get("name", "Unnamed resource")),
                            resource_id=resource_id,
                            resource_type=str(row.get("type", "")),
                            subscription_id=subscription.id,
                            resource_group=str(row.get("resourceGroup", "")),
                        )
                    )
            remaining = limit - len(resources)
        except (json.JSONDecodeError, TypeError, AttributeError):
            errors.append("Azure returned an unreadable resource list.")
    resources.sort(
        key=lambda item: (item.resource_type.casefold(), item.name.casefold())
    )
    return resources, errors


def discover_metric_definitions(
    resource_id: str, timeout_seconds: int = 45
) -> tuple[list[AzureMetricDefinition], str]:
    arguments = [
        "monitor",
        "metrics",
        "list-definitions",
        "--resource",
        resource_id,
        "--output",
        "json",
        "--only-show-errors",
    ]
    try:
        completed = _run_az(arguments, timeout_seconds)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return [], _redact(str(exc))
    if completed.returncode != 0:
        return [], _redact(completed.stderr or "Metric discovery failed.")
    try:
        rows = json.loads(completed.stdout)
        definitions = []
        for row in rows:
            name_data = row.get("name", {})
            name = str(name_data.get("value", ""))
            if not name:
                continue
            definitions.append(
                AzureMetricDefinition(
                    name=name,
                    display_name=str(name_data.get("localizedValue", name)),
                    namespace=str(row.get("namespace", "")),
                    unit=str(row.get("unit", "")),
                    aggregations=tuple(
                        str(value) for value in row.get("supportedAggregationTypes", [])
                    ),
                    dimensions=tuple(
                        str(value.get("value", ""))
                        for value in row.get("dimensions", [])
                        if isinstance(value, dict) and value.get("value")
                    ),
                )
            )
    except (json.JSONDecodeError, TypeError, AttributeError):
        return [], "Azure returned unreadable metric definitions."
    definitions.sort(key=lambda item: item.display_name.casefold())
    return definitions, ""


def _portal_url(definition: CheckDefinition) -> str:
    if definition.portal_url:
        return definition.portal_url
    tenant = f"@{definition.tenant_id}" if definition.tenant_id else ""
    return (
        f"https://portal.azure.com/#{tenant}/resource{definition.resource_id}/overview"
    )


def run_provisioning_check(
    definition: CheckDefinition, *, timeout_seconds: int = 30, retry_count: int = 2
) -> CheckResult:
    portal_url = _portal_url(definition)
    if not _azure_cli_command():
        return CheckResult(
            definition.id,
            definition.name,
            CheckState.UNCONNECTABLE,
            "A trusted machine-wide Azure CLI installation was not found.",
            portal_url=portal_url,
        )

    if definition.tenant_id:
        tenant_result = _run_az(
            [
                "account",
                "show",
                "--subscription",
                definition.subscription_id,
                "--query",
                "tenantId",
                "--output",
                "tsv",
                "--only-show-errors",
            ],
            timeout_seconds,
        )
        if tenant_result.returncode != 0:
            return CheckResult(
                definition.id,
                definition.name,
                CheckState.UNCONNECTABLE,
                _redact(
                    tenant_result.stderr
                    or "Could not verify the configured Azure tenant."
                ),
                portal_url=portal_url,
            )
        actual_tenant = tenant_result.stdout.strip()
        if actual_tenant.casefold() != definition.tenant_id.casefold():
            return CheckResult(
                definition.id,
                definition.name,
                CheckState.UNCONNECTABLE,
                "The subscription is available, but its tenant does not match the rule's safety pin.",
                portal_url=portal_url,
            )

    arguments = [
        "resource",
        "show",
        "--ids",
        definition.resource_id,
        "--subscription",
        definition.subscription_id,
        "--query",
        "properties.provisioningState",
        "--output",
        "tsv",
        "--only-show-errors",
    ]
    last_error = "Azure did not return a result."
    for attempt in range(retry_count + 1):
        try:
            completed = _run_az(arguments, timeout_seconds)
        except subprocess.TimeoutExpired:
            last_error = f"Azure lookup timed out after {timeout_seconds} seconds."
        except (FileNotFoundError, OSError) as error:
            last_error = _redact(str(error))
        else:
            if completed.returncode == 0:
                observed = completed.stdout.strip()
                if not observed:
                    return CheckResult(
                        definition.id,
                        definition.name,
                        CheckState.FAILED,
                        "Azure responded, but the resource has no provisioningState value.",
                        observed_value="Missing",
                        portal_url=portal_url,
                    )
                expected = {value.casefold() for value in definition.expected_values}
                healthy = observed.casefold() in expected
                if healthy:
                    summary = f"Provisioning state is {observed}."
                    state = CheckState.HEALTHY
                else:
                    expected_text = ", ".join(definition.expected_values)
                    summary = (
                        f"Provisioning state is {observed}; expected {expected_text}."
                    )
                    state = CheckState.FAILED
                return CheckResult(
                    definition.id,
                    definition.name,
                    state,
                    summary,
                    observed_value=observed,
                    portal_url=portal_url,
                )
            last_error = _redact(
                completed.stderr or completed.stdout or "Azure CLI lookup failed."
            )
            lowered = last_error.casefold()
            if any(
                marker in lowered
                for marker in ("az login", "login", "authentication", "credential")
            ):
                last_error = (
                    "Azure sign-in is required. Open Azure CLI and run az login."
                )
        if attempt < retry_count:
            time.sleep(min(2**attempt, 4))
    return CheckResult(
        definition.id,
        definition.name,
        CheckState.UNCONNECTABLE,
        last_error,
        checked_at=datetime.now().astimezone(),
        portal_url=portal_url,
    )


def _finding_from_row(row: dict[str, object]) -> CheckFinding:
    title_keys = (
        "title",
        "name",
        "targetResourceName",
        "policyAssignmentName",
        "availabilityState",
        "id",
    )
    title = next((str(row[key]) for key in title_keys if row.get(key)), "Azure finding")
    details = []
    for key, value in row.items():
        if key.casefold() in {"id", "subscriptionid", "title"} or value in (None, ""):
            continue
        rendered = " ".join(str(value).split())
        details.append(f"{key}: {rendered[:120]}")
        if len(details) == 4:
            break
    resource_id = str(
        row.get("id") or row.get("resourceId") or row.get("targetResourceId") or ""
    )
    portal_url = ""
    if resource_id.casefold().startswith("/subscriptions/"):
        portal_url = f"https://portal.azure.com/#/resource{resource_id}/overview"
    return CheckFinding(
        title=title[:200], summary=" • ".join(details)[:500], portal_url=portal_url
    )


def _graph_rows(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise TypeError("Resource Graph returned an unreadable response")
    data = payload.get("data", [])
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        columns = data.get("columns", [])
        rows = data.get("rows", [])
        if isinstance(columns, list) and isinstance(rows, list):
            names = [
                str(item.get("name", "")) for item in columns if isinstance(item, dict)
            ]
            return [
                dict(zip(names, row, strict=False))
                for row in rows
                if isinstance(row, list)
            ]
    raise ValueError("Resource Graph returned an unreadable result table")


def run_resource_graph_check(
    definition: CheckDefinition, *, timeout_seconds: int = 30, retry_count: int = 2
) -> CheckResult:
    """Run a data-only findings query across every enabled subscription in the login."""
    subscriptions, subscription_error = list_subscriptions(timeout_seconds)
    if subscription_error:
        return CheckResult(
            definition.id,
            definition.name,
            CheckState.UNCONNECTABLE,
            subscription_error,
            portal_url=definition.portal_url,
        )

    by_tenant: dict[str, list[AzureSubscription]] = defaultdict(list)
    for subscription in subscriptions:
        by_tenant[subscription.tenant_id].append(subscription)

    findings: list[CheckFinding] = []
    total_records = 0
    errors: list[str] = []
    for tenant_subscriptions in by_tenant.values():
        for offset in range(0, len(tenant_subscriptions), RESOURCE_GRAPH_BATCH_SIZE):
            batch = tenant_subscriptions[offset : offset + RESOURCE_GRAPH_BATCH_SIZE]
            request = {
                "subscriptions": [item.id for item in batch],
                "query": definition.query,
                "options": {
                    "resultFormat": "objectArray",
                    "$top": MAX_FINDINGS_TO_DISPLAY,
                },
            }
            app_data_dir().mkdir(parents=True, exist_ok=True)
            handle, request_name = tempfile.mkstemp(
                prefix="resource-graph-", suffix=".json", dir=app_data_dir()
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(request, stream)
                arguments = [
                    "rest",
                    "--method",
                    "post",
                    "--url",
                    RESOURCE_GRAPH_URL,
                    "--subscription",
                    batch[0].id,
                    "--body",
                    f"@{request_name}",
                    "--output",
                    "json",
                    "--only-show-errors",
                ]
                last_error = "Resource Graph did not return a result."
                for attempt in range(retry_count + 1):
                    try:
                        completed = _run_az(arguments, timeout_seconds)
                    except subprocess.TimeoutExpired:
                        last_error = (
                            f"Resource Graph timed out after {timeout_seconds} seconds."
                        )
                    except (FileNotFoundError, OSError) as error:
                        last_error = _redact(str(error))
                    else:
                        if completed.returncode == 0:
                            try:
                                payload = json.loads(completed.stdout)
                                rows = _graph_rows(payload)
                                reported = int(payload.get("totalRecords", len(rows)))
                            except (
                                json.JSONDecodeError,
                                TypeError,
                                ValueError,
                            ) as error:
                                last_error = _redact(str(error))
                            else:
                                total_records += max(reported, len(rows))
                                remaining = MAX_FINDINGS_TO_DISPLAY - len(findings)
                                findings.extend(
                                    _finding_from_row(row) for row in rows[:remaining]
                                )
                                last_error = ""
                                break
                        else:
                            last_error = _redact(
                                completed.stderr
                                or completed.stdout
                                or "Resource Graph query failed."
                            )
                    if attempt < retry_count:
                        time.sleep(min(2**attempt, 4))
                if last_error:
                    errors.append(last_error)
            finally:
                Path(request_name).unlink(missing_ok=True)

    scope_text = f"{len(subscriptions)} accessible subscription"
    if len(subscriptions) != 1:
        scope_text += "s"
    if total_records:
        summary = f"Found {total_records} matching row(s) across {scope_text}."
        if errors:
            summary += " Some tenant scopes could not be checked."
        return CheckResult(
            definition.id,
            definition.name,
            CheckState.FAILED,
            summary,
            observed_value=str(total_records),
            portal_url=definition.portal_url,
            findings=findings,
        )
    if errors:
        return CheckResult(
            definition.id,
            definition.name,
            CheckState.UNCONNECTABLE,
            f"Could not verify every Azure scope: {errors[0]}",
            portal_url=definition.portal_url,
        )
    return CheckResult(
        definition.id,
        definition.name,
        CheckState.HEALTHY,
        f"No matching rows across {scope_text}.",
        observed_value="0",
        portal_url=definition.portal_url,
    )


def _log_rows(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("value"), list):
            return [row for row in payload["value"] if isinstance(row, dict)]
        tables = payload.get("tables")
        if isinstance(tables, list) and tables:
            first = tables[0]
            if isinstance(first, dict):
                columns = first.get("columns", [])
                rows = first.get("rows", [])
                names = [
                    str(column.get("name", ""))
                    for column in columns
                    if isinstance(column, dict)
                ]
                return [
                    dict(zip(names, row, strict=False))
                    for row in rows
                    if isinstance(row, list)
                ]
    raise ValueError("Log Analytics returned an unreadable result table")


def run_log_analytics_check(
    definition: CheckDefinition, *, timeout_seconds: int = 30, retry_count: int = 2
) -> CheckResult:
    bounded_query = f"{definition.query.rstrip()}\n| take {MAX_FINDINGS_TO_DISPLAY + 1}"
    arguments = [
        "monitor",
        "log-analytics",
        "query",
        "--workspace",
        definition.workspace_id,
        "--analytics-query",
        bounded_query,
        "--timespan",
        f"PT{definition.lookback_minutes}M",
        "--output",
        "json",
        "--only-show-errors",
    ]
    last_error = "Log Analytics did not return a result."
    for attempt in range(retry_count + 1):
        try:
            completed = _run_az(arguments, timeout_seconds)
        except subprocess.TimeoutExpired:
            last_error = f"Log query timed out after {timeout_seconds} seconds."
        except (FileNotFoundError, OSError) as exc:
            last_error = _redact(str(exc))
        else:
            if completed.returncode == 0:
                try:
                    rows = _log_rows(json.loads(completed.stdout))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    last_error = _redact(str(exc))
                else:
                    findings = [
                        _finding_from_row(row) for row in rows[:MAX_FINDINGS_TO_DISPLAY]
                    ]
                    if rows:
                        count_text = (
                            f"{MAX_FINDINGS_TO_DISPLAY}+"
                            if len(rows) > MAX_FINDINGS_TO_DISPLAY
                            else str(len(rows))
                        )
                        return CheckResult(
                            definition.id,
                            definition.name,
                            CheckState.FAILED,
                            f"Found {count_text} matching log row(s) in the last {definition.lookback_minutes} minute(s).",
                            observed_value=count_text,
                            portal_url=definition.portal_url,
                            findings=findings,
                        )
                    return CheckResult(
                        definition.id,
                        definition.name,
                        CheckState.HEALTHY,
                        f"No matching log rows in the last {definition.lookback_minutes} minute(s).",
                        observed_value="0",
                        portal_url=definition.portal_url,
                    )
            else:
                last_error = _redact(
                    completed.stderr or completed.stdout or "Log query failed."
                )
        if attempt < retry_count:
            time.sleep(min(2**attempt, 4))
    return CheckResult(
        definition.id,
        definition.name,
        CheckState.UNCONNECTABLE,
        last_error,
        portal_url=definition.portal_url,
    )


def _metric_values(payload: object, aggregation: str) -> list[tuple[str, float]]:
    if not isinstance(payload, dict):
        raise TypeError("Azure Monitor returned an unreadable metric response")
    values: list[tuple[str, float]] = []
    key = aggregation.casefold()
    for metric in payload.get("value", []):
        if not isinstance(metric, dict):
            continue
        for series in metric.get("timeseries", []):
            if not isinstance(series, dict):
                continue
            for point in series.get("data", []):
                if not isinstance(point, dict) or point.get(key) is None:
                    continue
                values.append((str(point.get("timeStamp", "")), float(point[key])))
    return values


def _reduce_metric(values: list[tuple[str, float]], reducer: str) -> float:
    numbers = [value for _, value in values]
    if reducer == "latest":
        return max(values, key=lambda item: item[0])[1]
    if reducer == "maximum":
        return max(numbers)
    if reducer == "minimum":
        return min(numbers)
    if reducer == "average":
        return fmean(numbers)
    return sum(numbers)


def _compare_metric(value: float, operator: str, threshold: float) -> bool:
    comparisons = {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
        "eq": value == threshold,
        "ne": value != threshold,
    }
    return comparisons[operator]


def run_metric_check(
    definition: CheckDefinition, *, timeout_seconds: int = 30, retry_count: int = 2
) -> CheckResult:
    arguments = [
        "monitor",
        "metrics",
        "list",
        "--resource",
        definition.resource_id,
        "--metrics",
        definition.metric_name,
        "--aggregation",
        definition.metric_aggregation,
        "--offset",
        f"{definition.lookback_minutes}m",
        "--interval",
        "1m",
        "--output",
        "json",
        "--only-show-errors",
    ]
    if definition.metric_namespace:
        arguments.extend(["--namespace", definition.metric_namespace])
    if definition.metric_filter:
        arguments.extend(["--filter", definition.metric_filter])
    last_error = "Azure Monitor did not return metric data."
    for attempt in range(retry_count + 1):
        try:
            completed = _run_az(arguments, timeout_seconds)
        except subprocess.TimeoutExpired:
            last_error = f"Metric query timed out after {timeout_seconds} seconds."
        except (FileNotFoundError, OSError) as exc:
            last_error = _redact(str(exc))
        else:
            if completed.returncode == 0:
                try:
                    values = _metric_values(
                        json.loads(completed.stdout), definition.metric_aggregation
                    )
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    last_error = _redact(str(exc))
                else:
                    if not values:
                        return CheckResult(
                            definition.id,
                            definition.name,
                            CheckState.UNCONNECTABLE,
                            "Azure returned no metric samples; health cannot be proven.",
                            portal_url=_portal_url(definition),
                        )
                    observed = _reduce_metric(values, definition.metric_reducer)
                    tripped = _compare_metric(
                        observed,
                        definition.metric_operator,
                        definition.metric_threshold,
                    )
                    symbol = {
                        "gt": ">",
                        "gte": "≥",
                        "lt": "<",
                        "lte": "≤",
                        "eq": "=",
                        "ne": "≠",
                    }[definition.metric_operator]
                    summary = (
                        f"{definition.metric_name} is {observed:g}; "
                        f"alert condition is {symbol} {definition.metric_threshold:g}."
                    )
                    return CheckResult(
                        definition.id,
                        definition.name,
                        CheckState.FAILED if tripped else CheckState.HEALTHY,
                        summary,
                        observed_value=f"{observed:g}",
                        portal_url=_portal_url(definition),
                    )
            else:
                last_error = _redact(
                    completed.stderr or completed.stdout or "Metric query failed."
                )
        if attempt < retry_count:
            time.sleep(min(2**attempt, 4))
    return CheckResult(
        definition.id,
        definition.name,
        CheckState.UNCONNECTABLE,
        last_error,
        portal_url=_portal_url(definition),
    )


def run_check(
    definition: CheckDefinition, *, timeout_seconds: int = 30, retry_count: int = 2
) -> CheckResult:
    if definition.kind == "azure_resource_graph":
        return run_resource_graph_check(
            definition, timeout_seconds=timeout_seconds, retry_count=retry_count
        )
    if definition.kind == "azure_log_analytics":
        return run_log_analytics_check(
            definition, timeout_seconds=timeout_seconds, retry_count=retry_count
        )
    if definition.kind == "azure_monitor_metric":
        return run_metric_check(
            definition, timeout_seconds=timeout_seconds, retry_count=retry_count
        )
    return run_provisioning_check(
        definition, timeout_seconds=timeout_seconds, retry_count=retry_count
    )
