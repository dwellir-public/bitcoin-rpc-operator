import base64
import json
import os
import shlex
from pathlib import Path

import jubilant

APP = "bitcoin-rpc"
UNIT = f"{APP}/0"
VERSION = os.environ["BITCOIN_VERSION"]
UPDATED_VERSION = os.environ["BITCOIN_UPDATED_VERSION"]
BINARY_URL = os.environ["BITCOIN_BINARY_URL"]
UPDATED_BINARY_URL = os.environ["BITCOIN_UPDATED_BINARY_URL"]
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


def _wait_status(juju: jubilant.Juju, expected: str, timeout: int = 300):
    return juju.wait(
        lambda status: status.apps[APP].units[UNIT].workload_status.current == expected,
        timeout=timeout,
    )


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
            "binary-url": BINARY_URL,
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
    assert info.results["rpc-proxy-running"] == "True"

    assert juju.run(UNIT, "stop-node", wait=300).status == "completed"
    assert juju.ssh(UNIT, "systemctl is-active bitcoind || true").strip() == "inactive"
    assert juju.run(UNIT, "start-node", wait=300).status == "completed"
    _wait_active(juju)

    before_restart = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()
    restarted = juju.run(UNIT, "restart-node", wait=300)
    assert restarted.status == "completed"
    _wait_active(juju)
    after_restart = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()
    assert after_restart != before_restart

    readme = juju.run(UNIT, "print-readme", wait=300)
    assert readme.status == "completed"
    assert "# Bitcoin" in readme.results["readme"]


def test_upgrade_hook_is_metadata_only(charm: Path, juju: jubilant.Juju):
    """Refreshing the same artifact must not replace or restart the workload."""
    before_hash = juju.ssh(UNIT, "sudo sha256sum /home/bitcoin/bitcoind").split()[0]
    before_pid = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()

    juju.refresh(APP, path=charm)
    _wait_active(juju)

    after_hash = juju.ssh(UNIT, "sudo sha256sum /home/bitcoin/bitcoind").split()[0]
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
    juju.config(APP, reset="collector-s3-credentials")
    _wait_active(juju)


def test_invalid_secret_blocks_and_recovers_without_stopping_bitcoind(juju: jubilant.Juju):
    """Invalid credentials must block metadata while preserving the workload."""
    before_pid = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()
    secret = juju.add_secret("bitcoin-invalid-metadata", {"invalid": "value"})
    juju.grant_secret(secret, APP)

    juju.config(APP, {"collector-s3-credentials": str(secret)})
    blocked = _wait_status(juju, "blocked")

    message = blocked.apps[APP].units[UNIT].workload_status.message
    assert "invalid collector-s3-credentials" in message
    assert juju.ssh(UNIT, "systemctl is-active bitcoind").strip() == "active"
    assert juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip() == before_pid

    juju.config(APP, reset="collector-s3-credentials")
    _wait_active(juju)
    assert juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip() == before_pid


def test_upload_failure_blocks_and_recovers(juju: jubilant.Juju):
    """An unreachable S3 endpoint must block, then recover after secret reset."""
    secret = juju.add_secret(
        "bitcoin-unreachable-metadata",
        {
            "bucket": "test-bucket",
            "region": "test-region-1",
            "endpoint-url": "http://127.0.0.1:1",
            "access-key-id": "unreachable-access-key",
            "secret-access-key": "unreachable-secret-key",
        },
    )
    juju.grant_secret(secret, APP)

    juju.config(APP, {"collector-s3-credentials": str(secret)})
    blocked = _wait_status(juju, "blocked")

    assert "metadata upload failed" in blocked.apps[APP].units[UNIT].workload_status.message
    assert juju.ssh(UNIT, "systemctl is-active bitcoind").strip() == "active"

    juju.config(APP, reset="collector-s3-credentials")
    _wait_active(juju)


def test_local_metadata_write_failure_blocks_and_recovers(juju: jubilant.Juju):
    """A broken local metadata path must block, then recover after repair."""
    metadata_dir = "/tmp/dwellir-metadata-uploader"
    backup_dir = "/tmp/dwellir-metadata-uploader.integration-backup"
    juju.ssh(
        UNIT,
        f"sudo mv {metadata_dir} {backup_dir} && sudo touch {metadata_dir}",
    )

    juju.config(APP, {"service-args": "-regtest=1 -server=1 -networkactive=0 -txindex=1 -maxconnections=16"})
    blocked = _wait_status(juju, "blocked")

    assert "metadata collection failed" in blocked.apps[APP].units[UNIT].workload_status.message
    assert juju.ssh(UNIT, "systemctl is-active bitcoind").strip() == "active"

    juju.ssh(
        UNIT,
        f"sudo rm {metadata_dir} && sudo mv {backup_dir} {metadata_dir}",
    )
    juju.config(APP, {"service-args": "-regtest=1 -server=1 -networkactive=0 -txindex=1"})
    _wait_active(juju)


def test_running_release_replacement_verifies_rpc_and_versions(juju: jubilant.Juju):
    """A running replacement must expose the requested version through RPC."""
    before_pid = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()

    juju.config(APP, {"version": UPDATED_VERSION, "binary-url": UPDATED_BINARY_URL})
    completed = juju.run(UNIT, "print-event-log", wait=1800)
    assert completed.status == "completed"
    assert f"version={UPDATED_VERSION}" in completed.results["event-log"]
    assert f"binary-url={UPDATED_BINARY_URL}" in completed.results["event-log"]
    _wait_active(juju)

    after_pid = juju.ssh(UNIT, "systemctl show bitcoind -p MainPID --value").strip()
    assert after_pid != before_pid
    assert UPDATED_VERSION in juju.ssh(UNIT, "sudo /home/bitcoin/bitcoind --version")
    assert UPDATED_VERSION in juju.ssh(UNIT, "sudo /home/bitcoin/bitcoin-cli --version")
    network = json.loads(
        juju.ssh(
            UNIT,
            "sudo /home/bitcoin/bitcoin-cli -regtest -rpcuser=integration-user "
            "-rpcpassword=integration-password getnetworkinfo",
        )
    )
    assert UPDATED_VERSION in network["subversion"]
    assert _payload(juju)["blockchain"]["client_version"].startswith(UPDATED_VERSION)


def test_release_replacement_preserves_stopped_state(juju: jubilant.Juju):
    """A validated version replacement must not start an operator-stopped node."""
    juju.run(UNIT, "stop-node", wait=300)
    assert juju.ssh(UNIT, "systemctl is-active bitcoind || true").strip() == "inactive"

    juju.config(APP, {"version": VERSION, "binary-url": BINARY_URL})
    completed = juju.run(UNIT, "print-event-log", wait=1800)
    assert completed.status == "completed"
    assert f"version={VERSION}" in completed.results["event-log"]
    assert f"binary-url={BINARY_URL}" in completed.results["event-log"]

    assert juju.ssh(UNIT, "systemctl is-active bitcoind || true").strip() == "inactive"
    assert VERSION in juju.ssh(UNIT, "sudo /home/bitcoin/bitcoind --version")
    assert VERSION in juju.ssh(UNIT, "sudo /home/bitcoin/bitcoin-cli --version")
