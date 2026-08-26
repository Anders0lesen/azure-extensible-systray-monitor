from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import app_data_dir
from .model import CheckDefinition, CheckResult, CheckState

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(frozen=True, slots=True)
class AzureSubscription:
    id: str
    name: str
    tenant_id: str


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
