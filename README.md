# Bitcoin RPC Operator

The `bitcoin-rpc` operator runs a Bitcoin Core full node with a filtering RPC proxy, using the [Juju framework](https://juju.is/).

This repository is maintained by Dwellir - a blockchain and web3 infrastructure provider. For more information, see [Dwellir](https://dwellir.com/).

## Description

Bitcoin is the original public blockchain, securing a peer-to-peer electronic cash network through proof-of-work. [Bitcoin Core] is the reference full-node implementation that validates the chain and serves the network.

This charm runs a Bitcoin Core full node as a `bitcoind` systemd service. It manages the client's installation, configuration and lifecycle, and through Juju provides a simple interface for operations.

For more information on the client itself, go to [Bitcoin GitHub repository], or on the Bitcoin Core software, see the [Bitcoin Core] website.

## Usage

### Deploy

To deploy the `bitcoin-rpc` charm, using example values meant to open the node up for RPC access:

```bash
cd /path/to/bitcoin-rpc-operator
charmcraft pack

# Deploy archive node
juju deploy ./bitcoin-rpc_ubuntu@24.04-amd64.charm --base=ubuntu@24.04 --config version=31.0 --config rpc-proxy-version=0.2.1 --config service-args='-chain=main -server=1 -txindex=1 -rpcthreads=16 -rpcworkqueue=64 -debug=rpc' --config rpc-user=<rpc-user> --config rpc-password=<rpc-password>
# Deploy pruned node with ~10GB of data
juju deploy ./bitcoin-rpc_ubuntu@24.04-amd64.charm --base=ubuntu@24.04 --config version=31.0 --config rpc-proxy-version=0.2.1 --config service-args='-chain=main -server=1 -prune=10000 -rpcthreads=16 -rpcworkqueue=64 -debug=rpc' --config rpc-user=<rpc-user> --config rpc-password=<rpc-password>

# metrics
juju deploy prometheus2 prometheus
juju integrate bitcoin-rpc:node-prometheus prometheus:manual-jobs
juju integrate bitcoin-rpc:monitor-prometheus prometheus:manual-jobs
juju integrate bitcoin-rpc:rpc-proxy-prometheus prometheus:manual-jobs
```

NOTE: pick your own RPC credentials; a strong password can be generated with e.g. `openssl rand -hex 16`. See [User and password](#user-and-password) for how the credentials are used.

NOTE: do not pass `-rpcbind`, `-rpcallowip` or `-rpcport` in `service-args` — the charm strips them and pins `bitcoind`'s RPC to loopback on a fixed port, since the [RPC proxy](#rpc-proxy) is the node's only RPC front door. External access is configured via `rpc-proxy-listen` instead.

- The `version` config is **required** and has no default. Without it the Bitcoin client is not installed and the unit stays blocked. Pick any released version from the [Bitcoin client index](https://bitcoincore.org/bin/), e.g. `--config version=31.0` as above.
- The `rpc-proxy-version` config defaults to a published release of this repository. The matching `v<version>` release with a `bitcoin-rpc-proxy` asset must exist on this repository's GitHub releases page, or the unit goes into `BlockedStatus` with `bitcoind` left loopback-only (see [RPC proxy](#rpc-proxy)).
- Setting `-txindex=1` is optional, but recommended for full transaction indexing. This equals "archive mode" for other blockchains. If this is not set from deploy, one might need to run a reindex operation to make sure all transactions are indexed.

#### Pruning Level

The charm has no dedicated pruning config; the mode is selected with the `-prune` flag in `service-args`.

| Mode | Flag | Disk | Notes |
|---|---|---|---|
| Archive (default) | `-prune=0` or omit | ~700+ GB, growing | Keeps all blocks since genesis. The only mode compatible with `-txindex=1`. Serves historical blocks to peers and supports `getblock` at any height and full wallet rescans. |
| Pruned | `-prune=<MiB>` (min 550) | down to ~10–15 GB | Deletes old block/undo files once validated, keeping at least the target size. Incompatible with `-txindex`; `-reindex` forces a full re-download; serves only the last 288 blocks to peers. |
| Manual | `-prune=1` | operator-controlled | Pruning allowed but nothing is deleted automatically; trigger it with the `pruneblockchain <height>` RPC. |

Examples:

```bash
# Pruned node keeping roughly 50 GB of recent blocks
juju config bitcoin-rpc service-args='-chain=main -server=1 -prune=50000 ...'

# Manual pruning
juju config bitcoin-rpc service-args='-chain=main -server=1 -prune=1 ...'
```

Caveats:

- `-prune` conflicts with `-txindex=1`; remove the latter when enabling pruning. Switching an existing node from pruned back to archive requires a reindex (full re-download).
- The RPC proxy classifies `pruneblockchain` as dangerous, so the default allowlist blocks it. For manual pruning, either add it via `--config rpc-proxy-extend-allowlist=pruneblockchain` or call it from the unit itself with `bitcoin-cli` (`bitcoind`'s own RPC listens on loopback).
- `-blockfilterindex` and `-coinstatsindex` work alongside pruning; `-blocksonly=1` saves bandwidth, not disk, and is independent of pruning.

#### Bootstrap & Snapshots

A fresh node spends hours to days on initial block download (IBD). Two ways to shortcut that:

**AssumeUTXO (recommended).** Bitcoin Core's native fast-bootstrap: load a ~12 GB serialized UTXO-set snapshot via the `loadtxoutset` RPC and the node is usable at chain tip within minutes, while full history downloads and validates in the background. The snapshot hash is hardcoded in Bitcoin Core's source per release, so a tampered snapshot is rejected regardless of where it was downloaded from. Mainnet snapshot heights track releases: 840,000 (v28), 880,000 (v29), 910,000 (v30), 935,000 (v31). Snapshots are available from [bitcoin-snapshots.jaonoctus.dev](https://bitcoin-snapshots.jaonoctus.dev/) (torrent + direct), or dump your own from a synced node with `dumptxoutset`.

Run the load against the **live, running node** — do not stop the service or delete the chainstate first. `loadtxoutset` adds a second (snapshot) chainstate alongside the existing background-validation one; deleting the datadir would only throw away IBD progress that the background sync reuses.

```bash
# On the unit, after header sync. The txoutset RPCs are not in the proxy's
# allowlist, so drive bitcoin-cli against bitcoind's loopback RPC. bitcoin-cli is
# not on PATH; call it by full path as the bitcoin user.
juju ssh bitcoin-rpc/0

# bitcoind runs with -rpcuser/-rpcpassword (from the rpc-user/rpc-password configs),
# which DISABLES cookie auth, so bitcoin-cli must be handed the same credentials.
CLI="sudo -u bitcoin /home/bitcoin/bitcoin-cli -rpcuser=<rpc-user> -rpcpassword=<rpc-password>"

$CLI setnetworkactive false   # pause P2P during the load; node stays running
# Pass an ABSOLUTE path. A relative path is resolved against the datadir
# (/home/bitcoin/.bitcoin), and the file must be readable by the bitcoin user --
# it is bitcoind, not your shell, that opens it. If you downloaded the snapshot
# as another user, move it: sudo install -o bitcoin -g bitcoin -m644 <src> /home/bitcoin/
$CLI -rpcclienttimeout=0 loadtxoutset /home/bitcoin/utxo-935000.dat
$CLI setnetworkactive true
$CLI getchainstates   # monitor background validation
```

Caveats:

- **`loadtxoutset` blocks** for several minutes while it reads the ~12 GB file (hence `-rpcclienttimeout=0`); the node keeps serving throughout. Don't `restart`/`stop` bitcoind mid-load.
- Bandwidth cost is unchanged: full history still downloads in the background, only time-to-usable improves.
- Works fine with pruning; `-txindex` is not built until background validation completes.

**Pruned datadir snapshot.** For pruned deployments, [prunednode.today](https://prunednode.today/) publishes a complete `prune=550` datadir tarball, GPG-signed by the Specter team. Stop the node (`stop-node` action), extract the tarball into `/home/bitcoin/.bitcoin` (owned by the `bitcoin` user), then `start-node`; the node only syncs the blocks since the snapshot. Trust model: the imported history is never validated, so you are trusting the snapshot signer — verify the GPG signature, and prefer AssumeUTXO when full validation matters.

### Operations

#### Juju CLI

For the most effective interaction with the charm, use the Juju CLI. Some example operations (assuming the unit number is 0):

```bash
juju status
juju debug-log
juju run bitcoin-rpc/0 get-node-info
juju run bitcoin-rpc/0 get-node-help
juju run bitcoin-rpc/0 print-readme
juju run bitcoin-rpc/0 restart-node
juju run bitcoin-rpc/0 start-node
juju run bitcoin-rpc/0 stop-node
juju ssh bitcoin-rpc/0 -- sudo systemctl status bitcoind
juju ssh bitcoin-rpc/0 -- sudo systemctl status bitcoind-monitor
juju ssh bitcoin-rpc/0 -- sudo systemctl status bitcoin-rpc-proxy
```

Upgrading the Bitcoin client is as easy as setting the `version` config to any released version:

```bash
juju config bitcoin-rpc version=31.0
```

#### On the unit

- User: `bitcoin`
- Home directory: `/home/bitcoin`
- Default data directory: `/home/bitcoin/.bitcoin`
- Configuration file: `/home/bitcoin/.bitcoin/bitcoin.conf`
  - Note: this is not in use per default, but can be used to configure the Bitcoin client. Configurations from it **WILL** override any service arguments if used.
- Daemon binary: `/home/bitcoin/bitcoind`
- RPC client binary: `/home/bitcoin/bitcoin-cli`
- Service name: `bitcoind`
- Monitor service name: `bitcoind-monitor`
- RPC proxy service name: `bitcoin-rpc-proxy`

Both `bitcoind` and `bitcoin-cli` are extracted from the same release tarball and live in the home directory. They are not on the system `PATH`, so invoke `bitcoin-cli` by its full path as the `bitcoin` user. The client talks to `bitcoind`'s loopback RPC directly, bypassing the proxy's method allowlist, which is what makes admin RPCs like `loadtxoutset` and `pruneblockchain` reachable on the unit.

Because the charm sets `-rpcuser`/`-rpcpassword`, `bitcoind` disables cookie auth, so `bitcoin-cli` must be given the same credentials or it fails with "Could not locate RPC credentials":

```bash
sudo -u bitcoin /home/bitcoin/bitcoin-cli -rpcuser=<rpc-user> -rpcpassword=<rpc-password> getblockchaininfo
```

To avoid passing the flags on every call, append `rpcuser=`/`rpcpassword=` (same values) to `/home/bitcoin/.bitcoin/bitcoin.conf` — but note that `bitcoin.conf` overrides `service-args`, and remove the lines later if you don't want them to persist.

All Bitcoin client versions are available from the Bitcoin Core website: [Bitcoin client index](https://bitcoincore.org/bin/)

The Bitcoin RPC API reference: [RPC API reference](https://bitcoincore.org/en/doc/)

### Node RPC

#### User and password

`bitcoind` requires authentication on its JSON-RPC interface: every request must carry a username and password. The charm sets these on the node via the `rpc-user` and `rpc-password` configs (mapping to `bitcoind`'s `-rpcuser`/`-rpcpassword` flags).

If the credentials aren't acting as a real authentication layer — for example when running a public RPC node where a gateway in front attaches them to incoming requests — they don't need to be secure. Note that the password is stored in plain text in the charm's config, so treat the Juju model's config as sensitive.

#### curl examples

If the user and password are handled, making requests to a Bitcoin node is no different than with other blockchains. Here are some examples:

```bash
curl --user <rpc-user>:<rpc-password> -d '{"jsonrpc": "2.0", "id": "1", "method": "getblockchaininfo", "params": []}' -H 'content-type: application/json' http://<unit-ip>:8331

curl --user <rpc-user>:<rpc-password> -d '{"jsonrpc": "1.1", "id": "1", "method": "getbestblockhash", "params": []}' -H 'content-type: application/json' http://<unit-ip>:8331

curl -d '{"jsonrpc": "1.0", "id": "curltest", "method": "getblockstats", "params": ["00000000000000000000aa5be5529776f87e5d53d0201420af66cef91c88c4ca", ["time","height"]]}' -H 'content-type: application/json' http://<rpc-user>:<rpc-password>@<unit-ip>:8331
```

Note a few variations in these commands:
- Several `jsonrpc` versions are used, `1.0`, `1.1` and `2.0`. The Bitcoin node did not always support `2.0` but does from some time back ([source](https://github.com/bitcoin/bitcoin/pull/27101)).
- The user and password are set in different ways, either as a part of the URL or as a part of the request body.

#### RPC proxy

All external RPC traffic goes through the `bitcoin-rpc-proxy`, a small filtering service that always runs in front of `bitcoind`. `bitcoind`'s own JSON-RPC is pinned to loopback (`127.0.0.1`/`[::1]`) unconditionally, so the proxy is the node's only RPC front door. Consumers connect to the proxy's port (`rpc-proxy-listen`, default `8331`), never to `bitcoind`'s `8332`.

By default the proxy enforces a default-deny method allowlist (the SAFE baseline, widened via `rpc-proxy-extend-allowlist`). The single knob that opens the node up is `rpc-proxy-filter`:

- `rpc-proxy-filter=true` (default): only allowlisted methods reach `bitcoind`.
- `rpc-proxy-filter=false`: every method is forwarded ("fully open" node). This exposes dangerous methods such as `stop` and, unless `disable-wallet` is true, wallet methods. Even then `bitcoind` itself stays loopback-only behind the proxy.

The method classification behind the allowlist is documented in [docs/bitcoin-rpc-methods.md](./docs/bitcoin-rpc-methods.md), and the `bitcoind` JSON-RPC behavior the proxy builds against in [docs/bitcoind-api.md](./docs/bitcoind-api.md).

The proxy binary is installed by `rpc-proxy-version` (a published release asset). If it is missing, the unit goes `BlockedStatus` and the node is left **fail-closed**: `bitcoind` is loopback-only and unreachable from the network, rather than exposed.

This all-or-nothing model (always-on proxy, filtering as the only switch) is intentional: it keeps `bitcoind` off the network in every configuration and gives the charm a single control knob instead of juggling `bitcoind`'s own `-rpcbind`/`-rpcallowip`. If a future deployment genuinely needs a direct, proxy-less path to `bitcoind`, a charm developer can reintroduce one by removing the unconditional loopback pin in `utils.harden_service_args` and exposing `bitcoind`'s bind address as config; nothing else in the design depends on the pin being unconditional.

### Monitoring

The `bitcoin-rpc` charm provides Prometheus metrics interfaces. These can be scraped by Prometheus or other monitoring tools, the recommendation is to use the `prometheus2` charm for this. See the [deployment](#deploy) section for an example of how to deploy the charm with Prometheus metrics enabled.

The blockchain metrics are built by the [bitcoind-monitor.py](./templates/bitcoind-monitor.py) script from the [Bitcoin Prometheus exporter] repository, slightly modified. The script is run as a service on the unit and exposes the metrics on the unit's IP address and a specific port (set in [charm.py](./src/charm.py)).

## Known limitations

- **Mainnet only.** The RPC proxy and the monitor both target `bitcoind`'s mainnet RPC port (`8332`), which the charm pins via `-rpcport`. Selecting another network through `service-args` (`-testnet`, `-signet`, `-regtest`) is not supported: `bitcoind` would still listen on the pinned port, but the rest of the network switch (P2P port, data directory, allowlist relevance) is unhandled. See [docs/TODO.md](./docs/TODO.md) for the open research item.

## Development

The charm's lint, type and test suites are driven by [tox](https://tox.wiki/):

```bash
tox -e format      # ruff format + autofix
tox -e lint        # codespell + ruff checks
tox -e static      # pyright type checks
tox -e unit        # unit tests with coverage
tox -e integration # integration tests; needs a live Juju controller (e.g. local LXD)
```

CI runs `lint`, `static` and `unit` on every PR. The integration suite builds and deploys the charm for real, so it is not part of the PR lane; run it manually against a bootstrapped controller.

The bundled Go RPC proxy is a separate lane: see [bitcoin-rpc-proxy/README.md](./bitcoin-rpc-proxy/README.md) and its Makefile.

## Other resources

- [Bitcoin GitHub repository]
- [Bitcoin GitHub docs]
- [Bitcoin Core]
- [Bitcoin Prometheus exporter]

## License

This project is licensed under the Apache License 2.0; see [LICENSE](./LICENSE).

The vendored monitor script [templates/bitcoind-monitor.py](./templates/bitcoind-monitor.py) is from the [Bitcoin Prometheus exporter] and remains under its upstream BSD 3-Clause license; see [templates/LICENSE.bitcoind-monitor](./templates/LICENSE.bitcoind-monitor).

[Bitcoin GitHub repository]: https://github.com/bitcoin/bitcoin
[Bitcoin GitHub docs]: https://github.com/bitcoin/bitcoin/tree/master/doc
[Bitcoin Core]: https://bitcoincore.org/
[Bitcoin Prometheus exporter]: https://github.com/jvstein/bitcoin-prometheus-exporter
