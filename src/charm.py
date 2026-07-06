#!/usr/bin/env python3
# Copyright 2024-2026 Dwellir
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the Bitcoin blockchain client.

See the README for more information on how to use this charm.
"""

import logging
import time

import ops

import constants as c
import utils
from interface_prometheus import PrometheusProvider

logger = logging.getLogger(__name__)

# Cap on the rolling event log kept in StoredState. Persisted into the unit
# database, so bound it (ring buffer) to keep the log from growing unbounded.
_EVENT_LOG_MAX = 256

# Config keys whose value must never be echoed into the event log.
_SENSITIVE_CONFIG = {"rpc-password"}

# (config-key, stored-attr) pairs the charm diffs on config-changed. Kept in sync
# with the per-key comparisons in _on_config_changed so the recorded summary
# matches what the handler actually acts on.
_TRACKED_CONFIG = (
    ("rpc-user", "rpc_user"),
    ("rpc-password", "rpc_password"),
    ("service-args", "service_args"),
    ("version", "version"),
    ("rpc-proxy-filter", "rpc_proxy_filter"),
    ("rpc-proxy-version", "rpc_proxy_version"),
    ("rpc-proxy-listen", "rpc_proxy_listen"),
    ("rpc-proxy-extend-allowlist", "rpc_proxy_extend_allowlist"),
    ("disable-wallet", "disable_wallet"),
)

# Width the event-log detail (the part after the event name) is truncated to in
# the get-node-info compact view. print-event-log keeps the full detail.
_EVENT_DETAIL_WIDTH = 32


def _truncate_event_detail(line: str, width: int = _EVENT_DETAIL_WIDTH) -> str:
    """Return `line` with its trailing detail (after "<timestamp>  <name>") capped at `width` chars.

    Entries without a detail (plain "<timestamp>  <name>") are returned unchanged.
    """
    parts = line.split("  ", 2)
    if len(parts) < 3:
        return line
    timestamp, name, detail = parts
    if len(detail) > width:
        detail = detail[:width] + "..."
    return f"{timestamp}  {name}  {detail}"


class BitcoinCharm(ops.CharmBase):
    """Charm the Bitcoin blockchain client."""

    _stored = ops.StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        # Relations provided
        self.prometheus_node_provider = PrometheusProvider(self, "node-prometheus", 9100, "/metrics")
        self.prometheus_monitor_provider = PrometheusProvider(self, "monitor-prometheus", 9332, "/metrics")
        self.prometheus_rpc_proxy_provider = PrometheusProvider(
            self, "rpc-proxy-prometheus", c.RPC_PROXY_ADMIN_PORT, "/metrics"
        )
        # Hooks
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        # Stored values
        self._stored.set_default(
            rpc_password=self.config.get("rpc-password"),
            rpc_user=self.config.get("rpc-user"),
            service_args=self.config.get("service-args"),
            version=self.config.get("version"),
            rpc_proxy_filter=self.config.get("rpc-proxy-filter"),
            rpc_proxy_version=self.config.get("rpc-proxy-version"),
            rpc_proxy_listen=self.config.get("rpc-proxy-listen"),
            rpc_proxy_extend_allowlist=self.config.get("rpc-proxy-extend-allowlist"),
            disable_wallet=self.config.get("disable-wallet"),
            event_log=[],
        )
        # Actions
        self.framework.observe(self.on.get_node_help_action, self._on_get_node_help_action)
        self.framework.observe(self.on.get_node_info_action, self._on_get_node_info_action)
        self.framework.observe(self.on.print_event_log_action, self._on_print_event_log_action)
        self.framework.observe(self.on.print_readme_action, self._on_print_readme_action)
        self.framework.observe(self.on.restart_node_action, self._on_restart_node_action)
        self.framework.observe(self.on.start_node_action, self._on_start_node_action)
        self.framework.observe(self.on.stop_node_action, self._on_stop_node_action)

    def _record_event(self, name: str) -> None:
        """Append a timestamped entry for `name` to the rolling event log.

        Entries are flat, fixed-width strings ("YYYY-MM-DD HH:MM:SS UTC  name")
        so lexical order matches chronological order and StoredState never wraps
        them in a StoredList (which nested containers would trigger). The slice
        keeps only the newest _EVENT_LOG_MAX entries.
        """
        log = list(self._stored.event_log)
        log.append(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  {name}")
        self._stored.event_log = log[-_EVENT_LOG_MAX:]

    def _config_change_summary(self) -> str:
        """Summarize tracked config keys changed since last seen, as comma-joined key=value pairs.

        Reads the pre-update _stored values, so call it before those values are
        refreshed. rpc-password is redacted; other values are recorded in full
        (get-node-info truncates them for its compact view, print-event-log keeps
        them whole). Returns an empty string when nothing tracked changed.
        """
        changes = []
        for key, attr in _TRACKED_CONFIG:
            new = self.config.get(key)
            if getattr(self._stored, attr) == new:
                continue
            changes.append(f"{key}=<redacted>" if key in _SENSITIVE_CONFIG else f"{key}={new}")
        return ", ".join(changes)

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        """Handle changed configuration, restarting bitcoind at most once.

        Each block records whether bitcoind's args need re-rendering rather than
        restarting inline; a single update_service_args at the end applies them, so
        changing several options in one event costs one restart, not several.
        """
        summary = self._config_change_summary()
        self._record_event(f"config-changed  {summary}" if summary else "config-changed")
        restart_bitcoind = False

        if self._stored.rpc_user != self.config.get("rpc-user") or self._stored.rpc_password != self.config.get(
            "rpc-password"
        ):
            utils.install_bitcoind_monitor(self.config, restart_service=True)
            # The proxy authenticates upstream with these creds too; rewrite its
            # env and restart it so rotation doesn't leave it probing with dead
            # credentials until the next proxy config change.
            if utils.rpc_proxy_binary_installed():
                utils.write_rpc_proxy_env_file(self.config)
                utils.restart_rpc_proxy()
            self._stored.rpc_password = self.config.get("rpc-password")
            self._stored.rpc_user = self.config.get("rpc-user")
            restart_bitcoind = True

        if self._stored.service_args != self.config.get("service-args"):
            self._stored.service_args = self.config.get("service-args")
            restart_bitcoind = True

        # Toggling disable-wallet rewrites bitcoind's own args (the loopback pin is
        # unconditional, so only the wallet flag can change here).
        if self._stored.disable_wallet != self.config.get("disable-wallet"):
            self._stored.disable_wallet = self.config.get("disable-wallet")
            restart_bitcoind = True

        if self._stored.version != self.config.get("version"):
            # Stop first so the running binary can be replaced (ETXTBSY otherwise);
            # the final update_service_args restarts into the new binary + args.
            utils.stop_service()
            utils.install_bitcoin(str(self.config.get("version") or ""))
            self._stored.version = self.config.get("version")
            restart_bitcoind = True

        # Refresh the proxy (binary/unit/env, start or stop) before re-rendering
        # bitcoind's args, so the loopback pin only engages once the binary exists.
        if self._rpc_proxy_config_changed():
            self._reconcile_rpc_proxy()
            self._stored.rpc_proxy_filter = self.config.get("rpc-proxy-filter")
            self._stored.rpc_proxy_version = self.config.get("rpc-proxy-version")
            self._stored.rpc_proxy_listen = self.config.get("rpc-proxy-listen")
            self._stored.rpc_proxy_extend_allowlist = self.config.get("rpc-proxy-extend-allowlist")

        if restart_bitcoind:
            utils.update_service_args(self.config, restart_service=True)

        self._update_status()

    def _rpc_proxy_config_changed(self) -> bool:
        """Return True if any proxy-affecting config option changed since last seen."""
        return (
            self._stored.rpc_proxy_filter != self.config.get("rpc-proxy-filter")
            or self._stored.rpc_proxy_version != self.config.get("rpc-proxy-version")
            or self._stored.rpc_proxy_listen != self.config.get("rpc-proxy-listen")
            or self._stored.rpc_proxy_extend_allowlist != self.config.get("rpc-proxy-extend-allowlist")
        )

    def _on_install(self, event):
        """Handle install."""
        self._record_event("install")
        utils.create_user()
        utils.install_dependencies()
        self._install_bitcoind()
        self._install_bitcoind_monitor()
        # The proxy is always bitcoind's RPC front door: lay down its unit/env and
        # download the binary (rpc-proxy-version defaults to a real release).
        self._install_rpc_proxy()
        utils.chown()

    def _install_bitcoind(self, restart_service: bool = False):
        """Install the bitcoind client and service."""
        utils.install_bitcoin(str(self.config.get("version") or ""))
        utils.install_service_file(f"templates/{c.SERVICE_NAME}.service", c.SERVICE_NAME)
        utils.update_service_args(self.config, restart_service=restart_service)

    def _install_bitcoind_monitor(self, restart_service: bool = False):
        """Install the bitcoind monitor."""
        utils.install_service_file(f"templates/{c.MONITOR_SERVICE_NAME}.service", c.MONITOR_SERVICE_NAME)
        utils.install_bitcoind_monitor(self.config, restart_service=restart_service)

    def _install_rpc_proxy(self, restart_service: bool = False):
        """Install the RPC proxy binary (if a version is set), systemd unit, and environment file."""
        utils.install_rpc_proxy(str(self.config.get("rpc-proxy-version") or ""))
        utils.install_rpc_proxy_service(self.config, restart_service=restart_service)

    def _reconcile_rpc_proxy(self):
        """Refresh the always-on proxy (binary/unit/env) and (re)start it.

        The proxy is bitcoind's only RPC front door (bitcoind is loopback-pinned).
        It can only run once its binary is installed; if the binary is missing the
        node is left fail-closed (loopback only) and _update_status blocks the unit.
        """
        self._install_rpc_proxy()
        if utils.rpc_proxy_binary_installed():
            utils.restart_rpc_proxy()

    def _on_start(self, event):
        """Handle start."""
        self._record_event("start")
        utils.start_service()
        utils.start_monitor()
        if utils.rpc_proxy_binary_installed():
            utils.start_rpc_proxy()
        self._update_status()

    def _on_stop(self, event):
        """Handle stop."""
        self._record_event("stop")
        utils.stop_service()
        utils.stop_monitor()
        utils.stop_rpc_proxy()
        self._update_status()

    def _on_update_status(self, event):
        """Handle update status."""
        self._update_status()

    def _update_status(self):
        """Update status."""
        self.unit.set_workload_version(utils.get_version())
        if not utils.bitcoind_binary_installed():
            # No binary means `version` was never set (it defaults to empty) or
            # the install never ran; missing config is Blocked, not a hook error.
            self.unit.status = ops.BlockedStatus("bitcoind not installed; set version")
            return
        if not utils.rpc_proxy_binary_installed():
            # The proxy is the only RPC front door and its binary is missing:
            # bitcoind stays loopback-pinned (fail-closed, not exposed) and the
            # operator must set a valid rpc-proxy-version.
            self.unit.status = ops.BlockedStatus("rpc-proxy binary not installed; set rpc-proxy-version")
            return
        msg_dict = {True: "up", False: "down"}
        status_node = utils.service_running(c.SERVICE_NAME)
        status_monitor = utils.service_running(c.MONITOR_SERVICE_NAME)
        status_proxy = utils.service_running(c.RPC_PROXY_SERVICE_NAME)
        statuses = [status_node, status_monitor, status_proxy]
        parts = [
            f"Node: {msg_dict[status_node]}",
            f"monitor: {msg_dict[status_monitor]}",
            f"proxy: {msg_dict[status_proxy]}",
        ]
        msg = ", ".join(parts)
        if all(statuses):
            self.unit.status = ops.ActiveStatus(msg)
            return
        self.unit.status = ops.WaitingStatus(msg)

    def _on_upgrade_charm(self, event):
        """Handle upgrade charm event."""
        # The upgrade-charm hook runs the new charm code from the new charm dir, so
        # get_charm_version() here is the version being upgraded to.
        self._record_event(f"upgrade-charm  {utils.get_charm_version()}")
        # Re-apply charm logic (unit files, env, args) to the existing host. The
        # bitcoind and proxy binaries are version-driven (config) and handled in
        # _on_config_changed, so neither is re-downloaded here; upgrade should not
        # change the running versions.
        #
        # install_dependencies re-runs the pinned monitor venv install, so a
        # revision that bumps PIP_PACKAGES (or migrates a unit off a pre-venv
        # revision) lands the new deps; the monitor is restarted below to pick
        # them up. The venv is reused in place, not torn down, so the running
        # monitor keeps serving until the explicit restart.
        utils.install_dependencies()
        unit_changed = utils.install_service_file(f"templates/{c.SERVICE_NAME}.service", c.SERVICE_NAME)
        self._install_bitcoind_monitor()
        utils.install_rpc_proxy_service(self.config, restart_service=False)
        utils.chown()
        if utils.service_running(c.MONITOR_SERVICE_NAME):
            utils.restart_monitor()
        # Re-render bitcoind's args in place (no restart here) so loopback-pin / wallet
        # hardening reaches units upgraded from a pre-hardening revision. Restart
        # bitcoind only if something it actually consumes changed -- its unit file or
        # its rendered args -- so a no-op charm upgrade doesn't bounce a running node.
        args_changed = utils.update_service_args(self.config, restart_service=False)
        if (unit_changed or args_changed) and utils.service_running(c.SERVICE_NAME):
            utils.restart_service()
        if utils.rpc_proxy_binary_installed():
            utils.restart_rpc_proxy()
        self._update_status()

    def _on_get_node_help_action(self, event: ops.ActionEvent) -> None:
        self._record_event("get-node-help")
        event.set_results(results={"help-output": utils.get_client_help_output()})

    def _on_get_node_info_action(self, event: ops.ActionEvent) -> None:
        """Provide information about the node to the action's results."""
        self._record_event("get-node-info")
        # Charm
        event.set_results(results={"charm-version": utils.get_charm_version()})
        # Disk usage
        disk_usage = utils.get_disk_usage(c.HOME_DIR)
        event.set_results(results={"disk-usage": disk_usage})
        # Client
        event.set_results(results={"client-version": utils.get_version()})
        event.set_results(results={"client-service-args": utils.get_service_args()})
        proc_cmdline = utils.get_client_proc_cmdline()
        if proc_cmdline:
            event.set_results(results={"client-proc-cmdline": proc_cmdline})
        else:
            event.set_results(results={"client-proc-cmdline": "process not found"})
        # RPC proxy
        event.set_results(results={"rpc-proxy-installed": utils.rpc_proxy_binary_installed()})
        event.set_results(results={"rpc-proxy-version": utils.get_rpc_proxy_version()})
        event.set_results(results={"rpc-proxy-running": utils.get_status(c.RPC_PROXY_SERVICE_NAME)})
        event.set_results(results={"rpc-proxy-env": utils.get_rpc_proxy_env()})
        # Event log (latest 16 to keep the combined output readable, with each
        # entry's detail truncated; use print-event-log for the full history).
        recent = [_truncate_event_detail(line) for line in list(self._stored.event_log)[-16:]]
        event.set_results(results={"event-log": "\n".join(recent)})

    def _on_print_event_log_action(self, event: ops.ActionEvent) -> None:
        """Print the full recorded event log to the action's results."""
        self._record_event("print-event-log")
        event.set_results(results={"event-log": "\n".join(self._stored.event_log)})

    def _on_print_readme_action(self, event: ops.ActionEvent) -> None:
        """Print the README.md file to the action's results."""
        self._record_event("print-readme")
        event.set_results(results={"readme": utils.get_readme()})

    def _on_restart_node_action(self, event: ops.ActionEvent) -> None:
        self._record_event("restart-node")
        self.unit.status = ops.MaintenanceStatus("Restarting node services...")
        utils.stop_service()
        time.sleep(3)  # Wait for the service to fully stop
        utils.start_service()
        # The proxy is bitcoind's RPC front door, so the node actions manage it too.
        if utils.rpc_proxy_binary_installed():
            utils.restart_rpc_proxy()
        self._update_status()

    def _on_start_node_action(self, event: ops.ActionEvent) -> None:
        self._record_event("start-node")
        self.unit.status = ops.MaintenanceStatus("Starting node services...")
        utils.start_service()
        utils.start_monitor()
        if utils.rpc_proxy_binary_installed():
            utils.start_rpc_proxy()
        self._update_status()

    def _on_stop_node_action(self, event: ops.ActionEvent) -> None:
        self._record_event("stop-node")
        self.unit.status = ops.MaintenanceStatus("Stopping node services...")
        utils.stop_monitor()
        utils.stop_service()
        utils.stop_rpc_proxy()
        self._update_status()


if __name__ == "__main__":  # pragma: nocover
    ops.main(BitcoinCharm)  # type: ignore
