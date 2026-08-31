from __future__ import annotations

import hashlib
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import __version__
from .azure import (
    AzureSubscription,
    delete_isolated_azure_state,
    discover_metric_definitions,
    discover_resources,
    discover_workspace_tables,
    discover_workspaces,
    interactive_login,
    list_subscriptions,
    redact_error,
    run_check,
    validate_subscription_access,
)
from .config import (
    AppConfig,
    app_data_dir,
    clear_connection_metadata,
    connection_expires_at,
    connection_is_expired,
    definition_from_dict,
    export_rule_pack,
    import_rule_pack,
    load_config,
    log_path,
    mark_connection_established,
    save_config,
)
from .model import CheckDefinition, CheckResult, CheckState
from .signal_sources import SIGNAL_SOURCES
from .updater import download_verified_installer, fetch_latest_release, is_newer_version

LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    target = log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        target, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _fingerprint(definition: CheckDefinition) -> str:
    encoded = json.dumps(
        asdict(definition), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rule_payload(definition: CheckDefinition) -> dict[str, object]:
    return asdict(definition)


def _result_payload(result: CheckResult) -> dict[str, object]:
    return {
        "check_id": result.check_id,
        "name": result.name,
        "state": result.state.value,
        "summary": result.summary,
        "observed_value": result.observed_value,
        "checked_at": result.checked_at.isoformat(),
        "portal_url": result.portal_url,
        "findings": [asdict(item) for item in result.findings],
    }


def _subscription_payload(subscription: AzureSubscription) -> dict[str, str]:
    return asdict(subscription)


def _connection_snapshot(config: AppConfig) -> dict[str, object]:
    expires = connection_expires_at(config)
    return {
        "initialized": config.onboarding_completed,
        "subscription_id": config.azure_subscription_id,
        "subscription_name": config.azure_subscription_name,
        "tenant_id": config.azure_tenant_id,
        "expires_at": expires.isoformat() if expires else "",
    }


def _settings_snapshot(config: AppConfig) -> dict[str, object]:
    return {
        "interval_minutes": config.interval_minutes,
        "timeout_seconds": config.timeout_seconds,
        "retry_count": config.retry_count,
        "update_mode": config.update_mode,
        "last_update_check_utc": config.last_update_check_utc,
        "start_with_windows": config.start_with_windows,
        "start_minimized": config.start_minimized,
        "theme_mode": config.theme_mode,
    }


def _purge_expired_connection(config: AppConfig) -> AppConfig:
    if not config.connection_purge_pending and not connection_is_expired(config):
        return config
    config.connection_purge_pending = True
    clear_connection_metadata(config)
    save_config(config)
    delete_isolated_azure_state()
    config.connection_purge_pending = False
    save_config(config)
    return config


class Bridge:
    """Data-only command boundary between the Windows shell and Python engine."""

    def __init__(self) -> None:
        self.tested_fingerprints: set[str] = set()

    def dispatch(self, command: str, payload: dict[str, Any]) -> object:
        handlers = {
            "ping": self.ping,
            "snapshot": self.snapshot,
            "login": self.login,
            "list_subscriptions": self.subscriptions,
            "complete_setup": self.complete_setup,
            "delete_connection": self.delete_connection,
            "update_settings": self.update_settings,
            "check_all": self.check_all,
            "test_rule": self.test_rule,
            "save_rule": self.save_rule,
            "delete_rule": self.delete_rule,
            "discover_resources": self.resources,
            "discover_workspaces": self.workspaces,
            "discover_workspace_tables": self.workspace_tables,
            "discover_metrics": self.metrics,
            "import_rules": self.import_rules,
            "export_rules": self.export_rules,
            "check_update": self.check_update,
            "prepare_update": self.prepare_update,
        }
        if command not in handlers:
            raise ValueError("Unsupported application command")
        return handlers[command](payload)

    @staticmethod
    def ping(_payload: dict[str, Any]) -> dict[str, str]:
        return {"version": __version__}

    @staticmethod
    def snapshot(_payload: dict[str, Any]) -> dict[str, object]:
        config = _purge_expired_connection(load_config())
        return {
            "version": __version__,
            "connection": _connection_snapshot(config),
            "settings": _settings_snapshot(config),
            "rules": [_rule_payload(item) for item in config.checks],
            "sources": [asdict(item) for item in SIGNAL_SOURCES],
        }

    @staticmethod
    def login(payload: dict[str, Any]) -> dict[str, object]:
        success, message = interactive_login(str(payload.get("tenant_hint", "")))
        return {"success": success, "message": message}

    @staticmethod
    def subscriptions(_payload: dict[str, Any]) -> dict[str, object]:
        subscriptions, error = list_subscriptions()
        return {
            "subscriptions": [_subscription_payload(item) for item in subscriptions],
            "error": error,
        }

    @staticmethod
    def complete_setup(payload: dict[str, Any]) -> dict[str, object]:
        subscription = AzureSubscription(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            tenant_id=str(payload.get("tenant_id", "")),
        )
        success, message = validate_subscription_access(subscription)
        if not success:
            return {"success": False, "message": message}
        config = load_config()
        config.azure_subscription_id = subscription.id
        config.azure_subscription_name = subscription.name
        config.azure_tenant_id = subscription.tenant_id
        config.onboarding_completed = True
        config.connection_purge_pending = False
        mark_connection_established(config)
        save_config(config)
        return {"success": True, "message": message}

    @staticmethod
    def delete_connection(_payload: dict[str, Any]) -> dict[str, object]:
        config = load_config()
        config.connection_purge_pending = True
        clear_connection_metadata(config)
        save_config(config)
        delete_isolated_azure_state()
        config.connection_purge_pending = False
        save_config(config)
        return {"success": True}

    @staticmethod
    def update_settings(payload: dict[str, Any]) -> dict[str, object]:
        config = load_config()
        interval = int(payload.get("interval_minutes", config.interval_minutes))
        timeout = int(payload.get("timeout_seconds", config.timeout_seconds))
        retries = int(payload.get("retry_count", config.retry_count))
        update_mode = str(payload.get("update_mode", config.update_mode))
        theme = str(payload.get("theme_mode", config.theme_mode))
        if not 1 <= interval <= 1440:
            raise ValueError("Check interval must be between 1 and 1440 minutes")
        if not 5 <= timeout <= 300:
            raise ValueError("Timeout must be between 5 and 300 seconds")
        if not 0 <= retries <= 5:
            raise ValueError("Retry count must be between 0 and 5")
        if update_mode not in {"manual", "notify", "automatic"}:
            raise ValueError("Unsupported update mode")
        if theme not in {"dark", "light"}:
            raise ValueError("Unsupported theme")
        config.interval_minutes = interval
        config.timeout_seconds = timeout
        config.retry_count = retries
        config.update_mode = update_mode
        config.start_with_windows = bool(
            payload.get("start_with_windows", config.start_with_windows)
        )
        config.start_minimized = bool(
            payload.get("start_minimized", config.start_minimized)
        )
        config.theme_mode = theme
        save_config(config)
        return _settings_snapshot(config)

    @staticmethod
    def check_all(_payload: dict[str, Any]) -> dict[str, object]:
        config = _purge_expired_connection(load_config())
        if not config.onboarding_completed:
            return {"state": "unconnectable", "results": []}
        enabled = [item for item in config.checks if item.enabled]
        results: list[CheckResult] = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(enabled)))) as pool:
            futures = {
                pool.submit(
                    run_check,
                    item,
                    timeout_seconds=config.timeout_seconds,
                    retry_count=config.retry_count,
                ): item
                for item in enabled
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as error:
                    item = futures[future]
                    LOGGER.exception("Check failed unexpectedly: %s", item.id)
                    results.append(
                        CheckResult(
                            item.id,
                            item.name,
                            CheckState.UNCONNECTABLE,
                            redact_error(str(error))[:500],
                        )
                    )
        if any(item.state is CheckState.FAILED for item in results):
            state = "failed"
        elif not results or any(
            item.state is CheckState.UNCONNECTABLE for item in results
        ):
            state = "unconnectable"
        else:
            state = "healthy"
        return {"state": state, "results": [_result_payload(item) for item in results]}

    def test_rule(self, payload: dict[str, Any]) -> dict[str, object]:
        definition = definition_from_dict(dict(payload.get("rule", {})))
        config = _purge_expired_connection(load_config())
        if not config.onboarding_completed:
            raise ValueError("Set up the Azure connection before testing rules")
        result = run_check(
            definition,
            timeout_seconds=config.timeout_seconds,
            retry_count=config.retry_count,
        )
        if result.state is not CheckState.UNCONNECTABLE:
            self.tested_fingerprints.add(_fingerprint(definition))
        return _result_payload(result)

    def save_rule(self, payload: dict[str, Any]) -> dict[str, object]:
        definition = definition_from_dict(dict(payload.get("rule", {})))
        fingerprint = _fingerprint(definition)
        if fingerprint not in self.tested_fingerprints:
            raise ValueError("Test the current rule successfully before saving it")
        config = load_config()
        existing = next(
            (
                index
                for index, item in enumerate(config.checks)
                if item.id == definition.id
            ),
            None,
        )
        if existing is None:
            config.checks.append(definition)
        else:
            config.checks[existing] = definition
        save_config(config)
        self.tested_fingerprints.discard(fingerprint)
        return {"rule": _rule_payload(definition)}

    @staticmethod
    def delete_rule(payload: dict[str, Any]) -> dict[str, object]:
        rule_id = str(payload.get("id", ""))
        config = load_config()
        original = len(config.checks)
        config.checks = [item for item in config.checks if item.id != rule_id]
        if len(config.checks) == original:
            raise ValueError("The selected rule no longer exists")
        save_config(config)
        return {"deleted": rule_id}

    @staticmethod
    def resources(_payload: dict[str, Any]) -> dict[str, object]:
        resources, errors = discover_resources()
        return {"resources": [asdict(item) for item in resources], "errors": errors}

    @staticmethod
    def workspaces(_payload: dict[str, Any]) -> dict[str, object]:
        workspaces, errors = discover_workspaces()
        return {"workspaces": [asdict(item) for item in workspaces], "errors": errors}

    @staticmethod
    def workspace_tables(payload: dict[str, Any]) -> dict[str, object]:
        raw = dict(payload.get("workspace", {}))
        workspaces, errors = discover_workspaces()
        workspace = next(
            (
                item
                for item in workspaces
                if item.customer_id == str(raw.get("customer_id", ""))
            ),
            None,
        )
        if workspace is None:
            raise ValueError(errors[0] if errors else "Workspace is not accessible")
        tables, error = discover_workspace_tables(workspace)
        return {"tables": tables, "error": error}

    @staticmethod
    def metrics(payload: dict[str, Any]) -> dict[str, object]:
        metrics, error = discover_metric_definitions(
            str(payload.get("resource_id", ""))
        )
        return {"metrics": [asdict(item) for item in metrics], "error": error}

    @staticmethod
    def import_rules(payload: dict[str, Any]) -> dict[str, object]:
        imported = import_rule_pack(Path(str(payload.get("path", ""))))
        config = load_config()
        known = {item.id for item in config.checks}
        for item in imported:
            if item.id in known:
                raise ValueError(f"A rule with ID {item.id} already exists")
            config.checks.append(item)
        save_config(config)
        return {
            "count": len(imported),
            "rules": [_rule_payload(item) for item in imported],
        }

    @staticmethod
    def export_rules(payload: dict[str, Any]) -> dict[str, object]:
        config = load_config()
        target = export_rule_pack(Path(str(payload.get("path", ""))), config.checks)
        return {"path": str(target)}

    @staticmethod
    def check_update(_payload: dict[str, Any]) -> dict[str, object]:
        release = fetch_latest_release()
        config = load_config()
        config.last_update_check_utc = datetime.now(UTC).isoformat()
        save_config(config)
        return {
            "version": release.version,
            "html_url": release.page_url,
            "installer_name": release.installer_name,
            "is_newer": is_newer_version(release.version, __version__),
        }

    @staticmethod
    def prepare_update(_payload: dict[str, Any]) -> dict[str, object]:
        release = fetch_latest_release()
        if not is_newer_version(release.version, __version__):
            raise ValueError("Azure Health Beacon is already fully up to date")
        target = download_verified_installer(release)
        return {"path": str(target), "version": release.version}


def main() -> int:
    _configure_logging()
    app_data_dir().mkdir(parents=True, exist_ok=True)
    bridge = Bridge()
    for line in sys.stdin:
        request_id: object = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError("Application request must be a JSON object")
            request_id = request.get("id")
            command = str(request.get("command", ""))
            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                raise TypeError("Application payload must be a JSON object")
            if command == "shutdown":
                response = {"id": request_id, "ok": True, "result": {}}
                print(json.dumps(response), flush=True)
                return 0
            result = bridge.dispatch(command, payload)
            response = {"id": request_id, "ok": True, "result": result}
        except Exception as error:
            LOGGER.exception("Application command failed")
            response = {
                "id": request_id,
                "ok": False,
                "error": " ".join(redact_error(str(error)).split())[:500],
            }
        print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
