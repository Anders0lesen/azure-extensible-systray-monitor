from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .model import CheckDefinition

APP_NAME = "AzureHealthBeacon"
SCHEMA_VERSION = 3
RULE_PACK_FORMAT = "azure-health-beacon-rule-pack"
RULE_PACK_SCHEMA_VERSION = 1
MAX_RULE_PACK_BYTES = 1_000_000
MAX_RULES_PER_PACK = 500
AUTHORIZATION_MAX_AGE = timedelta(days=14)
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "credential", "access_key")
CHECK_KEYS = {
    "id",
    "name",
    "resource_id",
    "portal_url",
    "tenant_id",
    "expected_values",
    "enabled",
    "kind",
}


@dataclass(slots=True)
class AppConfig:
    schema_version: int = SCHEMA_VERSION
    onboarding_completed: bool = False
    azure_subscription_id: str = ""
    azure_subscription_name: str = ""
    azure_tenant_id: str = ""
    connection_established_utc: str = ""
    connection_purge_pending: bool = False
    interval_minutes: int = 5
    timeout_seconds: int = 30
    retry_count: int = 2
    update_mode: str = "manual"
    last_update_check_utc: str = ""
    checks: list[CheckDefinition] = field(default_factory=list)


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return app_data_dir() / "checks.json"


def log_path() -> Path:
    return app_data_dir() / "beacon.log"


def _reject_sensitive_keys(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise ValueError(
                    f"Secrets are not allowed in check configuration ({path}.{key})"
                )
            _reject_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")


def _reject_secret_like_text(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_secret_like_text(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_like_text(child, f"{path}[{index}]")
    elif isinstance(value, str):
        patterns = (
            r"(?i)accountkey\s*=",
            r"(?i)sharedaccesssignature\s*=",
            r"(?i)client[_-]?secret\s*=",
            r"(?i)[?&]sig=",
            r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.",
        )
        if any(re.search(pattern, value) for pattern in patterns):
            raise ValueError(
                f"Possible secret detected in {path}; it will not be stored or exported"
            )


def _definition_from_dict(raw: dict[str, Any]) -> CheckDefinition:
    unknown = set(raw) - CHECK_KEYS
    if unknown:
        raise ValueError(
            f"Unsupported fields in check definition: {', '.join(sorted(unknown))}"
        )
    expected = raw.get("expected_values", ["Succeeded"])
    if not isinstance(expected, list) or not expected:
        raise ValueError("Each check requires at least one expected value")
    definition = CheckDefinition(
        id=str(raw["id"]),
        name=str(raw["name"]).strip(),
        resource_id=str(raw["resource_id"]).strip(),
        portal_url=str(raw.get("portal_url", "")).strip(),
        tenant_id=str(raw.get("tenant_id", "")).strip(),
        expected_values=[str(item).strip() for item in expected if str(item).strip()],
        enabled=bool(raw.get("enabled", True)),
        kind=str(raw.get("kind", "azure_resource_provisioning")),
    )
    validate_definition(definition)
    return definition


def load_config(path: Path | None = None) -> AppConfig:
    target = path or config_path()
    if not target.exists():
        return AppConfig()
    raw = json.loads(target.read_text(encoding="utf-8"))
    _reject_sensitive_keys(raw)
    loaded_schema = int(raw.get("schema_version", 0))
    if loaded_schema not in (1, 2, SCHEMA_VERSION):
        raise ValueError(
            f"Unsupported configuration schema: {raw.get('schema_version')}"
        )
    interval = int(raw.get("interval_minutes", 5))
    timeout = int(raw.get("timeout_seconds", 30))
    retries = int(raw.get("retry_count", 2))
    update_mode = str(raw.get("update_mode", "manual")).strip()
    if not 1 <= interval <= 1440:
        raise ValueError("interval_minutes must be between 1 and 1440")
    if not 5 <= timeout <= 300:
        raise ValueError("timeout_seconds must be between 5 and 300")
    if not 0 <= retries <= 5:
        raise ValueError("retry_count must be between 0 and 5")
    if update_mode not in {"manual", "notify", "automatic"}:
        raise ValueError("update_mode must be manual, notify, or automatic")
    checks = [_definition_from_dict(item) for item in raw.get("checks", [])]
    return AppConfig(
        schema_version=SCHEMA_VERSION,
        onboarding_completed=bool(raw.get("onboarding_completed", False))
        if loaded_schema >= 2
        else False,
        azure_subscription_id=str(raw.get("azure_subscription_id", "")).strip(),
        azure_subscription_name=str(raw.get("azure_subscription_name", "")).strip(),
        azure_tenant_id=str(raw.get("azure_tenant_id", "")).strip(),
        connection_established_utc=str(
            raw.get("connection_established_utc", "")
        ).strip(),
        connection_purge_pending=bool(raw.get("connection_purge_pending", False)),
        interval_minutes=interval,
        timeout_seconds=timeout,
        retry_count=retries,
        update_mode=update_mode,
        last_update_check_utc=str(raw.get("last_update_check_utc", "")).strip(),
        checks=checks,
    )


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "onboarding_completed": config.onboarding_completed,
        "azure_subscription_id": config.azure_subscription_id,
        "azure_subscription_name": config.azure_subscription_name,
        "azure_tenant_id": config.azure_tenant_id,
        "connection_established_utc": config.connection_established_utc,
        "connection_purge_pending": config.connection_purge_pending,
        "interval_minutes": config.interval_minutes,
        "timeout_seconds": config.timeout_seconds,
        "retry_count": config.retry_count,
        "update_mode": config.update_mode,
        "last_update_check_utc": config.last_update_check_utc,
        "checks": [asdict(check) for check in config.checks],
    }
    _reject_sensitive_keys(payload)
    _reject_secret_like_text(payload)
    # Validate the exact data before replacing the known-good file.
    validation_file = target.with_suffix(".validation.json")
    validation_file.write_text(json.dumps(payload), encoding="utf-8")
    try:
        load_config(validation_file)
    finally:
        validation_file.unlink(missing_ok=True)
    if target.exists():
        shutil.copy2(target, target.with_suffix(".json.bak"))
    handle, temp_name = tempfile.mkstemp(
        prefix="checks-", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def mark_connection_established(config: AppConfig, now: datetime | None = None) -> None:
    timestamp = now or datetime.now(UTC)
    config.connection_established_utc = timestamp.astimezone(UTC).isoformat()


def connection_expires_at(config: AppConfig) -> datetime | None:
    if not config.onboarding_completed or not config.connection_established_utc:
        return None
    try:
        established = datetime.fromisoformat(config.connection_established_utc)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if established.tzinfo is None:
        established = established.replace(tzinfo=UTC)
    return established.astimezone(UTC) + AUTHORIZATION_MAX_AGE


def connection_is_expired(config: AppConfig, now: datetime | None = None) -> bool:
    expires = connection_expires_at(config)
    if expires is None:
        return config.onboarding_completed
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return current >= expires


def clear_connection_metadata(config: AppConfig) -> None:
    config.onboarding_completed = False
    config.azure_subscription_id = ""
    config.azure_subscription_name = ""
    config.azure_tenant_id = ""
    config.connection_established_utc = ""


def validate_definition(definition: CheckDefinition) -> None:
    if definition.kind != "azure_resource_provisioning":
        raise ValueError(f"Unsupported check kind: {definition.kind}")
    if not definition.id or not definition.name:
        raise ValueError("Check ID and name are required")
    if not is_valid_resource_id(definition.resource_id):
        raise ValueError(
            "Enter a complete Azure resource ID or Azure Portal resource URL"
        )
    if not definition.expected_values:
        raise ValueError("At least one expected provisioning state is required")
    if len(definition.name) > 200 or len(definition.resource_id) > 2048:
        raise ValueError("Check name or resource ID is unexpectedly long")
    if len(definition.portal_url) > 4096 or len(definition.tenant_id) > 256:
        raise ValueError("Portal URL or tenant value is unexpectedly long")
    if len(definition.expected_values) > 20 or any(
        len(value) > 100 for value in definition.expected_values
    ):
        raise ValueError("Too many or overly long expected values")
    text_values = [
        definition.name,
        definition.resource_id,
        definition.tenant_id,
        *definition.expected_values,
    ]
    if any(any(ord(character) < 32 for character in value) for value in text_values):
        raise ValueError("Check definitions cannot contain control characters")
    if definition.portal_url:
        parsed = urlparse(definition.portal_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "portal.azure.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Portal links must use HTTPS on an Azure Portal domain")
    _reject_secret_like_text(asdict(definition), "check")


def is_valid_resource_id(value: str) -> bool:
    pattern = re.compile(
        r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/[^/]+/[^/]+/[^/]+(?:/[^/]+/[^/]+)*$",
        re.IGNORECASE,
    )
    return bool(pattern.match(value.strip().rstrip("/")))


def parse_resource_reference(value: str) -> tuple[str, str, str]:
    """Return (resource_id, portal_url, tenant_hint) from an ID or portal URL."""
    raw = unquote(value.strip())
    if raw.startswith("/subscriptions/"):
        resource_id = raw.rstrip("/")
        validate_resource_id_text(resource_id)
        return resource_id, "", ""

    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "portal.azure.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("This is not an Azure Portal URL or Azure resource ID")
    decoded = unquote(parsed.fragment or parsed.path)
    match = re.search(r"(?i)(/subscriptions/.+)", decoded)
    if not match:
        raise ValueError("The Azure Portal URL does not contain a resource ID")
    candidate = match.group(1)
    for marker in (
        "/overview",
        "/properties",
        "/activitylog",
        "/diagnoseand solve problems",
    ):
        index = candidate.casefold().find(marker)
        if index >= 0:
            candidate = candidate[:index]
    candidate = candidate.split("?", 1)[0].rstrip("/")
    validate_resource_id_text(candidate)
    tenant_hint = ""
    tenant_match = re.search(r"#@([^/]+)/resource", raw, re.IGNORECASE)
    if tenant_match:
        tenant_hint = tenant_match.group(1)
    return candidate, raw, tenant_hint


def validate_resource_id_text(resource_id: str) -> None:
    if not is_valid_resource_id(resource_id):
        raise ValueError("Could not identify a complete Azure resource ID")


def export_rule_pack(path: Path, checks: list[CheckDefinition]) -> Path:
    if not checks:
        raise ValueError("There are no rules to export")
    payload = {
        "format": RULE_PACK_FORMAT,
        "schema_version": RULE_PACK_SCHEMA_VERSION,
        "created_utc": datetime.now(UTC).isoformat(),
        "checks": [asdict(check) for check in checks],
    }
    _reject_sensitive_keys(payload, "rule_pack")
    _reject_secret_like_text(payload, "rule_pack")
    for check in checks:
        validate_definition(check)
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_RULE_PACK_BYTES:
        raise ValueError("Rule pack is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix="rules-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def import_rule_pack(path: Path) -> list[CheckDefinition]:
    size = path.stat().st_size
    if size > MAX_RULE_PACK_BYTES:
        raise ValueError("Rule pack exceeds the 1 MB safety limit")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("Rule pack must contain one JSON object")
    allowed_pack_keys = {"format", "schema_version", "created_utc", "checks"}
    unknown = set(raw) - allowed_pack_keys
    if unknown:
        raise ValueError(f"Unsupported rule-pack fields: {', '.join(sorted(unknown))}")
    _reject_sensitive_keys(raw, "rule_pack")
    _reject_secret_like_text(raw, "rule_pack")
    if raw.get("format") != RULE_PACK_FORMAT:
        raise ValueError("This is not an Azure Health Beacon rule pack")
    if int(raw.get("schema_version", 0)) != RULE_PACK_SCHEMA_VERSION:
        raise ValueError("Unsupported rule-pack schema")
    items = raw.get("checks")
    if not isinstance(items, list) or not items:
        raise ValueError("Rule pack contains no checks")
    if len(items) > MAX_RULES_PER_PACK:
        raise ValueError(
            f"Rule pack exceeds the {MAX_RULES_PER_PACK}-rule safety limit"
        )
    checks = [_definition_from_dict(item) for item in items]
    ids = [check.id for check in checks]
    if len(ids) != len(set(ids)):
        raise ValueError("Rule pack contains duplicate check IDs")
    # Imported rules are inert until the user reviews, tests, and enables them.
    for check in checks:
        check.enabled = False
    return checks
