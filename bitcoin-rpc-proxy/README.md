# bitcoin-rpc-proxy

A small Go HTTP service that sits in front of Bitcoin Core's JSON-RPC, applies a
**default-deny method allowlist** (single and batch requests), and forwards
survivors to bitcoind on localhost. It is the control point external consumers
connect to; bitcoind's own RPC is pinned to loopback.

This service is deployed by the `bitcoin-rpc` charm in this repository (see the charm
[README](../README.md#rpc-proxy) for the operator-facing view). The wire-protocol
contract it is built against is [`docs/bitcoind-api.md`](../docs/bitcoind-api.md);
the method risk classification behind the baseline allowlist is
[`docs/bitcoin-rpc-methods.md`](../docs/bitcoin-rpc-methods.md).

## Why it exists

bitcoind's RPC surface includes methods that shut the node down (`stop`),
manipulate its chain view (`invalidateblock`, `submitblock`), isolate it on the
network (`setban`, `setnetworkactive`), exfiltrate keys or move funds (the entire
Wallet category, `signrawtransactionwithkey`), and exhaust resources
(`gettxoutsetinfo`, `scantxoutset`, `verifychain`). Consumers should reach only a
vetted set of read-only methods, with defense in depth and observability of
denials, without coupling the policy to bitcoind restarts.

## Architecture

```
                 exposed interface (private subnet)
   consumers ───────────────► [bitcoin-rpc-proxy] ──localhost──► bitcoind RPC :8332
                                allowlist (single+batch)          (-rpcbind=127.0.0.1
                                + metrics + logs                   -disablewallet*)
                              * when no wallet is needed
```

Defense in depth, layered:

- **L1 — network.** bitcoind's RPC is bound to loopback (`-rpcbind=127.0.0.1` and
  `[::1]`); it never listens on a routable address. The charm enforces this
  **unconditionally**, so the proxy is the only RPC front door in every
  configuration.
- **L2 — bitcoind native (deferred).** `-rpcwhitelist` is intentionally **not**
  configured. With bitcoind already loopback-bound (L1) and the proxy enforcing
  policy (L3), `-rpcwhitelist` would only guard a host-local bypass while adding
  username/allowlist coupling and a bitcoind-restart dependency. `-disablewallet`
  *is* applied by default (the charm's `disable-wallet` option) so wallet methods
  are unreachable regardless of the allowlist.
- **L3 — this proxy.** Parses each JSON-RPC call, applies the allowlist (including
  per-batch), logs and counts decisions, and forwards survivors.

Because bitcoind is loopback-only, **the proxy fails closed**: if it is down or its
binary is missing, consumers cannot reach bitcoind at all (rather than reaching it
unfiltered).

### Filtering can be turned off

The single knob that opens the node up is `--filter` / `PROXY_FILTER` (charm:
`rpc-proxy-filter`, default on). With filtering off, every method is forwarded — a
deliberately "open" node. bitcoind stays loopback-bound behind the proxy and, if
`disable-wallet` is set, wallet methods remain unreachable; but dangerous methods
such as `stop` then reach bitcoind. This is the intended escape hatch; leave
filtering on unless you specifically want an unfiltered node.

## Filtering policy

- **Effective allowlist = SAFE baseline ∪ `--extend-allowlist`.** Method names are
  case-sensitive (matching bitcoind).
- The SAFE baseline is a hardcoded constant in the binary (`internal/policy`), so a
  misconfigured deployment still defaults to safe. It is sourced from
  [`docs/bitcoin-rpc-methods.md` §5](../docs/bitcoin-rpc-methods.md); a unit test
  (`TestBaselineMatchesSafeTier`) asserts the two stay in sync — update both
  together.
- `--extend-allowlist` is comma-separated; whitespace trimmed, empty entries
  ignored, duplicates collapsed. Example to enable broadcast:
  `--extend-allowlist="sendrawtransaction"`.

## Behavior

### Endpoints

The proxy exposes two listen ports: the main JSON-RPC port (default `0.0.0.0:8331`)
serves the filtered RPC API plus health checks; the admin port (default
`127.0.0.1:8360`, opened to `0.0.0.0:8360` by the charm) serves Prometheus metrics
and the same health checks. Health is on both ports so a load balancer fronting the
RPC port can probe it without reaching the admin port.

| Endpoint | Port | Description |
|----------|------|-------------|
| `POST /` | Main (`8331`) | JSON-RPC, single or batch, filtered through the allowlist |
| `POST /wallet/<name>` | Main (`8331`) | Wallet-scoped JSON-RPC; path forwarded verbatim |
| `GET /healthz` | Main (`8331`) | Liveness probe -- plain text `ok` (200), always |
| `GET /health` | Main (`8331`) | Upstream-reachability detail (JSON) |
| `GET /metrics` | Admin (`8360`) | Prometheus scrape endpoint |
| `GET /healthz` | Admin (`8360`) | Liveness probe (also available on admin port) |
| `GET /health` | Admin (`8360`) | Upstream-reachability detail (also available on admin port) |

Any other method/path → 405 / 404.

**`/healthz`** -- liveness probe (the process is up)

- **200** `ok` (text/plain) -- the proxy is running. Reflects only process
  liveness, *not* upstream reachability.

**`/health`** -- operator-facing upstream detail (JSON)

- **200** `{"status":"ok","version":"...","upstream":"ok","height":860123}` --
  bitcoind reachable; `height` is from the periodic `getblockcount` probe (omitted
  until the first successful poll)
- **503** `{"status":"degraded","version":"...","upstream":"unreachable","error":"..."}`
  -- upstream unreachable past the threshold
- **503** `{"status":"starting","version":"...","upstream":"pending"}` -- no
  successful poll yet

Because `/healthz` is upstream-blind, alert on bitcoind reachability via `/health`
or the `btc_rpc_proxy_upstream_healthy` metric, not `/healthz`.

### Forwarding semantics

When **all** calls in an envelope are allowed, the original body is forwarded
byte-for-byte (not re-serialized) with the client's `Authorization` header; the
upstream status, body, and `Content-Type` are returned verbatim. A batch with any
disallowed call is rejected whole. The deny path never reaches upstream.

Status/error mapping the proxy emits itself (upstream statuses pass through
unchanged). [`docs/bitcoind-api.md` §8](../docs/bitcoind-api.md) is authoritative
for the exact, JSON-RPC-version-matched error bodies.

| Condition | HTTP | JSON-RPC error code |
|---|---|---|
| All methods allowed | (from upstream) | (from upstream) |
| Disallowed method (single or any batch element) | 403 | `-32601` |
| Missing/non-string `method` | 403 | treated as not-allowed |
| Body over `max-body-bytes` | 413 | `-32600` |
| Malformed JSON | 400 | `-32700` |
| Non-POST | 405 | (plain) |
| Upstream timeout | 504 | `-32603` |
| Upstream unreachable | 502 | `-32603` |

## Configuration

CLI flags with a per-flag env-var fallback; no config file. Precedence: built-in
default → `PROXY_*` env var → flag. The charm delivers these via
`EnvironmentFile=/etc/default/bitcoin-rpc-proxy`.

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--listen` | `PROXY_LISTEN` | `0.0.0.0:8331` | Main JSON-RPC listener (distinct from bitcoind 8332). |
| `--admin-listen` | `PROXY_ADMIN_LISTEN` | `127.0.0.1:8360` | `/healthz`, `/health`, `/metrics` (the charm opens this to `0.0.0.0:8360`). |
| `--upstream-url` | `PROXY_UPSTREAM_URL` | `http://127.0.0.1:8332` | bitcoind RPC, loopback. |
| `--upstream-timeout` | `PROXY_UPSTREAM_TIMEOUT` | `30s` | Per-request upstream timeout. |
| `--upstream-user` | `PROXY_UPSTREAM_USER` | `""` | Health-probe bitcoind auth user (probe only; forwarded requests carry the client's own auth). |
| `--upstream-password` | `PROXY_UPSTREAM_PASSWORD` | `""` | Health-probe bitcoind auth password. |
| `--max-body-bytes` | `PROXY_MAX_BODY_BYTES` | `262144` | Request body cap. |
| `--extend-allowlist` | `PROXY_EXTEND_ALLOWLIST` | `""` | Methods added to the SAFE baseline. |
| `--filter` | `PROXY_FILTER` | `true` | Enforce the allowlist; `false` forwards every method. |
| `--log-level` | `PROXY_LOG_LEVEL` / `LOG_LEVEL` | `info` | zerolog level. |

Plus `--version` (print build info and exit).

## Observability

Metrics (`/metrics`, admin port) — low cardinality, no per-IP labels:

- `btc_rpc_proxy_requests_total{decision,status}` — counter; `decision` =
  `allowed|denied`.
- `btc_rpc_proxy_denied_method_total{method}` — counter, denied methods only.
- `btc_rpc_proxy_request_duration_seconds` — histogram (no method label).
- `btc_rpc_proxy_upstream_healthy` — gauge 0/1.

Health: `GET /healthz` is always-200 liveness; `GET /health` polls upstream
`getblockcount` periodically and reports reachability (non-200 once upstream has
been unreachable past a threshold). With `--upstream-user`/`--upstream-password`
set, the probe authenticates and a 401/403 counts as unhealthy (it asserts real
RPC access); without them, any non-5xx response counts as reachable.

Logs (zerolog, structured): request-id, remote addr, batch size, method names,
decision, upstream status, duration. **Bodies are never logged** (they may carry
PSBTs / signed transactions).

## Scope and non-goals

The proxy is a **policy filter, not an auth boundary**. It assumes inbound traffic
has already passed an upstream proxy that terminates TLS and sanitizes input.
Deliberately out of scope (v1):

- **Authentication** — `Authorization` is passed through; bitcoind still owns auth.
- **TLS termination** — handled upstream.
- **Rate limiting** — deferred.
- **Per-consumer policies** — one global allowlist.
- **Argument-level validation** — only method names are checked.

## Build, test, release

- Go 1.26.3, golangci-lint 2.12.2. `Makefile` targets: `build`, `run`, `test`,
  `lint`, `fmt`. Build injects version metadata via `-ldflags` from the repo-root
  `VERSION` file + git.
- Cross-compile target is `GOOS=linux GOARCH=amd64`, static (`CGO_ENABLED=0`); the
  charm host is Ubuntu 24.04 amd64.
- The proxy is released together with the charm under a single repo version by
  `.github/workflows/release.yml`: GitHub Releases tagged `v<version>` with the
  asset `bitcoin-rpc-proxy-<version>-linux-amd64`. The charm's `rpc-proxy-version`
  config selects which asset to install (see `RPC_PROXY_DL_URL` in
  `src/constants.py`).

A Docker-based black-box harness for local development lives in [`dev/`](./dev/).
