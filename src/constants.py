"""Constants for the Bitcoin charm."""

from pathlib import Path

# BITCOIN

USER = "bitcoin"
SERVICE_NAME = "bitcoind"

HOME_DIR = Path("/home/bitcoin")
BINARY_NAME = "bitcoind"
BINARY_PATH = HOME_DIR / BINARY_NAME

# bitcoind's RPC port. The proxy fronts bitcoind on loopback only, so this is an
# internal-only detail pinned by harden_service_args; the proxy upstream and the
# monitor both derive their target from it.
BITCOIND_RPC_PORT = 8332

DL_URL = "https://bitcoincore.org/bin/bitcoin-core-VERSION/bitcoin-VERSION-x86_64-linux-gnu.tar.gz"

# MONITOR

MONITOR_SERVICE_NAME = "bitcoind-monitor"
MONITOR_DIR = HOME_DIR / "monitor"
MONITOR_SCRIPT_NAME = "bitcoind-monitor.py"
MONITOR_SCRIPT_PATH = MONITOR_DIR / MONITOR_SCRIPT_NAME

# The monitor's pip dependencies are installed into a dedicated venv so the
# system Python is left untouched (Ubuntu 24.04 marks it externally managed,
# PEP 668). The monitor service runs from this interpreter; keep it in sync
# with the ExecStart in templates/bitcoind-monitor.service.
MONITOR_VENV_DIR = MONITOR_DIR / "venv"
MONITOR_VENV_PYTHON = MONITOR_VENV_DIR / "bin" / "python"
MONITOR_VENV_PIP = MONITOR_VENV_DIR / "bin" / "pip"

MONITOR_ENV = {
    "BITCOIN_RPC_SCHEME": "http",
    "BITCOIN_RPC_HOST": "localhost",
    "BITCOIN_RPC_PORT": str(BITCOIND_RPC_PORT),
    "BITCOIN_RPC_USER": "default",
    "BITCOIN_RPC_PASSWORD": "default",
    "METRICS_PORT": "9332",
    "METRICS_ADDR": "",
    "LOG_LEVEL": "INFO",
    # Per-address ban metrics are opt-in upstream (high label cardinality);
    # kept on here to preserve the metrics this charm emitted previously.
    "BAN_ADDRESS_METRICS": "true",
}

# RPC PROXY

RPC_PROXY_SERVICE_NAME = "bitcoin-rpc-proxy"
RPC_PROXY_BINARY_NAME = "bitcoin-rpc-proxy"
RPC_PROXY_BINARY_PATH = HOME_DIR / RPC_PROXY_BINARY_NAME

# Default proxy ports. The admin port is frozen (no config knob) so the metrics
# relation and health probes have a stable target; the main listen port is
# operator-configurable via rpc-proxy-listen. PROXY_ADMIN_LISTEN binds the admin
# server (health + unauthenticated /metrics) to 0.0.0.0 so a remote Prometheus can
# scrape it via the unit's ingress address; this is accepted as low-risk (the data
# is non-sensitive telemetry and the port is not externally exposed in deployment).
RPC_PROXY_PORT = 8331
RPC_PROXY_ADMIN_PORT = 8360

# Release asset published by .github/workflows/release.yml: a GitHub Release on
# this repo, tag v<version>, asset bitcoin-rpc-proxy-<version>-linux-amd64.
# VERSION is substituted at install time (the rpc-proxy-version config),
# mirroring DL_URL.
RPC_PROXY_DL_URL = (
    "https://github.com/dwellir-public/bitcoin-rpc-operator/releases/download/"
    "vVERSION/bitcoin-rpc-proxy-VERSION-linux-amd64"
)

# Written to /etc/default/bitcoin-rpc-proxy; the proxy reads these PROXY_* vars
# (with flag overrides). PROXY_LISTEN, PROXY_EXTEND_ALLOWLIST, PROXY_FILTER, and the
# upstream probe credentials are overridden from charm config in
# utils.write_rpc_proxy_env_file; PROXY_ADMIN_LISTEN is frozen. PROXY_UPSTREAM_USER
# / PROXY_UPSTREAM_PASSWORD authenticate the health probe only (forwarded client
# traffic carries the client's own auth); they reuse the monitor's rpc-user/rpc-password.
RPC_PROXY_ENV = {
    "PROXY_LISTEN": f"0.0.0.0:{RPC_PROXY_PORT}",
    "PROXY_ADMIN_LISTEN": f"0.0.0.0:{RPC_PROXY_ADMIN_PORT}",
    "PROXY_UPSTREAM_URL": f"http://127.0.0.1:{BITCOIND_RPC_PORT}",
    "PROXY_UPSTREAM_TIMEOUT": "30s",
    "PROXY_UPSTREAM_USER": "default",
    "PROXY_UPSTREAM_PASSWORD": "default",
    "PROXY_MAX_BODY_BYTES": "262144",
    "PROXY_EXTEND_ALLOWLIST": "",
    "PROXY_FILTER": "true",
    "PROXY_LOG_LEVEL": "info",
}

# INSTALL

APT_PACKAGES = [
    "aria2",  # Utility
    "tree",  # Utility
    "jq",  # Utility
    "prometheus-node-exporter",  # Metrics
    "python3-venv",  # For the Bitcoind Monitor venv
]

# Pinned so deploys are reproducible: a venv pulls from PyPI at install time, so
# without pins two deploys could land different versions. Bump deliberately.
PIP_PACKAGES = [
    "prometheus_client==0.25.0",  # For Bitcoind Monitor
    "python-bitcoinlib==0.12.2",  # For Bitcoind Monitor
    "riprova==0.3.1",  # For Bitcoind Monitor
]
