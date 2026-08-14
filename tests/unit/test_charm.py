# Copyright 2024-2026 Dwellir
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import MagicMock, patch

import ops.testing
from ops.testing import Context, State, StoredState

import charm as charm_module
import constants as c
from charm import BitcoinCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        metadata_patcher = patch("charm.bitcoin_metadata.collect_upload_metadata", return_value=None)
        self.mock_collect_metadata = metadata_patcher.start()
        self.addCleanup(metadata_patcher.stop)
        self.harness = ops.testing.Harness(BitcoinCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_charm_initializes_with_rpc_proxy_defaults(self):
        # Exercises __init__ (relation providers + stored state) with the M6 config.
        self.assertTrue(self.harness.charm.config.get("rpc-proxy-filter"))
        self.assertEqual(self.harness.charm.config.get("rpc-proxy-version"), "0.1.0")
        self.assertTrue(self.harness.charm.config.get("disable-wallet"))

    def test_collector_credentials_config_is_secret_typed(self):
        self.assertEqual(self.harness.charm.meta.config["collector-s3-credentials"].type, "secret")

    @patch("charm.bitcoin_metadata.collect_upload_metadata")
    @patch("charm.utils")
    def test_update_status_collects_metadata_after_service_health(self, mock_utils, collect_metadata):
        mock_utils.get_version.return_value = "v31.1.0"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.service_running.return_value = True
        collect_metadata.return_value = None

        self.harness.charm._update_status()

        collect_metadata.assert_called_once_with(self.harness.charm)
        self.assertIsInstance(self.harness.charm.unit.status, ops.ActiveStatus)

    @patch("charm.bitcoin_metadata.collect_upload_metadata")
    @patch("charm.utils")
    def test_update_status_blocks_on_metadata_failure(self, mock_utils, collect_metadata):
        mock_utils.get_version.return_value = "v31.1.0"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.service_running.return_value = True
        collect_metadata.return_value = "metadata upload failed: denied"

        self.harness.charm._update_status()

        self.assertEqual(self.harness.charm.unit.status, ops.BlockedStatus("metadata upload failed: denied"))

    @patch("charm.bitcoin_metadata.collect_upload_metadata")
    @patch("charm.utils")
    def test_upgrade_charm_is_metadata_only(self, mock_utils, collect_metadata):
        mock_utils.get_version.return_value = "v31.1.0"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.service_running.return_value = True
        collect_metadata.return_value = None

        self.harness.charm.on.upgrade_charm.emit()

        collect_metadata.assert_called_once_with(self.harness.charm)
        for method in (
            "install_dependencies",
            "install_service_file",
            "install_bitcoind_monitor",
            "install_rpc_proxy_service",
            "update_service_args",
            "restart_service",
            "restart_monitor",
            "restart_rpc_proxy",
            "start_service",
            "stop_service",
        ):
            getattr(mock_utils, method).assert_not_called()

    @patch("charm.utils")
    def test_version_change_uses_transactional_config_lifecycle(self, mock_utils):
        mock_utils.get_version.return_value = "v31.0.0"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.service_running.return_value = True

        self.harness.update_config({"version": "31.0"})

        mock_utils.apply_config_transaction.assert_called_once()
        transaction = mock_utils.apply_config_transaction.call_args
        self.assertIs(transaction.args[0], self.harness.charm.config)
        self.assertEqual(transaction.kwargs["changed_keys"], {"version"})
        mock_utils.install_bitcoin.assert_not_called()

    @patch("charm.utils")
    def test_binary_url_change_uses_transactional_config_lifecycle(self, mock_utils):
        mock_utils.get_version.return_value = "v31.0.0"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.service_running.return_value = True
        binary_url = "https://downloads.example.test/bitcoin-31.0-x86_64-linux-gnu.tar.gz"

        self.harness.update_config({"version": "31.0", "binary-url": binary_url})

        transaction = mock_utils.apply_config_transaction.call_args
        self.assertEqual(transaction.kwargs["changed_keys"], {"version", "binary-url"})
        self.assertEqual(transaction.args[0]["binary-url"], binary_url)

    @patch("charm.utils")
    def test_combined_credential_and_version_change_commits_stored_state_after_transaction(self, mock_utils):
        mock_utils.get_version.return_value = "v31.0.0"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.service_running.return_value = True

        self.harness.update_config(
            {
                "rpc-user": "new-user",
                "rpc-password": "new-password",
                "service-args": "-txindex=1",
                "version": "31.0",
            }
        )

        transaction = mock_utils.apply_config_transaction.call_args
        self.assertEqual(
            transaction.kwargs["changed_keys"],
            {"rpc-user", "rpc-password", "service-args", "version"},
        )
        self.assertNotEqual(transaction.kwargs["previous_config"]["rpc-user"], "new-user")
        self.assertNotEqual(transaction.kwargs["previous_config"]["rpc-password"], "new-password")
        self.assertNotEqual(transaction.kwargs["previous_config"]["version"], "31.0")
        self.assertEqual(self.harness.charm._stored.rpc_user, "new-user")
        self.assertEqual(self.harness.charm._stored.rpc_password, "new-password")
        self.assertEqual(self.harness.charm._stored.service_args, "-txindex=1")
        self.assertEqual(self.harness.charm._stored.version, "31.0")

    @patch("charm.utils")
    def test_failed_config_transaction_preserves_previous_stored_state(self, mock_utils):
        mock_utils.apply_config_transaction.side_effect = RuntimeError("activation failed")

        with self.assertRaisesRegex(RuntimeError, "activation failed"):
            self.harness.update_config({"rpc-user": "new-user", "version": "31.0"})

        self.assertNotEqual(self.harness.charm._stored.rpc_user, "new-user")
        self.assertNotEqual(self.harness.charm._stored.version, "31.0")

    @patch(
        "charm.bitcoin_metadata.redact_runtime_value",
        side_effect=lambda value: value.replace("secret-value", "REDACTED"),
    )
    @patch("charm.utils")
    def test_get_node_info_redacts_runtime_and_proxy_values(self, mock_utils, _redact):
        mock_utils.get_service_args.return_value = "BITCOIND_CLI_ARGS=-rpcpassword=secret-value -txindex=1"
        mock_utils.get_client_proc_cmdline.return_value = "bitcoind -rpcpassword=secret-value -txindex=1"
        mock_utils.get_rpc_proxy_env.return_value = "PROXY_UPSTREAM_PASSWORD=secret-value"
        event = MagicMock()

        self.harness.charm._on_get_node_info_action(event)

        results = {}
        for result_call in event.set_results.call_args_list:
            results.update(result_call.kwargs["results"])
        self.assertNotIn("secret-value", repr(results))

    @patch("charm.utils")
    def test_action_result_boundary_recursively_redacts_serialized_secrets(self, mock_utils):
        mock_utils.get_client_help_output.return_value = (
            'relation={"headers":{"X-API-Key":"action boundary secret"},"safe":"visible"}'
        )
        event = MagicMock()

        self.harness.charm._on_get_node_help_action(event)

        results = event.set_results.call_args.kwargs["results"]
        self.assertNotIn("action boundary secret", repr(results))
        self.assertIn("visible", repr(results))

    @patch("charm.utils")
    def test_cred_rotation_refreshes_proxy_env(self, mock_utils):
        # Credential rotation must enter the transaction that updates every RPC consumer.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.update_config({"rpc-user": "rotated", "rpc-password": "rotated"})
        transaction = mock_utils.apply_config_transaction.call_args
        self.assertEqual(transaction.kwargs["changed_keys"], {"rpc-user", "rpc-password"})

    @patch("charm.utils")
    def test_update_status_blocks_without_bitcoind_binary(self, mock_utils):
        # An empty `version` config means no bitcoind binary: Blocked, not error.
        mock_utils.get_version.return_value = ""
        mock_utils.bitcoind_binary_installed.return_value = False
        self.harness.charm._update_status()
        self.assertEqual(
            self.harness.charm.unit.status,
            ops.BlockedStatus("bitcoind not installed; set version"),
        )

    @patch("charm.utils")
    def test_update_status_blocks_without_proxy_binary(self, mock_utils):
        # bitcoind present but proxy binary missing: the proxy-specific block.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = False
        self.harness.charm._update_status()
        self.assertEqual(
            self.harness.charm.unit.status,
            ops.BlockedStatus("rpc-proxy binary not installed; set rpc-proxy-version"),
        )

    @patch("charm.utils")
    def test_start_node_action_starts_proxy(self, mock_utils):
        # The proxy is the RPC front door, so start-node brings it up too.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.charm._on_start_node_action(MagicMock())
        mock_utils.start_service.assert_called_once()
        mock_utils.start_monitor.assert_called_once()
        mock_utils.start_rpc_proxy.assert_called_once()

    @patch("charm.utils")
    def test_start_node_action_skips_proxy_without_binary(self, mock_utils):
        # Nothing to start if the proxy binary was never downloaded.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = False
        self.harness.charm._on_start_node_action(MagicMock())
        mock_utils.start_rpc_proxy.assert_not_called()

    @patch("charm.utils")
    def test_stop_node_action_stops_proxy(self, mock_utils):
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        self.harness.charm._on_stop_node_action(MagicMock())
        mock_utils.stop_service.assert_called_once()
        mock_utils.stop_monitor.assert_called_once()
        mock_utils.stop_rpc_proxy.assert_called_once()

    @patch("charm.utils")
    def test_get_node_info_action_reports_proxy(self, mock_utils):
        # node-info must cover the proxy: it is the RPC front door.
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.get_status.return_value = True
        mock_utils.get_service_args.return_value = ""
        mock_utils.get_client_proc_cmdline.return_value = ""
        mock_utils.get_rpc_proxy_env.return_value = "PROXY_LISTEN=0.0.0.0:8331"
        mock_utils.get_rpc_proxy_version.return_value = "0.1.0"
        mock_utils.get_charm_version.return_value = "v0.1.0-6-gd0d4771"
        event = MagicMock()
        self.harness.charm._on_get_node_info_action(event)
        results = {}
        for call in event.set_results.call_args_list:
            results.update(call.kwargs["results"])
        self.assertIs(results["rpc-proxy-installed"], True)
        self.assertIs(results["rpc-proxy-running"], True)
        self.assertEqual(results["rpc-proxy-env"], "PROXY_LISTEN=0.0.0.0:8331")
        self.assertEqual(results["rpc-proxy-version"], "0.1.0")
        self.assertEqual(results["charm-version"], "v0.1.0-6-gd0d4771")
        mock_utils.get_status.assert_called_once_with(c.RPC_PROXY_SERVICE_NAME)

    @patch("charm.time.sleep")
    @patch("charm.utils")
    def test_restart_node_action_restarts_proxy(self, mock_utils, _mock_sleep):
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.charm._on_restart_node_action(MagicMock())
        mock_utils.start_service.assert_called_once()
        mock_utils.restart_rpc_proxy.assert_called_once()

    @staticmethod
    def _log_names(log):
        # Names are recoverable from the flat "<timestamp>  <name>" lines.
        return [line.rsplit("  ", 1)[-1] for line in log]

    @patch("charm.utils")
    def test_event_log_records_hooks_and_actions_in_order(self, mock_utils):
        # A lifecycle hook and an action both land in the log, in the order run.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.charm.on.config_changed.emit()
        self.harness.charm._on_get_node_help_action(MagicMock())
        names = self._log_names(list(self.harness.charm._stored.event_log))
        self.assertIn("config-changed", names)
        self.assertIn("get-node-help", names)
        self.assertLess(names.index("config-changed"), names.index("get-node-help"))

    def test_event_log_cap_enforced(self):
        # The ring buffer keeps only the newest _EVENT_LOG_MAX entries.
        with patch.object(charm_module, "_EVENT_LOG_MAX", 3):
            for i in range(5):
                self.harness.charm._record_event(f"e{i}")
        names = self._log_names(list(self.harness.charm._stored.event_log))
        self.assertEqual(names, ["e2", "e3", "e4"])

    @patch("charm.utils")
    def test_node_info_action_truncates_event_log_to_16(self, mock_utils):
        # node-info shows only the latest 16 entries, newest last.
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.get_service_args.return_value = ""
        mock_utils.get_client_proc_cmdline.return_value = ""
        mock_utils.get_rpc_proxy_env.return_value = ""
        self.harness.charm._stored.event_log = [f"2026-07-06 00:00:{i:02d} UTC  e{i}" for i in range(20)]
        event = MagicMock()
        self.harness.charm._on_get_node_info_action(event)
        results = {}
        for call in event.set_results.call_args_list:
            results.update(call.kwargs["results"])
        lines = results["event-log"].split("\n")
        self.assertEqual(len(lines), 16)
        # The action records itself, so the newest line is get-node-info.
        self.assertEqual(self._log_names(lines)[-1], "get-node-info")

    def test_print_event_log_action_returns_full_log(self):
        # print-event-log dumps the whole history (plus its own invocation).
        self.harness.charm._stored.event_log = [f"2026-07-06 00:00:{i:02d} UTC  e{i}" for i in range(20)]
        event = MagicMock()
        self.harness.charm._on_print_event_log_action(event)
        results = event.set_results.call_args.kwargs["results"]
        lines = results["event-log"].split("\n")
        self.assertEqual(len(lines), 21)
        self.assertEqual(self._log_names(lines)[-1], "print-event-log")

    @patch("charm.utils")
    def test_config_changed_records_changed_keys(self, mock_utils):
        # config-changed annotates which tracked keys changed and their new values.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.update_config({"service-args": "-txindex=1", "disable-wallet": False})
        entry = list(self.harness.charm._stored.event_log)[-1]
        self.assertIn("config-changed  ", entry)
        self.assertIn("service-args=-txindex=1", entry)
        self.assertIn("disable-wallet=False", entry)

    @patch("charm.utils")
    def test_config_changed_redacts_rpc_password(self, mock_utils):
        # The password value is never echoed; only that it changed is recorded.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.update_config({"rpc-password": "hunter2-super-secret"})
        entry = list(self.harness.charm._stored.event_log)[-1]
        self.assertIn("rpc-password=<redacted>", entry)
        self.assertNotIn("hunter2", entry)

    @patch("charm.utils")
    def test_config_changed_redacts_secrets_embedded_in_service_args(self, mock_utils):
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True

        self.harness.update_config({"service-args": "-txindex=1 -rpcauth=alice:secret-value"})

        entry = list(self.harness.charm._stored.event_log)[-1]
        self.assertIn("-rpcauth=REDACTED", entry)
        self.assertNotIn("secret-value", entry)

    @patch("charm.utils")
    def test_node_info_truncates_long_event_detail(self, mock_utils):
        # A long detail is capped in the node-info view; print-event-log keeps it whole.
        mock_utils.rpc_proxy_binary_installed.return_value = True
        mock_utils.get_service_args.return_value = ""
        mock_utils.get_client_proc_cmdline.return_value = ""
        mock_utils.get_rpc_proxy_env.return_value = ""
        long_detail = "service-args=" + "x" * 100
        self.harness.charm._stored.event_log = [f"2026-07-06 00:00:00 UTC  config-changed  {long_detail}"]
        info_event, print_event = MagicMock(), MagicMock()
        self.harness.charm._on_get_node_info_action(info_event)
        self.harness.charm._on_print_event_log_action(print_event)
        info_results = {}
        for call in info_event.set_results.call_args_list:
            info_results.update(call.kwargs["results"])
        config_line = next(ln for ln in info_results["event-log"].split("\n") if "config-changed" in ln)
        self.assertTrue(config_line.endswith("..."))
        self.assertIn(long_detail, print_event.set_results.call_args.kwargs["results"]["event-log"])

    @patch("charm.utils")
    def test_upgrade_charm_records_target_version(self, mock_utils):
        # upgrade-charm annotates the version being upgraded to.
        mock_utils.get_charm_version.return_value = "v0.4.0"
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        self.harness.charm.on.upgrade_charm.emit()
        entry = list(self.harness.charm._stored.event_log)[-1]
        self.assertEqual(entry.split("  ", 1)[-1], "upgrade-charm  v0.4.0")


class TestEventLogPersistence(unittest.TestCase):
    """Scenario-based tests for StoredState serialization of the event log.

    The in-process Harness never triggers the load-time StoredList wrapping that
    breaks nested-container designs, so the round-trip that proves the flat-string
    design survives a real load-modify-save must run under Scenario.
    """

    @patch("charm.bitcoin_metadata.collect_upload_metadata", return_value=None)
    @patch("charm.utils")
    def test_event_log_survives_load_and_append(self, mock_utils, _collect_metadata):
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        ctx = Context(BitcoinCharm)
        seeded = StoredState(
            "_stored",
            owner_path="BitcoinCharm",
            content={"event_log": ["2026-07-06 00:00:00 UTC  install"]},
        )
        state_in = State(stored_states={seeded})
        # config-changed loads the seeded log, appends, and re-saves it. A nested
        # container here would raise ValueError on save; a flat list[str] does not.
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        stored = state_out.get_stored_state("_stored", owner_path="BitcoinCharm")
        names = [line.rsplit("  ", 1)[-1] for line in stored.content["event_log"]]
        self.assertEqual(names, ["install", "config-changed"])
