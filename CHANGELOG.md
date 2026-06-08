# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The charm and the bitcoin-rpc-proxy are released together under a single
version. Each release lists changes per vertical (`Charm` / `Proxy`); a
vertical with no changes states `No changes this release.`

## [Unreleased]

### Charm

- Fixed install hook failing on Ubuntu 24.04 with PEP 668
  `externally-managed-environment`: the monitor's pip dependencies
  (`prometheus_client`, `python-bitcoinlib`, `riprova`) now install into a
  dedicated, version-pinned venv instead of the system Python, and the
  `bitcoind-monitor` service runs from that venv.

### Proxy

- No changes this release.

## [0.1.0] - 2026-06-05

### Charm

- Initial release: a Juju machine charm that installs and operates a Bitcoin
  Core full node as the `bitcoind` systemd service on Ubuntu 24.04, with a
  `bitcoind-monitor` Prometheus exporter, node lifecycle actions
  (`start-node`, `stop-node`, `restart-node`, `get-node-info`), and
  configuration for client version, RPC credentials, and service arguments.

### Proxy

- Initial release: `bitcoin-rpc-proxy`, a filtering reverse proxy that is the
  node's only RPC front door (bitcoind stays loopback-only), with a method
  allowlist, configurable listen address, and an admin endpoint exposing
  health and Prometheus metrics.

[Unreleased]: https://github.com/dwellir-public/bitcoin-rpc-operator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dwellir-public/bitcoin-rpc-operator/releases/tag/v0.1.0
