# Local Development Environment

A black-box harness for `bitcoin-rpc-proxy`, complementing the Go unit/integration
tests. A mock bitcoind runs in Docker Compose; the proxy runs on the host so you get
fast rebuilds. `smoke-test.sh` drives the running binary and asserts its policy and
error behavior.

Scope: proxy + fake bitcoind only. The charm is exercised separately by the existing
`tox integration` (pytest-operator); this directory does not touch Juju.

## Prerequisites

- Docker with Compose v2
- Go (version matching `go.mod`)
- `curl` (for `smoke-test.sh`)

## Quick start

```bash
# Mock + build + run the proxy on the host (foreground)
./dev/run.sh

# In another shell, drive the running proxy:
./dev/smoke-test.sh
```

Or run the mock only and start the proxy yourself:

```bash
./dev/run.sh infra
# then, from the bitcoin-rpc-proxy/ dir:
make build
./bin/bitcoin-rpc-proxy --listen 127.0.0.1:18331 --admin-listen 127.0.0.1:18360 --upstream-url http://127.0.0.1:18332
```

## Tear down

`run.sh` stops the mock on exit (Ctrl+C). To stop it manually:

```bash
docker compose -f dev/docker-compose.yaml down
```

## Ports

Host ports use the `18xxx` range to avoid collisions with standard ports.

| Service | Host port | Notes |
|---------|-----------|-------|
| mock bitcoind (JSON-RPC) | 18332 | container `:8332`; proxy upstream |
| proxy main listener | 18331 | JSON-RPC entrypoint |
| proxy admin listener | 18360 | `/healthz`, `/health`, `/metrics` |

## smoke-test.sh

Assumes the proxy and mock are already up. Asserts (exits non-zero on any mismatch):

- allowed `getblockcount` -> 200, body forwarded (`result` present)
- `stop` -> 403 with JSON-RPC error `-32601`
- batch mixing an allowed and a denied method -> 403 whole-batch reject
- malformed JSON -> 400; missing `method` -> 403; body over the 262144-byte cap -> 413
- `GET /` -> 405
- admin `/healthz` -> 200, `/health` reports `upstream":"ok"`, `/metrics` exposes `btc_rpc_proxy_*`

Target a non-default deployment with env overrides:

```bash
PROXY_URL=http://127.0.0.1:18331 ADMIN_URL=http://127.0.0.1:18360 ./dev/smoke-test.sh
```

## mock-bitcoind

A stdlib HTTP server speaking just enough of bitcoind's wire protocol
(`docs/bitcoind-api.md`) for the proxy to forward against:

- POST-only; non-POST -> 405.
- Hybrid JSON-RPC 1.0/2.0 with version-matched reply shapes (V1 carries `result` +
  null `error`; V2 carries `jsonrpc:"2.0"` and only `result`/`error`).
- Single and batch requests; batches reply with a JSON array, HTTP 200.
- Canned `getblockcount` / `getblockchaininfo`; `ping`, `uptime`; a `stop` it
  acknowledges but never acts on (so a request reaching the mock is observable).
  Unknown methods -> `-32601`.
- Optional Basic auth: set `MOCK_RPC_AUTH=user:pass` (compose env) to require a
  matching `Authorization` header and return 401 otherwise, exercising the proxy's
  pass-through.

It executes nothing real; it is the integration-test spy as a standalone binary.
