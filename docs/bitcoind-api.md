# Bitcoin Core JSON-RPC Wire Protocol Reference

| Field | Value |
|---|---|
| Document date | 2026-06-03 |
| Repo commit | `9724fd1` |
| Bitcoin Core version anchor | **31.0** (source verified at git tag `v31.0`) |
| Purpose | Implementation-grade spec for the [`bitcoin-rpc-proxy`](../bitcoin-rpc-proxy/README.md) to parse, filter, and forward requests correctly |

> Companion docs: [`bitcoin-rpc-methods.md`](./bitcoin-rpc-methods.md) (method risk classification),
> the [`bitcoin-rpc-proxy` README](../bitcoin-rpc-proxy/README.md) (the proxy that builds against this spec).
> Regenerate against a live node when targeting a different version: `bitcoin-cli help`.

This is the exact behavior the proxy must reproduce or pass through. The proxy decides
allow/deny by reading the `method` field *before* forwarding; on deny it must synthesize a
response in the **same JSON-RPC version** the client used (§1). On allow it forwards the body
verbatim and returns bitcoind's response verbatim.

## 1. JSON-RPC version: hybrid 1.0/2.0, per-request

bitcoind supports JSON-RPC **1.0/1.1 (legacy)** and **2.0** on the same endpoint, selected
per request (`src/rpc/request.cpp`, `JSONRPCRequest::parse`; internal enum
`JSONRPCVersion{V1_LEGACY, V2}`).

Version detection:
- No `jsonrpc` field → **V1_LEGACY** (default).
- `"jsonrpc":"2.0"` → **V2**.
- `"jsonrpc":"1.0"` → V1_LEGACY (tolerated).
- `jsonrpc` present but not a string → throws `RPC_INVALID_REQUEST` ("jsonrpc field must be a string").
- Any other string (`"1.1"`, `"2.1"`, …) → throws `RPC_INVALID_REQUEST` ("JSON-RPC version not supported").

**Request object fields:**
- `method` — **required string**. Missing → `RPC_INVALID_REQUEST` "Missing method"; non-string → "Method must be a string".
- `params` — optional array, object, or null (null/absent ⇒ empty positional array). Other types → `RPC_INVALID_REQUEST` "Params must be an array or object".
- `id` — optional (§6).
- `jsonrpc` — optional, only `"1.0"`/`"2.0"`.

**Response object shape** (`JSONRPCReplyObj`):

| Field | V1_LEGACY | V2 |
|---|---|---|
| `jsonrpc` | absent | `"2.0"` |
| `result` on success | present | present |
| `error` on success | present, `null` | **absent** |
| `result` on error | present, `null` | **absent** |
| `error` on error | present (object) | present (object) |
| `id` | echoed if request had one | echoed if request had one |

In **V1** both `result` and `error` keys always appear (one is `null`). In **V2** exactly one
appears, and `jsonrpc:"2.0"` is added.

**Critical proxy gotcha — HTTP status is version-dependent.** In V1, RPC errors surface as HTTP
400/404/500 (§3). In V2, RPC errors return **HTTP 200** with the error in the body
(`src/httprpc.cpp`: `catch_errors = (m_json_version == V2)`). **Classify outcomes by parsing
the body's `error` field, never by HTTP status alone.** `JSONErrorReply` asserts it is never
called for V2.

## 2. HTTP transport

- **Method/path:** `POST` to `/` only. Non-POST → **405** `HTTP_BAD_METHOD` ("JSONRPC server handles only POST requests").
- **Multi-wallet routing:** a second handler at prefix `/wallet/` (`WALLET_ENDPOINT_BASE`, `src/wallet/rpc/util.cpp`). Wallet name = URL-decoded remainder after the prefix. Required when ≥2 wallets are loaded; otherwise `RPC_WALLET_NOT_SPECIFIED` (-19). Unknown/unloaded wallet → `RPC_WALLET_NOT_FOUND` (-18). **The proxy must forward the request path verbatim** so `/wallet/<name>` routing is preserved (moot under `-disablewallet`, but cheap to keep correct).
- **Headers:**
  - `Authorization: Basic <base64(user:pass)>` — required by bitcoind. Missing → **401** + `WWW-Authenticate`. Wrong creds → **401** after a deliberate 250 ms anti-brute-force delay. The proxy **passes this header through verbatim** (no auth of its own).
  - `Content-Type` — bitcoind does **not** validate the request content-type. Responses always set `Content-Type: application/json`.

## 3. HTTP status code mapping

`JSONErrorReply` (V1 only, `src/httprpc.cpp`) maps just two RPC codes; everything else is 500:

| Situation | HTTP status |
|---|---|
| Successful single call | **200** |
| V2 single call with RPC error | **200** (error in body) |
| V1 RPC error, code `RPC_INVALID_REQUEST` (-32600) | **400** |
| V1 RPC error, code `RPC_METHOD_NOT_FOUND` (-32601) | **404** |
| V1 RPC error, any other code (params/internal/parse/warmup/wallet/misc) | **500** |
| Malformed JSON body | **500** (V1, `RPC_PARSE_ERROR` default) |
| Non-POST method | **405** |
| Missing `Authorization` | **401** + `WWW-Authenticate` |
| Bad credentials | **401** (after 250 ms) |
| Whitelisted user → non-allowed method (single or batch) | **403** `HTTP_FORBIDDEN` |
| Single notification (no `id`) | **204** `HTTP_NO_CONTENT` (empty body) |
| Batch, all elements notifications (non-empty) | **204** |
| Batch with ≥1 non-notification | **200** |
| Empty array `[]` | **200** returning `[]` (deliberate back-compat) |
| Unknown path (not `/` or `/wallet/...`) | **404** (`src/httpserver.cpp`) |

`HTTPStatusCode` enum: `HTTP_OK=200, HTTP_NO_CONTENT=204, HTTP_BAD_REQUEST=400,
HTTP_UNAUTHORIZED=401, HTTP_FORBIDDEN=403, HTTP_NOT_FOUND=404, HTTP_BAD_METHOD=405,
HTTP_INTERNAL_SERVER_ERROR=500, HTTP_SERVICE_UNAVAILABLE=503`.

**Note:** 503 is in the enum but the JSON-RPC path never emits it. Warmup is an RPC error
`RPC_IN_WARMUP` (-28) → HTTP 500 (V1) / 200 (V2), **not** 503. **204 bodies are empty — do not
JSON-parse them.**

**bitcoind's own whitelist returns 403** for a disallowed method (single or batch). The proxy
mirrors this: deny → 403 (§Proxy alignment below).

## 4. Error object & error codes

Error object is always exactly `{"code": <int>, "message": "<string>"}` (`JSONRPCError`,
`src/rpc/request.cpp`). bitcoind does not populate a `data` field. Constants from
`src/rpc/protocol.h`:

**Standard JSON-RPC**: `RPC_INVALID_REQUEST` -32600, `RPC_METHOD_NOT_FOUND` -32601,
`RPC_INVALID_PARAMS` -32602, `RPC_INTERNAL_ERROR` -32603, `RPC_PARSE_ERROR` -32700.

**General**: `RPC_MISC_ERROR` -1, `RPC_TYPE_ERROR` -3, `RPC_INVALID_ADDRESS_OR_KEY` -5,
`RPC_OUT_OF_MEMORY` -7, `RPC_INVALID_PARAMETER` -8, `RPC_DATABASE_ERROR` -20,
`RPC_DESERIALIZATION_ERROR` -22, `RPC_VERIFY_ERROR` -25, `RPC_VERIFY_REJECTED` -26,
`RPC_VERIFY_ALREADY_IN_UTXO_SET` -27, `RPC_IN_WARMUP` -28, `RPC_METHOD_DEPRECATED` -32.

**P2P/client**: `RPC_CLIENT_NOT_CONNECTED` -9, `RPC_CLIENT_IN_INITIAL_DOWNLOAD` -10,
`RPC_CLIENT_NODE_ALREADY_ADDED` -23, `RPC_CLIENT_NODE_NOT_ADDED` -24,
`RPC_CLIENT_NODE_NOT_CONNECTED` -29, `RPC_CLIENT_INVALID_IP_OR_SUBNET` -30,
`RPC_CLIENT_P2P_DISABLED` -31, `RPC_CLIENT_MEMPOOL_DISABLED` -33,
`RPC_CLIENT_NODE_CAPACITY_REACHED` -34.

**Wallet**: `RPC_WALLET_ERROR` -4, `RPC_WALLET_INSUFFICIENT_FUNDS` -6,
`RPC_WALLET_INVALID_LABEL_NAME` -11, `RPC_WALLET_KEYPOOL_RAN_OUT` -12,
`RPC_WALLET_UNLOCK_NEEDED` -13, `RPC_WALLET_PASSPHRASE_INCORRECT` -14,
`RPC_WALLET_WRONG_ENC_STATE` -15, `RPC_WALLET_ENCRYPTION_FAILED` -16,
`RPC_WALLET_ALREADY_UNLOCKED` -17, `RPC_WALLET_NOT_FOUND` -18, `RPC_WALLET_NOT_SPECIFIED` -19,
`RPC_WALLET_ALREADY_LOADED` -35, `RPC_WALLET_ALREADY_EXISTS` -36.

(`RPC_FORBIDDEN_BY_SAFE_MODE` -2 is reserved/unused; -2 and -21 are historical gaps.)

## 5. Batch requests

- A batch is a **JSON array of request objects** (`valRequest.isArray()`).
- Response is a **JSON array in input order**, but **notification elements are omitted** — so
  clients (and the proxy, if it ever rewrites) must correlate by `id`, not position.
- **Per-element isolation in bitcoind:** each element runs with `catch_errors=true` in its own
  try/catch; a bad element yields an error object for that element only and does not fail the
  batch ("Batches never throw HTTP errors").
- **HTTP status:** 200 if any element produces a response; 204 if the non-empty batch is all
  notifications; `[]` returns `[]` at 200.
- **Whitelist pre-scan (the behavior the proxy emulates):** for a whitelisted user, bitcoind
  pre-scans the batch and rejects the **entire batch with 403** if any element's method is not
  allowed, *before* executing anything. A non-object element during pre-scan throws
  `RPC_INVALID_REQUEST` → HTTP 400 for the whole request.
- **No batch-size limit** in the RPC layer; only the HTTP body-size cap (libevent, ~32 MiB)
  applies.

## 6. id handling

- `id` is optional, stored as `std::optional<UniValue>`; **any** JSON type is accepted and
  echoed verbatim (string, number, null, even array/object — bitcoind does not type-check it).
- **Notification = the `id` key is absent.** An explicit `"id": null` is **not** a notification
  (key present) and is echoed as `"id": null`. Only a *missing* `id` suppresses the response
  (single → 204).
- Same echo rules for success and error replies.

## 7. params

- **Positional:** `params` as a JSON array.
- **Named:** `params` as a JSON object keyed by argument name.
- Missing/`null` ⇒ empty positional array. Other types → `RPC_INVALID_REQUEST`.
- All methods also accept a special named param `args` (array of leading positional values),
  allowing mixed named+positional (`doc/JSON-RPC-interface.md`).

## 8. Proxy alignment (how the proxy must behave)

Derived rules for `bitcoin-rpc-proxy`:

1. **Parse `method` from each call** (single object or each array element). Detect the request's
   JSON-RPC version from its `jsonrpc` field per §1.
2. **Deny → HTTP 403**, mirroring bitcoind's native whitelist. Body is a version-matched
   JSON-RPC error object using `RPC_METHOD_NOT_FOUND` (-32601):
   - V1: `{"result": null, "error": {"code": -32601, "message": "method '<m>' not allowed by proxy policy"}, "id": <echoed>}`
   - V2: `{"jsonrpc": "2.0", "error": {"code": -32601, "message": "..."}, "id": <echoed>}`
3. **Batch with any disallowed method → reject the whole batch with 403** (pre-scan, matching §5).
4. **Allow → forward the raw body and path verbatim**, pass through `Authorization`, return
   bitcoind's status and body unchanged. Do not re-serialize the allowed body.
5. Malformed JSON → 400; missing/non-string `method` → 403 (treat as not-allowed); body over
   cap → 413.
6. **Never log request or response bodies** (may contain PSBTs/signed txns).

### Source references
- `src/rpc/protocol.h` — `RPCErrorCode`, `HTTPStatusCode` enums.
- `src/rpc/request.cpp` — `JSONRPCRequest::parse`, `JSONRPCReplyObj`, `JSONRPCError`.
- `src/httprpc.cpp` — `HTTPReq_JSONRPC`, `JSONErrorReply`, batch loop, auth, 204/403/405.
- `src/wallet/rpc/util.cpp` — `WALLET_ENDPOINT_BASE`, wallet-name parsing.
- `src/httpserver.cpp` — handler registration, unknown-path 404, body-size cap.
- `doc/JSON-RPC-interface.md` — version semantics, multi-wallet, named params.

All values verified against the `v31.0` tag.
