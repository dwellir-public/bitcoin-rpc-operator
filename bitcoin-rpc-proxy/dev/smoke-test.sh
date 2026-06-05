#!/usr/bin/env bash
# Black-box smoke test for a running bitcoin-rpc-proxy. Drives the real binary over
# HTTP and asserts the proxy's policy/error behavior. Exits non-zero on
# any mismatch, so it is CI-usable.
#
# Assumes the proxy and its upstream mock are already up (start them with run.sh).
# Override the targets for a non-default deployment:
#   PROXY_URL=http://host:port ADMIN_URL=http://host:port ./dev/smoke-test.sh
set -uo pipefail

PROXY_URL="${PROXY_URL:-http://127.0.0.1:18331}"
ADMIN_URL="${ADMIN_URL:-http://127.0.0.1:18360}"

fail=0

# check NAME EXPECTED ACTUAL
check() {
	if [[ "$3" == "$2" ]]; then
		printf '  PASS  %-32s %s\n' "$1" "$3"
	else
		printf '  FAIL  %-32s expected %s, got %s\n' "$1" "$2" "$3"
		fail=1
	fi
}

# contains NAME NEEDLE HAYSTACK
contains() {
	if [[ "$3" == *"$2"* ]]; then
		printf '  PASS  %-32s contains %q\n' "$1" "$2"
	else
		printf '  FAIL  %-32s missing %q\n' "$1" "$2"
		fail=1
	fi
}

# Bodies are piped via stdin (--data-binary @-) so the oversize case does not hit
# the shell's ARG_MAX limit; printf is a builtin, so it does not exec either.
post_code() { printf '%s' "$1" | curl -s -o /dev/null -w '%{http_code}' -X POST "$PROXY_URL/" -H 'Content-Type: application/json' --data-binary @-; }
post_body() { printf '%s' "$1" | curl -s -X POST "$PROXY_URL/" -H 'Content-Type: application/json' --data-binary @-; }

echo "==> Proxy $PROXY_URL  admin $ADMIN_URL"

echo "-- policy"
check "allowed getblockcount -> 200" 200 "$(post_code '{"method":"getblockcount","id":1}')"
contains "allowed body forwarded" '"result"' "$(post_body '{"method":"getblockcount","id":1}')"
check "denied stop -> 403" 403 "$(post_code '{"method":"stop","id":1}')"
contains "deny body code -32601" '-32601' "$(post_body '{"method":"stop","id":1}')"
check "mixed batch -> 403" 403 "$(post_code '[{"method":"getblockcount","id":1},{"method":"stop","id":2}]')"

echo "-- malformed / limits / transport"
check "malformed JSON -> 400" 400 "$(post_code '{not json')"
check "missing method -> 403" 403 "$(post_code '{"id":1}')"
# Body just over the 262144-byte default cap -> 413.
big="$(head -c 300000 </dev/zero | tr '\0' a)"
check "oversize body -> 413" 413 "$(post_code "{\"method\":\"getblockcount\",\"id\":1,\"pad\":\"$big\"}")"
check "GET / -> 405" 405 "$(curl -s -o /dev/null -w '%{http_code}' "$PROXY_URL/")"

echo "-- admin"
check "/healthz -> 200" 200 "$(curl -s -o /dev/null -w '%{http_code}' "$ADMIN_URL/healthz")"
check "/health -> 200" 200 "$(curl -s -o /dev/null -w '%{http_code}' "$ADMIN_URL/health")"
contains "/health upstream ok" '"upstream":"ok"' "$(curl -s "$ADMIN_URL/health")"
contains "/metrics exposes btc_rpc_proxy_" 'btc_rpc_proxy_' "$(curl -s "$ADMIN_URL/metrics")"

echo ""
if [[ "$fail" -ne 0 ]]; then
	echo "SMOKE TEST FAILED"
	exit 1
fi
echo "SMOKE TEST PASSED"
