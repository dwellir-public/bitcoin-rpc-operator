# RPC Proxy Short-TTL Response Cache

> [!WARNING]
> **DO NOT IMPLEMENT UNTIL NEEDED.** This plan is parked for a future need (e.g.
> allowlisting verbose `getrawmempool` for Chainflip and seeing real upstream-cost
> pressure). It adds a caching layer, config surface, and charm plumbing that are
> pure overhead until that need is concrete. Build it only when a live deployment
> requires it — until then, leave it unimplemented.

- **Date:** 2026-07-21
- **Commit:** 8d56ccb
- **Branch:** main
- **Status:** parked — implement only when needed

## Problem

`getrawmempool verbose=true` is a cheap-to-send, expensive-to-serve method: response
construction is linear in mempool size, and at default `-maxmempool=300` saturation
(~100-150k transactions) a single call costs multiple seconds of CPU and produces a
60-120 MB body. Allowlisting the method (requested for Chainflip compatibility) exposes
this cost to every caller at full rate; the general Dwellir proxy rate-limits per key
but cannot collapse identical concurrent calls into one upstream execution.

The response depends only on node-global mempool state (no per-caller variance) and its
consumers tolerate seconds of staleness by design. A short-TTL cache with single-flight
deduplication caps upstream cost at one expensive build per TTL window regardless of
caller count, without breaking `verbose=true` the way parameter filtering would.

## Goals

- Cache single (non-batch) responses for an operator-configured method set, default off,
  with an optional per-method TTL override.
- Single-flight: concurrent misses for the same key share one upstream fetch.
- Per-caller correctness: cached results are re-enveloped with each caller's JSON-RPC
  `id` and version.
- Configurable via proxy flags/env and charm config, following existing patterns.
- Prometheus visibility into hit/miss ratio.

## Non-goals

- Parameter-level policy (filtering or rewriting params) — explicitly shelved.
- Caching batch requests, error responses, or non-200 upstream responses.
- Cache invalidation on mempool/chain events (ZMQ); TTL expiry is the only mechanism.
- LRU eviction; a total-byte admission budget with expired-entry sweeping suffices
  for the intended method set.

## Design overview

A new `internal/cache` package holds a TTL cache with single-flight semantics. The
handler consults it after the policy check: a request is cache-eligible when it is a
single (non-batch) call that carries an `id`, has no caller `Authorization` header,
and whose method is in the configured cache set. Notifications keep bitcoind's
204/empty-body semantics, and authenticated callers keep per-request auth and
per-user whitelist enforcement by bitcoind (`upstream.Client.Forward` passes a
caller-supplied `Authorization` through; proxy credentials are injected only when
the caller sent none — `internal/upstream/client.go:85`). On a hit, the
handler synthesizes a version-matched success reply from the stored `result` bytes and
the caller's own `id`. On a miss, one goroutine forwards upstream while concurrent
same-key requests wait and share the outcome; the result is stored only if the upstream
reply is a clean success. Everything else (batches, uncached methods, errors) takes the
existing verbatim path untouched.

Caching is off unless `--cache-methods` is non-empty, so default behavior is unchanged.

## Detailed design

### 1. `internal/rpc`: capture params, add success-reply builder

Two gaps today: `Call` does not carry params (`rawCall` at `internal/rpc/rpc.go:57`
only extracts `jsonrpc`/`method`/`id`), and only error replies can be synthesized
(`replyV1`/`replyV2` hardcode an `Error` field; there is no result envelope).

- Add `Params json.RawMessage` to `rawCall` and `Call` (`rpc.go:42`), populated in
  `parseElement` (`rpc.go:90`). Raw bytes, no interpretation — used only as cache-key
  material.
- Add a success builder mirroring `ErrorReply` (`rpc.go:148`):

  ```go
  // ResultReply builds a version-matched success reply carrying result and the
  // call's id (null when absent). V1 shape: {"result":...,"error":null,"id":...};
  // V2 shape: {"jsonrpc":"2.0","result":...,"id":...}.
  func (c Call) ResultReply(result json.RawMessage) json.RawMessage
  ```

  Internally: `successV1{Result json.RawMessage, Error any, ID json.RawMessage}` and
  `successV2{JSONRPC string, Result json.RawMessage, ID json.RawMessage}` structs next
  to the existing reply types (`rpc.go:129-146`).

### 2. `internal/cache`: TTL store with single-flight

New package, no third-party deps (`golang.org/x/sync` is absent from `go.mod`; a
hand-rolled in-flight map is small and keeps the module lean).

```go
// Cache is a short-TTL, single-flight cache of JSON-RPC result payloads keyed by
// path+method+params. Each method carries its own TTL. The zero-method Cache
// (nil) is a no-op.
type Cache struct {
    methods  map[string]time.Duration // method -> TTL
    maxBytes int64         // total budget for stored result bytes
    fills    chan struct{} // semaphore bounding concurrent upstream fetches

    mu       sync.Mutex
    bytes    int64                // stored result bytes across entries
    entries  map[string]entry     // key -> stored result + expiry
    inflight map[string]*flight   // key -> pending fetch
}

type entry struct {
    result  json.RawMessage
    expires time.Time
}

// Outcome classifies how Do satisfied a call, for metrics.
type Outcome int

const (
    Hit    Outcome = iota // served from a fresh entry
    Miss                  // this caller ran the upstream fetch
    Shared                // joined another caller's in-flight fetch
)

func New(methods map[string]time.Duration, maxBytes int64) *Cache

// Cacheable reports whether a single call to method is cache-eligible.
func (c *Cache) Cacheable(method string) bool

// Key derives the cache key: request path, method, and the raw params bytes
// (whitespace-trimmed). Path is included so `/` and `/wallet/<name>` scopes
// never collide. Formatting or param-order differences produce distinct keys;
// that only costs an extra upstream call.
func Key(path string, call rpc.Call) string

// Result is what Do returns. OK selects which fields are meaningful:
//   OK == true  -> Result holds cacheable result bytes (from a fresh entry, this
//                  caller's fetch, or a shared flight); re-envelope per caller.
//   OK == false -> the fetch ran but its outcome was uncacheable or failed; Resp
//                  and Err carry it verbatim for the caller to relay. A waiter
//                  whose ctx expired mid-wait also lands here with Err == ctx.Err().
// Outcome is always set, for metrics.
type Result struct {
    Result  json.RawMessage
    Resp    *upstream.Response
    Err     error
    Outcome Outcome
    OK      bool
}

// Do returns the cached result for key, or runs fetch exactly once across
// concurrent callers (bounded by the fill semaphore) and caches a successful
// result under method's TTL. ctx lets a waiter stop waiting without cancelling
// the shared fetch.
func (c *Cache) Do(ctx context.Context, method, key string, fetch func() (*upstream.Response, error)) Result
```

Mechanics:

- `Do` under `mu`: fresh entry → return it (`Hit`). Existing `flight` → release `mu`
  and `select` on the flight's done channel and `ctx.Done()`, so a disconnected or
  timed-out waiter unblocks immediately while the shared fetch keeps running for
  the others (`Shared`, or `ctx.Err()`). Otherwise register a `flight`, release
  `mu`, acquire a fill slot, run `fetch`, publish the outcome to waiters (`Miss`),
  and store the result when cacheable (see step 3's storability rule).
- Admission: before storing, sweep expired entries (subtracting their sizes from
  `bytes`) — sweeping only on exact-key lookup would let dead one-shot keys
  permanently exhaust the budget. Store only if `bytes + len(result) <= maxBytes`;
  an oversized result is served to its callers but not retained.
- `maxBytes` is a fixed constant (256 MiB), not a flag — enough for a couple of
  worst-case verbose `getrawmempool` blobs while bounding what param-spraying can
  pin in memory. The fill semaphore (constant, 4 slots) bounds how many large
  upstream responses can be in flight for cacheable methods at once, since
  distinct keys each get their own flight.
- A `flight`'s outcome is the full `(*upstream.Response, error)` pair; only the
  extracted `result` is persisted.

### 3. Storability: extract `result` from the upstream response

Store only when: upstream status is 200, the body is a JSON object whose `error` field
is absent or null, and `result` is present. Parse with a minimal struct
(`struct { Result, Error json.RawMessage }`) at store time — once per TTL window, not
per caller. Anything else (non-200, JSON-RPC error, unparseable body) is returned to
the caller verbatim and not cached, so callers never see stale errors and error
semantics stay bitcoind's own.

### 4. `internal/handler`: consult the cache

`RPC` gains a `cache *cache.Cache` field (nil = disabled), threaded through `NewRPC`
(`internal/handler/rpc.go:54`, call site `internal/app/app.go:41`).

In `ServeHTTP` (`rpc.go:58`) after the `anyDenied` check:

```go
if !env.IsBatch &&
    env.Calls[0].HasID &&
    r.Header.Get("Authorization") == "" &&
    h.cache.Cacheable(env.Calls[0].Method) {
    h.serveCached(w, r, env, start)
    return
}
h.forward(w, r, env, start)
```

The extra guards keep bitcoind authoritative where the cache cannot be: id-less
notifications get bitcoind's 204/empty-body reply (`docs/bitcoind-api.md`, §status
codes — a cached 200 `ResultReply` would violate notification semantics), and
requests with their own `Authorization` are forwarded so bitcoind authenticates
them and applies per-user whitelists. Cached traffic is exactly the anonymous flow
that uses the proxy's injected upstream credentials.

**Misconfiguration warning (runtime).** `cache-methods` and the allowlist are
independent knobs: with `filter` enabled, a cache-method that is not allowlisted is
denied by `anyDenied` (`rpc.go:77`) before the cache branch is ever reached, so
caching for it silently never engages. In `deny` (`rpc.go:132`), when `filter` is on
and a denied call's method is in the cache set, log a warning naming the method —
caching is configured for a method the allowlist blocks. This is the runtime half of
the startup check in §6; keep it rate-unbounded-safe (it fires only on the denied
path, which already logs per request).

`serveCached` calls `cache.Do(r.Context(), method, key, fetch)` where the fetch
closure runs `h.up.Forward(context.WithoutCancel(r.Context()), ...)` — detached from
the leader request's context so one caller disconnecting mid-fetch does not fail the
waiters sharing the flight (`upstream.Client.Forward` at
`internal/upstream/client.go:91` enforces its own timeout via the HTTP client).
Outcomes:

- `res.OK == true` (any `Outcome`): record metrics (`decisionAllowed`, 200, plus
  the cache outcome), write `env.Calls[0].ResultReply(res.Result)` with
  `Content-Type: application/json`, log with the outcome. `ResultReply` re-marshals
  the stored result bytes per caller (cost linear in body size × caller count);
  accepted for now, revisit only if hit-path CPU becomes a measured problem.
- `res.OK == false`, `ctx.Err() != nil` (waiter gave up): nothing useful to write;
  log and return.
- `res.OK == false`, `res.Err != nil` (fetch failed): existing `handleUpstreamError`
  path (`rpc.go:118`).
- `res.OK == false`, `res.Resp` set but uncacheable (non-200 or JSON-RPC error):
  parse the bitcoind error envelope (`struct { Error *errObj }`) from `res.Resp.Body`
  and re-envelope per caller as `env.Calls[0].ErrorReply(code, message)` with the
  upstream HTTP status — waiters must not receive the leader's `id` (identical
  method+params does not imply identical ids). If the body does not parse as a
  JSON-RPC error object, relay it verbatim as a last resort.

### 5. Metrics

Extend the `Recorder` interface (`internal/handler/rpc.go:37`) with
`RecordCache(outcome string)` and add to `Metrics`
(`internal/handler/metrics.go:14`):

```go
cache *prometheus.CounterVec // btc_rpc_proxy_cache_total{outcome="hit"|"miss"|"shared"}
```

`shared` (coalesced waiter) is deliberately its own outcome: it is upstream work
avoided like a hit, but counting it as one would overstate entry freshness.

registered in `NewMetrics` (`metrics.go:23`) alongside the existing
`btc_rpc_proxy_*` metrics. No counter increments when caching is disabled.

### 6. `internal/app`: config flags

Follow the existing pattern in `Config`/`Parse` (`internal/app/config.go:26,42`):

- `--cache-methods` / `PROXY_CACHE_METHODS`: comma-separated `method[=ttl]` entries,
  default `""` (disabled). A bare `method` uses the default TTL; `method=60s` (Go
  duration syntax) overrides it per method, e.g.
  `getrawmempool,getblockstats=60s`. Reuse `policy.ParseExtendList`
  (`internal/policy/policy.go:84`) for the comma split, then split each entry on the
  first `=` and `time.ParseDuration` the override. Parsing yields the
  `map[string]time.Duration` handed to `cache.New`.
- `--cache-ttl` / `PROXY_CACHE_TTL`: `time.Duration`, default `5s`, via
  `applyEnvDuration` (`config.go:144`). Applies to entries without an explicit TTL.
- `validate()` (`config.go:102`): `cache-ttl` and every per-method TTL must be
  positive; a malformed entry (bad duration, empty method) is a startup error, not
  silently skipped.
- `App.New` (`internal/app/app.go:36`): build `cache.New(...)` when the method list is
  non-empty, else pass nil to `NewRPC`.
- **Startup warning.** After the allowlist and cache are built, if `Filter` is on, log
  a warning for every cache-method not in the effective allowlist (baseline ∪
  `ExtendAllowlist`) — such a method can never be served from cache because it is
  denied first. Non-fatal, not a `validate()` error: the same method set can be
  legitimate under a `filter=false` deployment, so this is a warning, not a startup
  failure.

Confirm `applyEnvString` treats an empty env value as unset (the charm env file always
writes every key), matching how `PROXY_EXTEND_ALLOWLIST=""` behaves today.

### 7. Charm plumbing

Follow the `rpc-proxy-extend-allowlist` pattern end to end:

- `charmcraft.yaml` (options block, after `rpc-proxy-extend-allowlist` at :103):
  `rpc-proxy-cache-methods` (string, default `""`, "comma-separated `method[=ttl]`
  entries, e.g. `getrawmempool,getblockstats=60s`; each method must also be
  allowlisted (baseline or `rpc-proxy-extend-allowlist`) when filtering is on, or it
  is denied before the cache is consulted") and `rpc-proxy-cache-ttl`
  (string, default `""`, "default Go duration for entries without their own TTL,
  e.g. 5s; empty keeps the proxy default"). Descriptions note the
  `--flag / PROXY_*` mapping like the existing options.
- `src/constants.py` `RPC_PROXY_ENV` (:84): add `"PROXY_CACHE_METHODS": ""` and
  `"PROXY_CACHE_TTL": "5s"`.
- `src/utils.py` `write_rpc_proxy_env_file` (:306): add both keys to the `overrides`
  dict. The truthy-override behavior (:325) is correct here — empty config keeps the
  default, and neither option has a meaningful falsy override (unlike `PROXY_FILTER`).
- `src/charm.py`: the env file is only rewritten (and the proxy restarted) when
  `_rpc_proxy_config_changed()` reports a tracked option changed. Add both options
  to `_TRACKED_CONFIG` (:33), the stored-state initialization (:90), the
  `_rpc_proxy_config_changed` comparison (:191), and the post-reconcile stored
  assignments (:179). Without this, changing either option on a running unit
  silently does nothing.

## Affected files

| File | Change |
|---|---|
| `bitcoin-rpc-proxy/internal/rpc/rpc.go` | `Call.Params`, `ResultReply` + success reply structs |
| `bitcoin-rpc-proxy/internal/cache/cache.go` | new package: TTL store, single-flight, key derivation |
| `bitcoin-rpc-proxy/internal/handler/rpc.go` | cache field, eligibility branch, `serveCached`, `Recorder.RecordCache` |
| `bitcoin-rpc-proxy/internal/handler/metrics.go` | `btc_rpc_proxy_cache_total` counter |
| `bitcoin-rpc-proxy/internal/app/config.go` | `--cache-methods`, `--cache-ttl` flags + validation |
| `bitcoin-rpc-proxy/internal/app/app.go` | construct cache, thread into `NewRPC` |
| `charmcraft.yaml` | `rpc-proxy-cache-methods`, `rpc-proxy-cache-ttl` options |
| `src/constants.py` | `RPC_PROXY_ENV` defaults for the two new vars |
| `src/utils.py` | env-file overrides for the two new vars |
| `src/charm.py` | track both options in `_TRACKED_CONFIG` + stored state |
| `bitcoin-rpc-proxy/internal/{cache,handler,app,rpc}/*_test.go` | new/extended tests |
| `tests/unit/test_rpc_proxy.py`, `tests/unit/test_charm.py` | env-file override + config-tracking assertions |
| `docs/bitcoin-rpc-methods.md` | note caching as the mitigation for verbose `getrawmempool`; state that a cached method must also be allowlisted under `filter` |

## Edge cases & safety

- **Id rewriting.** Cached results are re-enveloped per caller via `ResultReply`; the
  upstream body (which embeds the leader's `id`) is never replayed from cache. V1
  callers get `{"result":...,"error":null,"id":...}`, V2 callers get the 2.0 shape,
  matching the version conventions already used for error replies.
- **Notifications.** Excluded from caching (`HasID` required). bitcoind answers an
  id-less single request with HTTP 204 and an empty body (`docs/bitcoind-api.md`);
  serving a cached 200 body would break that contract, so notifications always
  forward.
- **Authentication and path scope.** Requests carrying their own `Authorization`
  bypass the cache entirely, so bitcoind keeps authenticating callers and enforcing
  per-user `-rpcwhitelist` semantics; only the anonymous flow using the proxy's
  injected upstream credentials is cached. The request path is part of the cache
  key, so `/` and `/wallet/<name>` scopes cannot collide.
- **Shared error outcomes.** Waiters on a failed flight must not receive the
  leader's `id` (same method+params does not imply same id). Uncacheable JSON
  outcomes are re-enveloped per caller from the parsed bitcoind error object via
  `ErrorReply`; only a body that fails to parse as a JSON-RPC error is relayed
  verbatim.
- **Leader disconnect / waiter disconnect.** The fetch uses `context.WithoutCancel`,
  so a leader hanging up mid-fetch cannot fail the flight for its waiters, and each
  waiter selects on its own request context so it can stop waiting without
  cancelling the shared fetch; the upstream timeout still bounds the fetch.
- **Memory.** Stored results are bounded by the 256 MiB byte budget with an
  expired-entry sweep before admission (lookup-time-only cleanup would let dead
  one-shot keys exhaust the budget permanently). Transient memory — concurrent
  large upstream bodies before extraction — is bounded by the 4-slot fill
  semaphore. Worst case is therefore the budget plus a few in-flight blobs, not
  entries × blob size.
- **Staleness.** TTL-bounded per method (default 5 s). Acceptable for mempool/fee
  data by design; methods over immutable data (e.g. `getblockstats` for confirmed
  heights) tolerate much longer TTLs via the `method=ttl` form. Operators choosing
  to cache other methods own that trade-off — the option docs must say "only add
  methods whose responses tolerate that method's TTL of staleness".
- **Behavior contract.** With `--cache-methods` empty (default), no code path
  changes: no params captured means no behavior change in `Parse` consumers, and the
  handler branch short-circuits on a nil cache.

## Testing

Go (`go test ./...` in `bitcoin-rpc-proxy/`):

- `internal/cache`: TTL expiry, hit/miss/shared outcome sequencing, single-flight
  coalescing (N concurrent `Do`s → one fetch), uncacheable outcomes not stored,
  byte-budget admission (oversized result served but not stored), expired-entry
  sweep frees budget for new keys, fill-semaphore bounds concurrent fetches, waiter
  context cancellation leaves the flight running, key distinctness for differing
  params and paths.
- `internal/rpc`: `ResultReply` V1/V2 shapes, null-id echo, params capture.
- `internal/handler`: hit returns caller-id-rewritten success; miss forwards and
  caches; second request within TTL does not hit upstream (count fetches with a fake
  upstream); batch, uncached methods, notifications (no id), and requests with an
  `Authorization` header all bypass; upstream JSON-RPC error is re-enveloped with
  each caller's id and not cached; metrics record hit/miss/shared; a denied call
  whose method is in the cache set (filter on, method not allowlisted) logs the
  misconfiguration warning.
- `internal/app`: flag/env parsing precedence for the two new options; `method=ttl`
  entry parsing (bare entry gets default TTL, override wins, malformed entry errors);
  ttl validation; nil cache when methods empty; startup warning emitted when a
  cache-method is absent from the effective allowlist under `filter` (and not emitted
  when allowlisted or when `filter` is off).

Charm (`tox -e unit`):

- `tests/unit/test_rpc_proxy.py`: env file contains defaults when unset and override
  values when set, following `test_write_env_file_applies_config_overrides_and_keeps_defaults` (:119).
- `tests/unit/test_charm.py`: changing either cache option triggers proxy
  reconciliation (tracked-config path); unchanged config does not.

## Acceptance criteria

- `tox -e unit`, `tox -e lint`, `tox -e static`, and `go test ./...` pass.
- With `--cache-methods=getrawmempool --cache-ttl=5s`, two sequential identical
  `getrawmempool` requests inside 5 s produce exactly one upstream call, and each
  caller's response carries its own `id`.
- N concurrent identical requests produce exactly one upstream call.
- With `--cache-methods` unset, proxy behavior is byte-identical to today for all
  request shapes (existing handler tests unchanged and passing).
- `btc_rpc_proxy_cache_total{outcome}` appears on the metrics endpoint when caching
  is enabled.
- Changing `rpc-proxy-cache-methods` or `rpc-proxy-cache-ttl` on a deployed unit
  rewrites the env file and restarts the proxy (config-changed reconciliation).
- Requests with an `Authorization` header or without an `id` are never served from
  cache.

## Future work (out of scope for this change)

- **Param-value-aware cache targets.** The cache keys on raw params, so
  `getrawmempool verbose=true` and `verbose=false` land on distinct keys and both
  cache — the cheap non-verbose variant needlessly churns the byte budget. Accepted
  to start. A richer target syntax could later gate on param values (cache only
  `verbose=true`, let `verbose=false` bypass). Stretch; builds on the shelved
  parameter-level policy in Non-goals.
- **Durable-doc write-up after implementation.** Once shipped, document two
  operational limits in a durable doc (`docs/operations/` or `docs/architecture/`):
  (a) the global 4-slot fill semaphore serializes concurrent *distinct-key*
  cacheable misses, so under param-spraying the 5th+ concurrent miss queues (bounds
  memory at the cost of latency); (b) the hit path re-marshals stored result bytes
  per caller (cost linear in body size × caller count). Both are accepted trade-offs
  now, worth recording so a future operator or maintainer does not rediscover them.

## Open questions

- Should the operator docs recommend caching `getblockstats` (now viable with a long
  per-method TTL, e.g. `getblockstats=300s`, since confirmed-height stats are
  immutable)? Arbitrary-height keys still churn the byte budget under scraping, so
  the gain depends on the traffic shape. Decide when writing the operator docs.
