from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from azure_health_beacon.azure import (
    AzureSubscription,
    _run_az,
    delete_isolated_azure_state,
    interactive_login,
    list_subscriptions,
    run_resource_graph_check,
    validate_subscription_access,
)
from azure_health_beacon.config import (
    AppConfig,
    clear_connection_metadata,
    connection_is_expired,
    export_rule_pack,
    import_rule_pack,
    load_config,
    mark_connection_established,
    parse_resource_reference,
    save_config,
)
from azure_health_beacon.model import (
    BeaconState,
    CheckDefinition,
    CheckResult,
    CheckState,
    aggregate_state,
)
from azure_health_beacon.updater import (
    download_verified_installer,
    is_newer_version,
    launch_installer,
    parse_release_payload,
)
from azure_health_beacon.windows_startup import startup_command

RESOURCE_ID = (
    "/subscriptions/11111111-1111-1111-1111-111111111111/"
    "resourceGroups/rg-orion/providers/Microsoft.Network/azureFirewalls/orion-fw"
)


class StateTests(unittest.TestCase):
    def result(self, state: CheckState) -> CheckResult:
        return CheckResult(
            "1", "Orion firewall", state, "test", checked_at=datetime.now().astimezone()
        )

    def test_confirmed_failure_wins_during_check(self) -> None:
        self.assertEqual(
            aggregate_state([self.result(CheckState.FAILED)], checking=True),
            BeaconState.FAILED,
        )

    def test_unconnectable_is_not_failure(self) -> None:
        self.assertEqual(
            aggregate_state([self.result(CheckState.UNCONNECTABLE)]),
            BeaconState.UNCONNECTABLE,
        )

    def test_all_healthy_is_green(self) -> None:
        self.assertEqual(
            aggregate_state([self.result(CheckState.HEALTHY)]), BeaconState.HEALTHY
        )


class ConfigurationTests(unittest.TestCase):
    def test_portal_url_is_parsed(self) -> None:
        url = f"https://portal.azure.com/#@example.com/resource{RESOURCE_ID}/overview"
        resource_id, portal_url, tenant = parse_resource_reference(url)
        self.assertEqual(resource_id, RESOURCE_ID)
        self.assertEqual(portal_url, url)
        self.assertEqual(tenant, "example.com")

    def test_lookalike_portal_domain_is_rejected(self) -> None:
        url = f"https://portal.azure.com.attacker.example/#/resource{RESOURCE_ID}/overview"
        with self.assertRaisesRegex(ValueError, "not an Azure Portal"):
            parse_resource_reference(url)

    def test_save_load_and_backup(self) -> None:
        definition = CheckDefinition("one", "Orion firewall", RESOURCE_ID)
        config = AppConfig(
            onboarding_completed=True,
            azure_subscription_id="11111111-1111-1111-1111-111111111111",
            azure_subscription_name="Orion",
            azure_tenant_id="22222222-2222-2222-2222-222222222222",
            checks=[definition],
        )
        mark_connection_established(config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checks.json"
            save_config(config, path)
            save_config(config, path)
            loaded = load_config(path)
            self.assertEqual(
                loaded.checks[0].subscription_id, "11111111-1111-1111-1111-111111111111"
            )
            self.assertTrue(loaded.onboarding_completed)
            self.assertEqual(loaded.azure_subscription_name, "Orion")
            self.assertTrue(path.with_suffix(".json.bak").exists())

    def test_version_one_config_migrates_to_uninitialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checks.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "interval_minutes": 5,
                        "timeout_seconds": 30,
                        "retry_count": 2,
                        "checks": [],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_config(path)
            self.assertEqual(loaded.schema_version, 4)
            self.assertFalse(loaded.onboarding_completed)
            self.assertEqual(loaded.update_mode, "manual")
            self.assertFalse(loaded.start_with_windows)
            self.assertFalse(loaded.start_minimized)

    def test_version_three_config_adds_opt_in_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checks.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "onboarding_completed": False,
                        "interval_minutes": 5,
                        "timeout_seconds": 30,
                        "retry_count": 2,
                        "checks": [],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_config(path)
            self.assertEqual(loaded.schema_version, 4)
            self.assertFalse(loaded.start_with_windows)
            self.assertFalse(loaded.start_minimized)

    def test_connection_hard_expires_at_fourteen_days(self) -> None:
        started = datetime(2026, 8, 1, tzinfo=UTC)
        config = AppConfig(onboarding_completed=True)
        mark_connection_established(config, started)
        self.assertFalse(
            connection_is_expired(config, started + timedelta(days=13, hours=23))
        )
        self.assertTrue(connection_is_expired(config, started + timedelta(days=14)))

    def test_clear_connection_keeps_rules_but_removes_scope(self) -> None:
        definition = CheckDefinition("one", "Orion firewall", RESOURCE_ID)
        config = AppConfig(
            onboarding_completed=True,
            azure_subscription_id="11111111-1111-1111-1111-111111111111",
            azure_subscription_name="Orion",
            azure_tenant_id="22222222-2222-2222-2222-222222222222",
            checks=[definition],
        )
        mark_connection_established(config)
        clear_connection_metadata(config)
        self.assertFalse(config.onboarding_completed)
        self.assertFalse(config.azure_subscription_id)
        self.assertEqual(len(config.checks), 1)

    def test_secret_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checks.json"
            path.write_text(
                json.dumps({"schema_version": 1, "access_token": "nope"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Secrets are not allowed"):
                load_config(path)

    def test_exported_rule_pack_has_no_credentials_and_imports_disabled(self) -> None:
        definition = CheckDefinition("one", "Orion firewall", RESOURCE_ID)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.ahbrules.json"
            export_rule_pack(path, [definition])
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("access_token", text)
            imported = import_rule_pack(path)
            self.assertEqual(imported[0].resource_id, RESOURCE_ID)
            self.assertFalse(imported[0].enabled)

    def test_import_rejects_arbitrary_command_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malicious.ahbrules.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "azure-health-beacon-rule-pack",
                        "schema_version": 1,
                        "checks": [
                            {
                                "id": "one",
                                "name": "bad",
                                "resource_id": RESOURCE_ID,
                                "command": "whoami",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unsupported fields"):
                import_rule_pack(path)

    def test_graph_query_exports_without_credentials_and_imports_disabled(self) -> None:
        definition = CheckDefinition(
            "graph-one",
            "Fired alerts",
            "",
            expected_values=[],
            kind="azure_resource_graph",
            query="Resources | where name == 'orion' | project name, id",
            scope="all_accessible",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.ahbrules.json"
            export_rule_pack(path, [definition])
            imported = import_rule_pack(path)
            self.assertEqual(imported[0].query, definition.query)
            self.assertFalse(imported[0].enabled)
            self.assertNotIn("token", path.read_text(encoding="utf-8").casefold())

    def test_graph_query_rejects_secret_like_text(self) -> None:
        definition = CheckDefinition(
            "graph-one",
            "Bad query",
            "",
            expected_values=[],
            kind="azure_resource_graph",
            query="Resources | where note == 'client_secret=do-not-store-this'",
            scope="all_accessible",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Possible secret"):
                export_rule_pack(Path(directory) / "bad.json", [definition])


class AzureAuthenticationTests(unittest.TestCase):
    @patch("azure_health_beacon.azure.subprocess.run")
    @patch("azure_health_beacon.azure._azure_cli_command")
    @patch("azure_health_beacon.azure.app_data_dir")
    def test_every_cli_call_uses_the_isolated_profile(
        self, data_dir, cli_command, run
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data_dir.return_value = base
            cli_command.return_value = ["trusted-az.exe"]
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_az(["version"], 10)
            environment = run.call_args.kwargs["env"]
            self.assertEqual(Path(environment["AZURE_CONFIG_DIR"]), base / "azure-cli")
            self.assertNotEqual(
                environment["AZURE_CONFIG_DIR"], str(Path.home() / ".azure")
            )
            self.assertEqual(run.call_args.args[0], ["trusted-az.exe", "version"])

    @patch("azure_health_beacon.azure.app_data_dir")
    def test_authentication_purge_only_deletes_isolated_profile(self, data_dir) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data_dir.return_value = base
            isolated = base / "azure-cli"
            isolated.mkdir()
            (isolated / "msal_token_cache.bin").write_bytes(b"encrypted-placeholder")
            sibling = base / "checks.json"
            sibling.write_text("rules remain", encoding="utf-8")
            delete_isolated_azure_state()
            self.assertFalse(isolated.exists())
            self.assertTrue(sibling.exists())

    @patch("azure_health_beacon.azure._run_az")
    def test_interactive_login_never_passes_a_password_or_requests_a_token(
        self, run_az
    ) -> None:
        run_az.return_value = subprocess.CompletedProcess([], 0, "", "")
        success, _ = interactive_login("22222222-2222-2222-2222-222222222222")
        self.assertTrue(success)
        arguments = run_az.call_args.args[0]
        joined = " ".join(arguments).casefold()
        self.assertIn("login", arguments)
        self.assertNotIn("password", joined)
        self.assertNotIn("get-access-token", joined)

    @patch("azure_health_beacon.azure._run_az")
    def test_subscription_listing_returns_metadata_only(self, run_az) -> None:
        run_az.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "name": "Orion",
                        "tenantId": "22222222-2222-2222-2222-222222222222",
                    }
                ]
            ),
            "",
        )
        subscriptions, error = list_subscriptions()
        self.assertFalse(error)
        self.assertEqual(subscriptions[0].name, "Orion")

    @patch("azure_health_beacon.azure._run_az")
    def test_access_validation_is_read_only_and_subscription_scoped(
        self, run_az
    ) -> None:
        run_az.return_value = subprocess.CompletedProcess([], 0, "12\n", "")
        subscription = AzureSubscription(
            "11111111-1111-1111-1111-111111111111",
            "Orion",
            "22222222-2222-2222-2222-222222222222",
        )
        success, message = validate_subscription_access(subscription)
        self.assertTrue(success)
        self.assertIn("12 resource groups", message)
        arguments = run_az.call_args.args[0]
        self.assertEqual(arguments[:2], ["group", "list"])
        self.assertIn(subscription.id, arguments)


class ResourceGraphTests(unittest.TestCase):
    def definition(self) -> CheckDefinition:
        return CheckDefinition(
            "graph-one",
            "Fired alerts",
            "",
            expected_values=[],
            kind="azure_resource_graph",
            query="Resources | project name, id",
            scope="all_accessible",
        )

    @patch("azure_health_beacon.azure.app_data_dir")
    @patch("azure_health_beacon.azure._run_az")
    def test_matching_rows_are_confirmed_failures_across_all_subscriptions(
        self, run_az, data_dir
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir.return_value = Path(directory)
            subscriptions = [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "Orion",
                    "tenantId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "name": "Shared",
                    "tenantId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
            ]
            run_az.side_effect = [
                subprocess.CompletedProcess([], 0, json.dumps(subscriptions), ""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {
                            "totalRecords": 1,
                            "data": [
                                {
                                    "name": "orion-fw",
                                    "id": RESOURCE_ID,
                                    "state": "Failed",
                                }
                            ],
                        }
                    ),
                    "",
                ),
            ]
            result = run_resource_graph_check(self.definition(), retry_count=0)
            self.assertEqual(result.state, CheckState.FAILED)
            self.assertIn("2 accessible subscriptions", result.summary)
            self.assertEqual(result.findings[0].title, "orion-fw")
            rest_arguments = run_az.call_args_list[1].args[0]
            self.assertEqual(rest_arguments[0], "rest")
            self.assertIn("--subscription", rest_arguments)

    @patch("azure_health_beacon.azure.app_data_dir")
    @patch("azure_health_beacon.azure._run_az")
    def test_zero_rows_is_healthy(self, run_az, data_dir) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir.return_value = Path(directory)
            run_az.side_effect = [
                subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        [
                            {
                                "id": "11111111-1111-1111-1111-111111111111",
                                "name": "Orion",
                                "tenantId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                            }
                        ]
                    ),
                    "",
                ),
                subprocess.CompletedProcess(
                    [], 0, json.dumps({"totalRecords": 0, "data": []}), ""
                ),
            ]
            result = run_resource_graph_check(self.definition(), retry_count=0)
            self.assertEqual(result.state, CheckState.HEALTHY)

    @patch("azure_health_beacon.azure.app_data_dir")
    @patch("azure_health_beacon.azure._run_az")
    def test_query_error_is_grey_not_red(self, run_az, data_dir) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir.return_value = Path(directory)
            run_az.side_effect = [
                subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        [
                            {
                                "id": "11111111-1111-1111-1111-111111111111",
                                "name": "Orion",
                                "tenantId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                            }
                        ]
                    ),
                    "",
                ),
                subprocess.CompletedProcess([], 1, "", "BadRequest: invalid query"),
            ]
            result = run_resource_graph_check(self.definition(), retry_count=0)
            self.assertEqual(result.state, CheckState.UNCONNECTABLE)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str) -> None:
        super().__init__(data)
        self.headers: dict[str, str] = {"Content-Length": str(len(data))}
        self.url = url

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class UpdateTests(unittest.TestCase):
    def release_payload(self) -> dict[str, object]:
        tag = "v0.3.0"
        installer = f"AzureHealthBeacon-Setup-{tag}.exe"
        base = (
            "https://github.com/Anders0lesen/azure-extensible-systray-monitor/"
            f"releases/download/{tag}"
        )
        return {
            "draft": False,
            "prerelease": False,
            "tag_name": tag,
            "name": "Azure Health Beacon 0.3.0",
            "body": "Updater release",
            "html_url": (
                "https://github.com/Anders0lesen/azure-extensible-systray-monitor/"
                f"releases/tag/{tag}"
            ),
            "assets": [
                {
                    "name": installer,
                    "browser_download_url": f"{base}/{installer}",
                    "digest": (
                        "sha256:" + hashlib.sha256(b"test installer bytes").hexdigest()
                    ),
                },
                {
                    "name": f"{installer}.sha256",
                    "browser_download_url": f"{base}/{installer}.sha256",
                },
            ],
        }

    def test_semantic_version_comparison(self) -> None:
        self.assertTrue(is_newer_version("0.3.0", "0.2.0"))
        self.assertFalse(is_newer_version("0.2.0", "0.2.0"))
        self.assertFalse(is_newer_version("0.1.9", "0.2.0"))

    def test_release_parser_pins_repo_and_exact_asset_names(self) -> None:
        release = parse_release_payload(self.release_payload())
        self.assertEqual(release.version, "0.3.0")
        payload = self.release_payload()
        assets = payload["assets"]
        assert isinstance(assets, list)
        assert isinstance(assets[0], dict)
        assets[0]["browser_download_url"] = (
            "https://github.com/attacker/repo/releases/download/v0.3.0/"
            "AzureHealthBeacon-Setup-v0.3.0.exe"
        )
        with self.assertRaisesRegex(ValueError, "unexpected update download URL"):
            parse_release_payload(payload)

    @patch("azure_health_beacon.updater._request")
    def test_installer_download_requires_matching_sha256(self, request) -> None:
        payload = b"test installer bytes"
        digest = hashlib.sha256(payload).hexdigest()
        release = parse_release_payload(self.release_payload())
        request.side_effect = [
            FakeResponse(
                f"{digest} *{release.installer_name}\n".encode("ascii"),
                release.checksum_url,
            ),
            FakeResponse(payload, release.installer_url),
        ]
        with tempfile.TemporaryDirectory() as directory:
            installer = download_verified_installer(release, Path(directory))
            self.assertEqual(installer.read_bytes(), payload)

    @patch("azure_health_beacon.updater._request")
    def test_installer_download_rejects_checksum_mismatch(self, request) -> None:
        release = parse_release_payload(self.release_payload())
        request.side_effect = [
            FakeResponse(
                f"{release.installer_digest} *{release.installer_name}\n".encode(
                    "ascii"
                ),
                release.checksum_url,
            ),
            FakeResponse(b"not the approved installer", release.installer_url),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "failed SHA-256 verification"):
                download_verified_installer(release, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    @patch("azure_health_beacon.updater.subprocess.Popen")
    def test_approved_manual_update_is_silent_and_restarts(self, popen) -> None:
        installer = Path(r"C:\Temp\AzureHealthBeacon-Setup-v0.4.0.exe")
        launch_installer(installer, automatic=False)
        arguments = popen.call_args.args[0]
        self.assertIn("/VERYSILENT", arguments)
        self.assertIn("/RESTARTAPPLICATIONS", arguments)


class WindowsStartupTests(unittest.TestCase):
    def test_startup_command_quotes_executable_and_marks_startup_launch(self) -> None:
        command = startup_command(r"C:\Program Files\Azure Health Beacon\Beacon.exe")
        self.assertIn('"C:\\Program Files\\Azure Health Beacon\\Beacon.exe"', command)
        self.assertTrue(command.endswith("--startup"))


if __name__ == "__main__":
    unittest.main()
