#!/usr/bin/env bash
# Local dev environment for bitcoin-rpc-proxy: a mock bitcoind in Docker plus the
# proxy run on the host for fast rebuilds.
#
# Usage: ./dev/run.sh          (mock + build + run proxy on host, foreground)
#        ./dev/run.sh infra    (mock only; run the proxy yourself)
#
# Host ports use the 18xxx range to avoid collisions:
#   mock bitcoind  -> 18332   proxy main -> 18331   proxy admin -> 18360
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

PROXY_LISTEN="127.0.0.1:18331"
PROXY_ADMIN="127.0.0.1:18360"
PROXY_UPSTREAM="http://127.0.0.1:18332"

cd "$SCRIPT_DIR"

cleanup() {
	echo ""
	echo "==> Stopping mock-bitcoind..."
	docker compose -f "$SCRIPT_DIR/docker-compose.yaml" down
}
trap cleanup EXIT

echo "==> Starting mock-bitcoind (host :18332)..."
docker compose up -d --build --wait

if [[ "${1:-}" == "infra" ]]; then
	echo ""
	echo "Mock is ready (Ctrl+C to tear down). Run the proxy manually:"
	echo "  cd $REPO_DIR && make build"
	echo "  ./bin/bitcoin-rpc-proxy --listen $PROXY_LISTEN --admin-listen $PROXY_ADMIN --upstream-url $PROXY_UPSTREAM"
	echo ""
	echo "Then drive it:"
	echo "  ./dev/smoke-test.sh"
	echo ""
	# Block until Ctrl+C so the EXIT trap tears the mock down.
	sleep infinity &
	wait
fi

echo "==> Building proxy..."
cd "$REPO_DIR"
make build

echo "==> Starting proxy on $PROXY_LISTEN (admin $PROXY_ADMIN), upstream $PROXY_UPSTREAM"
echo "    Drive it from another shell with ./dev/smoke-test.sh (Ctrl+C here to stop)."
echo ""
./bin/bitcoin-rpc-proxy \
	--listen "$PROXY_LISTEN" \
	--admin-listen "$PROXY_ADMIN" \
	--upstream-url "$PROXY_UPSTREAM" \
	--log-level debug
