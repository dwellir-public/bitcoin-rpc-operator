import base64
import json
import os
import shlex
from pathlib import Path

import jubilant
import pytest

APP = "bitcoin-rpc"
UNIT = f"{APP}/0"
VERSION = os.getenv("BITCOIN_VERSION", "31.0")
UPDATED_VERSION = os.getenv("BITCOIN_UPDATED_VERSION", "")
REGTEST_GENESIS = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
METADATA_PATH = "/tmp/dwellir-metadata-uploader/bitcoin-rpc-0.json"

S3_CAPTURE_SERVER = b"""\
import pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_PUT(self):
        size = int(self.headers.get('content-length', '0'))
        pathlib.Path('/tmp/bitcoin-metadata-s3.json').write_bytes(self.rfile.read(size))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass

HTTPServer(('127.0.0.1', 19000), Handler).serve_forever()
"""


def _wait_active(juju: jubilant.Juju, timeout: int = 1800) -> None:
    juju.wait(lambda status: jubilant.all_active(status, APP), timeout=timeout, successes=3)


def _payload(juju: jubilant.Juju, path: str = METADATA_PATH) -> dict:
    return json.loads(juju.ssh(UNIT, f"sudo cat {shlex.quote(path)}"))


def test_regtest_runtime_metadata_and_actions(charm: Path, juju: jubilant.Juju):
    """Validate identity, effective settings, indexes, resources, and safe actions."""
    juju.deploy(
        charm,
        app=APP,
        base="ubuntu@24.04",
        config={
            "version": VERSION,
            "rpc-user": "integration-user",
            "rpc-password": "integration-password",
            "service-args": (
                "-regtest=1 -server=1 -networkactive=0 -txindex=1 "
                "-blockfilterindex=1 -zmqpubrawblock=tcp://127.0.0.1:28332"
            ),
        },
    )
    _wait_active(juju)

    payload = _payload(juju)
    assert payload["blockchain"]["blockchain_ecosystem"] == "bitcoin"
    assert payload["blockchain"]["blockchain_network_name"] == "Bitcoin regtest"
    assert payload["blockchain"]["client_name"] == "bitcoin-core"
    assert payload["blockchain"]["client_version"].startswith(VERSION)
    assert payload["blockchain"]["binary_path"] == "/home/bitcoin/bitcoind"
    assert payload["blockchain"]["genesis_hash"] == REGTEST_GENESIS
    assert "integration-password" not in json.dumps(payload)
    bitcoin = payload["bitcoin"]
    assert bitcoin["chain"] == "regtest"
    assert bitcoin["network_magic"] == "fabfb5da"
    assert bitcoin["effective_flags"]["txindex"] == "1"
    assert bitcoin["effective_flags"]["blockfilterindex"] == "1"
    assert "rpcpassword" not in bitcoin["effective_flags"]
    assert bitcoin["ports"] == {
        "p2p": 18444,
        "rpc_internal": 8332,
        "rpc_proxy": 8331,
        "zmq": [28332],
    }
    assert bitcoin["pruning"]["enabled"] is False
    assert "txindex" in bitcoin["indexes"]
    assert set(payload["resource_limits"]) == {
        "memory_max_bytes",
        "memory_high_bytes",
        "cpu_quota_percent",
        "tasks_max",
    }

    info = juju.run(UNIT, "get-node-info", wait=300)
    assert info.status == "completed"
    assert "integration-password" not in json.dumps(info.results)
    assert info.results["rpc-proxy-running"] is True

    assert juju.run(UNIT, "stop-node", wait=300).status == "completed"
    assert juju.ssh(UNIT, "systemctl is-active bitcoind || true").strip() == "inactive"
    assert juju.run(UNIT, "start-node", wait=300).status == "completed"
    _wait_active(juju)


def test_upgrade_hook_is_metadata_only(charm: Path, juju: jubilant.Juju):
    """Refreshing the same artifact must not replace or restart the workload."""
    before_hash = juju.ssh(UNIT, "sha256sum /home/bitcoin/bitcoind").split()[0]
    before_pid = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()

    juju.refresh(APP, path=charm)
    _wait_active(juju)

    after_hash = juju.ssh(UNIT, "sha256sum /home/bitcoin/bitcoind").split()[0]
    after_pid = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()
    assert after_hash == before_hash
    assert after_pid == before_pid
    assert _payload(juju)["blockchain"]["genesis_hash"] == REGTEST_GENESIS


def test_metadata_upload_uses_secret_without_leaking_it(juju: jubilant.Juju):
    """Upload one payload to a disposable loopback S3-compatible capture server."""
    encoded = base64.b64encode(S3_CAPTURE_SERVER).decode()
    juju.ssh(UNIT, f"echo {shlex.quote(encoded)} | base64 -d | sudo tee /tmp/s3-capture.py >/dev/null")
    juju.ssh(UNIT, "sudo systemd-run --unit=bitcoin-metadata-s3 python3 /tmp/s3-capture.py")
    secret = juju.add_secret(
        "bitcoin-metadata-integration",
        {
            "bucket": "test-bucket",
            "region": "test-region-1",
            "endpoint-url": "http://127.0.0.1:19000",
            "key-prefix": "integration/",
            "access-key-id": "integration-access-key",
            "secret-access-key": "integration-secret-key",
        },
    )
    juju.grant_secret(secret, APP)
    juju.config(APP, {"collector-s3-credentials": str(secret)})
    _wait_active(juju)

    captured = _payload(juju, "/tmp/bitcoin-metadata-s3.json")
    serialized = json.dumps(captured)
    assert captured["blockchain"]["genesis_hash"] == REGTEST_GENESIS
    assert "integration-access-key" not in serialized
    assert "integration-secret-key" not in serialized
    assert "collector-s3-credentials" not in captured["juju_application_config"]


@pytest.mark.skipif(not UPDATED_VERSION, reason="BITCOIN_UPDATED_VERSION is not set")
def test_release_replacement_preserves_stopped_state(juju: jubilant.Juju):
    """A validated version replacement must not start an operator-stopped node."""
    juju.run(UNIT, "stop-node", wait=300)
    assert juju.ssh(UNIT, "systemctl is-active bitcoind || true").strip() == "inactive"

    juju.config(APP, {"version": UPDATED_VERSION})
    juju.wait(lambda status: APP in status.apps, timeout=1800)

    assert juju.ssh(UNIT, "systemctl is-active bitcoind || true").strip() == "inactive"
    assert UPDATED_VERSION in juju.ssh(UNIT, "/home/bitcoin/bitcoind --version")
