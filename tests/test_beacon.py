from __future__ import annotations

import hashlib
import inspect
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import azure_health_beacon.azure as azure_module
import azure_health_beacon.azure_rest as azure_rest_module
from azure_health_beacon.azure import (
    AzureSubscription,
    delete_isolated_azure_state,
    interactive_login,
    list_subscriptions,
    run_log_analytics_check,
    run_metric_check,
    run_resource_graph_check,
    run_resource_property_check,
    run_vm_power_state_check,
    validate_subscription_access,
)
from azure_health_beacon.bridge import Bridge
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
    validate_definition,
)
from azure_health_beacon.identity import (
    _encrypted_persistence,
    account_state_path,
    identity_state_available,
    interactive_sign_in,
    token_cache_path,
    verify_encrypted_storage,
)
from azure_health_beacon.model import (
    BeaconState,
    CheckDefinition,
    CheckResult,
    CheckState,
    aggregate_state,
)
from azure_health_beacon.updater import (
    ReleaseInfo,
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
VM_RESOURCE_ID = (
    "/subscriptions/11111111-1111-1111-1111-111111111111/"
    "resourceGroups/rg-orion/providers/Microsoft.Compute/virtualMachines/orion-vm"
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


class BridgeTests(unittest.TestCase):
    def rule_payload(self) -> dict[str, object]:
        return {
            "id": "rule-one",
            "name": "VM should be running",
            "resource_id": VM_RESOURCE_ID,
            "kind": "azure_vm_power_state",
            "expected_values": ["PowerState/running"],
        }

    def test_unsupported_command_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported application command"):
            Bridge().dispatch("run_arbitrary_code", {})

    @patch("azure_health_beacon.bridge.load_config")
    def test_rule_must_be_tested_before_save(self, load: object) -> None:
        load.return_value = AppConfig(onboarding_completed=True)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ValueError, "Test the current rule"):
            Bridge().save_rule({"rule": self.rule_payload()})

    @patch("azure_health_beacon.bridge.save_config")
    @patch("azure_health_beacon.bridge.run_check")
    @patch("azure_health_beacon.bridge.identity_state_available", return_value=True)
    @patch("azure_health_beacon.bridge.load_config")
    def test_successful_test_issues_one_save_receipt(
        self, load: object, _identity: object, run: object, save: object
    ) -> None:
        config = AppConfig(onboarding_completed=True)
        mark_connection_established(config)
        load.return_value = config  # type: ignore[attr-defined]
        run.return_value = CheckResult(  # type: ignore[attr-defined]
            "rule-one",
            "VM should be running",
            CheckState.HEALTHY,
            "VM power state is PowerState/running.",
        )
        bridge = Bridge()
        tested = bridge.test_rule({"rule": self.rule_payload()})
        self.assertEqual(tested["state"], "healthy")
        bridge.save_rule({"rule": self.rule_payload()})
        self.assertEqual(config.checks[0].id, "rule-one")
        save.assert_called_once_with(config)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ValueError, "Test the current rule"):
            bridge.save_rule({"rule": self.rule_payload()})

    @patch("azure_health_beacon.bridge.identity_state_available", return_value=True)
    @patch("azure_health_beacon.bridge.load_config")
    def test_snapshot_contains_no_credential_material(
        self, load: object, _identity: object
    ) -> None:
        load.return_value = AppConfig(  # type: ignore[attr-defined]
            onboarding_completed=True,
            azure_subscription_id="subscription",
            azure_subscription_name="Example",
            azure_tenant_id="tenant",
        )
        encoded = json.dumps(Bridge.snapshot({})).casefold()
        for forbidden in ("password", "access_token", "refresh_token", "credential"):
            self.assertNotIn(forbidden, encoded)

    @patch("azure_health_beacon.bridge.save_config")
    @patch("azure_health_beacon.bridge.load_config")
    @patch("azure_health_beacon.bridge.fetch_latest_release")
    def test_update_check_records_when_the_network_was_queried(
        self, fetch: object, load: object, save: object
    ) -> None:
        release = ReleaseInfo(
            version="9.9.9",
            tag="v9.9.9",
            title="Test",
            notes="",
            page_url="https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v9.9.9",
            installer_name="AzureHealthBeacon-Setup-v9.9.9.exe",
            installer_url="https://github.com/example.exe",
            installer_digest="a" * 64,
            checksum_name="AzureHealthBeacon-Setup-v9.9.9.exe.sha256",
            checksum_url="https://github.com/example.sha256",
        )
        config = AppConfig()
        fetch.return_value = release  # type: ignore[attr-defined]
        load.return_value = config  # type: ignore[attr-defined]
        result = Bridge.check_update({})
        self.assertTrue(result["is_newer"])
        self.assertTrue(config.last_update_check_utc)
        save.assert_called_once_with(config)  # type: ignore[attr-defined]


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
            self.assertEqual(loaded.schema_version, 6)
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
            self.assertEqual(loaded.schema_version, 6)
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

    def test_generic_property_rule_rejects_command_like_property_path(self) -> None:
        definition = CheckDefinition(
            "property-one",
            "Unsafe property",
            RESOURCE_ID,
            kind="azure_resource_property",
            expected_values=["Succeeded"],
            property_path="properties.state; whoami",
        )
        with self.assertRaisesRegex(ValueError, "property path"):
            validate_definition(definition)

    def test_vm_power_rule_requires_a_vm_resource(self) -> None:
        definition = CheckDefinition(
            "vm-one",
            "Not a VM",
            RESOURCE_ID,
            kind="azure_vm_power_state",
            expected_values=["PowerState/running"],
        )
        with self.assertRaisesRegex(ValueError, "virtual-machine"):
            validate_definition(definition)


class AzureAuthenticationTests(unittest.TestCase):
    def test_runtime_has_no_azure_cli_execution_path(self) -> None:
        source = inspect.getsource(azure_module) + inspect.getsource(azure_rest_module)
        for forbidden in (
            "subprocess.run(",
            "subprocess.Popen(",
            "shutil.which(",
            "AZURE_CONFIG_DIR",
        ):
            self.assertNotIn(forbidden, source)

    @patch("azure_health_beacon.identity.app_data_dir")
    def test_encrypted_cache_and_lock_self_test_leaves_no_probe(
        self, data_dir: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data_dir.return_value = base
            self.assertEqual(
                verify_encrypted_storage(), "windows-dpapi-current-user"
            )
            self.assertFalse((base / "identity").exists())

    @patch("azure_health_beacon.identity.msal.PublicClientApplication")
    @patch("azure_health_beacon.identity.app_data_dir")
    def test_interactive_login_persists_only_dpapi_ciphertext(
        self, data_dir, application
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data_dir.return_value = base
            def authorize(**_kwargs):
                _encrypted_persistence(token_cache_path()).save(
                    '{"AccessToken":"plaintext-access-token-must-not-survive"}'
                )
                return {
                    "access_token": "plaintext-access-token-must-not-survive",
                    "account": {
                        "home_account_id": "home-account",
                        "username": "engineer@example.test",
                    },
                    "id_token_claims": {"tid": "tenant-id"},
                }

            application.return_value.acquire_token_interactive.side_effect = authorize
            success, _ = interactive_sign_in("tenant-id")
            self.assertTrue(success)
            self.assertTrue(identity_state_available())
            self.assertTrue(token_cache_path().is_file())
            encrypted = account_state_path().read_bytes()
            self.assertNotIn(b"engineer@example.test", encrypted)
            self.assertNotIn(b"plaintext-access-token", encrypted)
            self.assertNotIn(b"plaintext-access-token", token_cache_path().read_bytes())
            application.return_value.acquire_token_interactive.assert_called_once()

    @patch("azure_health_beacon.identity.app_data_dir")
    def test_authentication_purge_only_deletes_isolated_profile(self, data_dir) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data_dir.return_value = base
            isolated = base / "identity"
            isolated.mkdir()
            (isolated / "token-cache.bin").write_bytes(b"encrypted-placeholder")
            legacy = base / "azure-cli"
            legacy.mkdir()
            sibling = base / "checks.json"
            sibling.write_text("rules remain", encoding="utf-8")
            delete_isolated_azure_state()
            self.assertFalse(isolated.exists())
            self.assertFalse(legacy.exists())
            self.assertTrue(sibling.exists())

    @patch("azure_health_beacon.azure.interactive_sign_in")
    def test_interactive_login_never_passes_a_password_or_requests_a_token(
        self, sign_in
    ) -> None:
        sign_in.return_value = (True, "Microsoft sign-in completed.")
        success, _ = interactive_login("22222222-2222-2222-2222-222222222222")
        self.assertTrue(success)
        self.assertEqual(
            sign_in.call_args.args,
            ("22222222-2222-2222-2222-222222222222", 300),
        )

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


class AzureRestTests(unittest.TestCase):
    @staticmethod
    def response(payload: object) -> Mock:
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.content = b"json"
        response.json.return_value = payload
        return response

    @patch("azure_health_beacon.azure_rest.get_access_token")
    def test_authorization_cannot_leave_the_expected_microsoft_endpoint(
        self, token: Mock
    ) -> None:
        with self.assertRaisesRegex(OSError, "unexpected endpoint"):
            azure_rest_module._request_json(
                "GET", "https://attacker.example/collect", "tenant-id", 30
            )
        token.assert_not_called()

    @patch("azure_health_beacon.azure_rest.get_access_token", return_value="memory-token")
    @patch("azure_health_beacon.azure_rest.home_tenant_id", return_value="home-tenant")
    @patch("azure_health_beacon.azure_rest.requests.request")
    def test_subscription_discovery_uses_direct_https_across_tenants(
        self, request: Mock, _home: Mock, token: Mock
    ) -> None:
        responses = iter([
            self.response(
                {"value": [{"tenantId": "home-tenant"}, {"tenantId": "guest-tenant"}]}
            ),
            self.response(
                {
                    "value": [
                        {
                            "subscriptionId": "sub-home",
                            "displayName": "Home",
                            "state": "Enabled",
                        }
                    ]
                }
            ),
            self.response(
                {
                    "value": [
                        {
                            "subscriptionId": "sub-guest",
                            "displayName": "Guest",
                            "state": "Enabled",
                        }
                    ]
                }
            ),
        ])
        seen_headers: list[dict[str, str]] = []

        def send(_method: str, _url: str, **kwargs: object) -> Mock:
            seen_headers.append(dict(kwargs["headers"]))  # type: ignore[arg-type]
            return next(responses)

        request.side_effect = send
        completed = azure_rest_module.execute_azure_operation(
            ["account", "list", "--output", "json"], 30
        )
        self.assertEqual(completed.returncode, 0)
        subscriptions = json.loads(completed.stdout)
        self.assertEqual({item["id"] for item in subscriptions}, {"sub-home", "sub-guest"})
        self.assertEqual(token.call_count, 3)
        for call, headers in zip(request.call_args_list, seen_headers, strict=True):
            self.assertTrue(call.args[1].startswith("https://"))
            self.assertEqual(headers["Authorization"], "Bearer memory-token")

    @patch(
        "azure_health_beacon.azure_rest._tenant_for_subscription",
        return_value="tenant-id",
    )
    @patch("azure_health_beacon.azure_rest.get_access_token", return_value="memory-token")
    @patch("azure_health_beacon.azure_rest.requests.request")
    def test_resource_property_uses_provider_api_and_direct_arm_request(
        self, request: Mock, _token: Mock, _tenant: Mock
    ) -> None:
        request.side_effect = [
            self.response(
                {
                    "resourceTypes": [
                        {
                            "resourceType": "azureFirewalls",
                            "apiVersions": ["2025-01-01-preview", "2024-10-01"],
                        }
                    ]
                }
            ),
            self.response({"properties": {"provisioningState": "Succeeded"}}),
        ]
        completed = azure_rest_module.execute_azure_operation(
            [
                "resource",
                "show",
                "--ids",
                RESOURCE_ID,
                "--subscription",
                "11111111-1111-1111-1111-111111111111",
                "--query",
                "properties.provisioningState",
                "--output",
                "tsv",
            ],
            30,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "Succeeded")
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["api-version"], "2024-10-01"
        )


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


class ExtensibleSignalTests(unittest.TestCase):
    def log_definition(self) -> CheckDefinition:
        return CheckDefinition(
            "logs-one",
            "Three errors in one session",
            "",
            expected_values=[],
            kind="azure_log_analytics",
            query="AppExceptions | summarize count() by SessionId | where count_ >= 3",
            scope="workspace",
            workspace_id="11111111-1111-1111-1111-111111111111",
            lookback_minutes=5,
        )

    @patch("azure_health_beacon.azure._run_az")
    def test_log_query_rows_are_confirmed_findings(self, run_az) -> None:
        run_az.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "SessionId": "session-17",
                        "UserId": "customer-3",
                        "count_": 3,
                    }
                ]
            ),
            "",
        )
        result = run_log_analytics_check(self.log_definition(), retry_count=0)
        self.assertEqual(result.state, CheckState.FAILED)
        self.assertEqual(result.observed_value, "1")
        arguments = run_az.call_args.args[0]
        self.assertIn("--workspace", arguments)
        self.assertIn("--analytics-query", arguments)
        query = arguments[arguments.index("--analytics-query") + 1]
        self.assertTrue(query.endswith("| take 26"))

    @patch("azure_health_beacon.azure._run_az")
    def test_log_query_error_is_unknown_not_healthy(self, run_az) -> None:
        run_az.return_value = subprocess.CompletedProcess(
            [], 1, "", "Forbidden: table access denied"
        )
        result = run_log_analytics_check(self.log_definition(), retry_count=0)
        self.assertEqual(result.state, CheckState.UNCONNECTABLE)

    @patch("azure_health_beacon.azure._run_az")
    def test_metric_threshold_and_no_data_semantics(self, run_az) -> None:
        definition = CheckDefinition(
            "metric-one",
            "Host pool occupancy",
            RESOURCE_ID,
            expected_values=[],
            kind="azure_monitor_metric",
            metric_name="SessionOccupancyPercent",
            metric_aggregation="Average",
            metric_reducer="latest",
            metric_operator="gte",
            metric_threshold=100,
            lookback_minutes=5,
        )
        run_az.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "value": [
                        {
                            "timeseries": [
                                {
                                    "data": [
                                        {
                                            "timeStamp": "2026-08-27T08:00:00Z",
                                            "average": 99,
                                        },
                                        {
                                            "timeStamp": "2026-08-27T08:01:00Z",
                                            "average": 100,
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ),
            "",
        )
        result = run_metric_check(definition, retry_count=0)
        self.assertEqual(result.state, CheckState.FAILED)
        run_az.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"value": []}), ""
        )
        result = run_metric_check(definition, retry_count=0)
        self.assertEqual(result.state, CheckState.UNCONNECTABLE)

    def test_log_rule_pack_is_portable_and_imported_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.ahbrules.json"
            export_rule_pack(path, [self.log_definition()])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 4)
            self.assertNotIn("token", path.read_text(encoding="utf-8").casefold())
            imported = import_rule_pack(path)
            self.assertFalse(imported[0].enabled)
            self.assertEqual(
                imported[0].workspace_id, self.log_definition().workspace_id
            )

    def test_metric_rule_rejects_non_finite_threshold(self) -> None:
        definition = CheckDefinition(
            "metric-bad",
            "Invalid threshold",
            RESOURCE_ID,
            expected_values=[],
            kind="azure_monitor_metric",
            metric_name="Percentage CPU",
            metric_threshold=float("nan"),
        )
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_definition(definition)

    @patch("azure_health_beacon.azure._run_az")
    def test_generic_property_can_compare_any_arm_property(self, run_az) -> None:
        definition = CheckDefinition(
            "property-one",
            "Firewall tier",
            RESOURCE_ID,
            expected_values=["Premium"],
            kind="azure_resource_property",
            property_path="properties.sku.tier",
            property_operator="equals_any",
        )
        run_az.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps("Premium"), ""
        )
        result = run_resource_property_check(definition, retry_count=0)
        self.assertEqual(result.state, CheckState.HEALTHY)
        self.assertEqual(result.observed_value, "Premium")
        arguments = run_az.call_args.args[0]
        self.assertEqual(arguments[:2], ["resource", "show"])
        self.assertEqual(
            arguments[arguments.index("--query") + 1], definition.property_path
        )

    @patch("azure_health_beacon.azure._run_az")
    def test_missing_property_is_a_confirmed_result(self, run_az) -> None:
        definition = CheckDefinition(
            "property-two",
            "Required marker",
            RESOURCE_ID,
            expected_values=[],
            kind="azure_resource_property",
            property_path="properties.requiredMarker",
            property_operator="missing",
        )
        run_az.return_value = subprocess.CompletedProcess([], 0, "", "")
        result = run_resource_property_check(definition, retry_count=0)
        self.assertEqual(result.state, CheckState.HEALTHY)
        self.assertEqual(result.observed_value, "Missing")

    @patch("azure_health_beacon.azure._run_az")
    def test_vm_instance_view_reports_live_power_state(self, run_az) -> None:
        definition = CheckDefinition(
            "vm-one",
            "Orion VM running",
            VM_RESOURCE_ID,
            expected_values=["PowerState/running"],
            kind="azure_vm_power_state",
        )
        run_az.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "statuses": [
                        {"code": "ProvisioningState/succeeded"},
                        {"code": "PowerState/deallocated"},
                    ]
                }
            ),
            "",
        )
        result = run_vm_power_state_check(definition, retry_count=0)
        self.assertEqual(result.state, CheckState.FAILED)
        self.assertEqual(result.observed_value, "PowerState/deallocated")


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
        with patch.dict(
            "azure_health_beacon.updater.os.environ",
            {
                "_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI-stale",
                "BEACON_TEST_MARKER": "preserved",
            },
            clear=True,
        ):
            launch_installer(installer, automatic=False)
        arguments = popen.call_args.args[0]
        self.assertIn("/VERYSILENT", arguments)
        self.assertIn("/RESTARTAPPLICATIONS", arguments)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(environment["BEACON_TEST_MARKER"], "preserved")
        self.assertEqual(
            environment["_PYI_APPLICATION_HOME_DIR"], r"C:\Temp\_MEI-stale"
        )


class WindowsStartupTests(unittest.TestCase):
    def test_startup_command_quotes_executable_and_marks_startup_launch(self) -> None:
        command = startup_command(r"C:\Program Files\Azure Health Beacon\Beacon.exe")
        self.assertIn('"C:\\Program Files\\Azure Health Beacon\\Beacon.exe"', command)
        self.assertTrue(command.endswith("--startup"))


if __name__ == "__main__":
    unittest.main()
