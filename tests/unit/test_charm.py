# Copyright 2024-2026 Dwellir
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import MagicMock, patch

import ops.testing

import constants as c
from charm import BitcoinCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(BitcoinCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_charm_initializes_with_rpc_proxy_defaults(self):
        # Exercises __init__ (relation providers + stored state) with the M6 config.
        self.assertTrue(self.harness.charm.config.get("rpc-proxy-filter"))
        self.assertEqual(self.harness.charm.config.get("rpc-proxy-version"), "0.1.0")
        self.assertTrue(self.harness.charm.config.get("disable-wallet"))

    @patch("charm.utils")
    def test_upgrade_rewrites_bitcoind_args_without_restarting(self, mock_utils):
        # Upgrade must re-render /etc/default/bitcoind so loopback-pin / wallet
        # hardening reaches units upgraded from a pre-hardening revision, but the
        # rewrite itself must not restart bitcoind -- the restart is decided
        # separately from the unit/args change signals.
        mock_utils.service_running.return_value = True
        mock_utils.install_service_file.return_value = False
        mock_utils.update_service_args.return_value = False
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.update_service_args.assert_called_once()
        _, kwargs = mock_utils.update_service_args.call_args
        self.assertFalse(kwargs["restart_service"])

    @patch("charm.utils")
    def test_upgrade_restarts_bitcoind_on_args_change(self, mock_utils):
        # A running node whose rendered args changed is restarted once so the new
        # args take effect.
        mock_utils.service_running.return_value = True
        mock_utils.install_service_file.return_value = False
        mock_utils.update_service_args.return_value = True
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.restart_service.assert_called_once()

    @patch("charm.utils")
    def test_upgrade_restarts_bitcoind_on_unit_change(self, mock_utils):
        # A changed bitcoind.service unit also warrants a restart, even when the
        # rendered args are unchanged.
        mock_utils.service_running.return_value = True
        mock_utils.install_service_file.return_value = True
        mock_utils.update_service_args.return_value = False
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.restart_service.assert_called_once()

    @patch("charm.utils")
    def test_upgrade_does_not_restart_unchanged_bitcoind(self, mock_utils):
        # A no-op upgrade (no unit/args change) must not disturb a running node.
        mock_utils.service_running.return_value = True
        mock_utils.install_service_file.return_value = False
        mock_utils.update_service_args.return_value = False
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.restart_service.assert_not_called()

    @patch("charm.utils")
    def test_upgrade_does_not_restart_stopped_bitcoind(self, mock_utils):
        # Args are still rewritten, but a stopped node is not started by an upgrade
        # even when the unit/args changed.
        mock_utils.service_running.return_value = False
        mock_utils.install_service_file.return_value = True
        mock_utils.update_service_args.return_value = True
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.update_service_args.assert_called_once()
        _, kwargs = mock_utils.update_service_args.call_args
        self.assertFalse(kwargs["restart_service"])
        mock_utils.restart_service.assert_not_called()

    @patch("charm.utils")
    def test_upgrade_does_not_redownload_binaries(self, mock_utils):
        # Upgrade re-applies charm logic (unit files, env, args) but the
        # version-driven binaries belong to _on_config_changed, not the upgrade
        # hook. It re-renders the proxy unit/env without downloading the binary.
        mock_utils.service_running.return_value = True
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.install_bitcoin.assert_not_called()
        mock_utils.install_rpc_proxy.assert_not_called()
        mock_utils.install_rpc_proxy_service.assert_called_once()

    @patch("charm.utils")
    def test_upgrade_reinstalls_pinned_dependencies(self, mock_utils):
        # Upgrade must re-run the pinned monitor venv install so a revision that
        # bumps PIP_PACKAGES (or migrates a unit off a pre-venv revision) lands
        # the new deps, and re-lay the monitor unit so its venv ExecStart applies.
        mock_utils.service_running.return_value = True
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.install_dependencies.assert_called_once()
        mock_utils.install_bitcoind_monitor.assert_called_once()

    @patch("charm.utils")
    def test_upgrade_restarts_running_monitor(self, mock_utils):
        # A running monitor is restarted so reinstalled deps take effect.
        mock_utils.service_running.return_value = True
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.restart_monitor.assert_called_once()

    @patch("charm.utils")
    def test_upgrade_does_not_restart_stopped_monitor(self, mock_utils):
        # A stopped monitor stays stopped; an upgrade does not start it.
        mock_utils.service_running.return_value = False
        mock_utils.get_version.return_value = "v-test"
        self.harness.charm.on.upgrade_charm.emit()
        mock_utils.restart_monitor.assert_not_called()

    @patch("charm.utils")
    def test_cred_rotation_refreshes_proxy_env(self, mock_utils):
        # Rotating rpc-user/rpc-password must rewrite the proxy's env file and
        # restart it, or it keeps probing upstream with dead credentials.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = True
        self.harness.update_config({"rpc-user": "rotated", "rpc-password": "rotated"})
        mock_utils.write_rpc_proxy_env_file.assert_called_once()
        mock_utils.restart_rpc_proxy.assert_called_once()

    @patch("charm.utils")
    def test_cred_rotation_skips_proxy_without_binary(self, mock_utils):
        # A blocked unit (no proxy binary) has no proxy service to refresh.
        mock_utils.get_version.return_value = "v-test"
        mock_utils.service_running.return_value = True
        mock_utils.bitcoind_binary_installed.return_value = True
        mock_utils.rpc_proxy_binary_installed.return_value = False
        self.harness.update_config({"rpc-user": "rotated"})
        mock_utils.write_rpc_proxy_env_file.assert_not_called()
        mock_utils.restart_rpc_proxy.assert_not_called()

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
