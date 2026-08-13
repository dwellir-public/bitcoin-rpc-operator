import hashlib
import io
import tarfile
from pathlib import Path
from unittest import mock

import pytest

import bitcoin


def _release(version: str, daemon: bytes = b"new-daemon", cli: bytes = b"new-cli") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, payload in (("bitcoind", daemon), ("bitcoin-cli", cli)):
            info = tarfile.TarInfo(f"bitcoin-{version}/bin/{name}")
            info.size = len(payload)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _responses(payload: bytes):
    release = mock.MagicMock(content=payload)
    release.raise_for_status.return_value = None
    sums = mock.MagicMock(text=f"{hashlib.sha256(payload).hexdigest()}  bitcoin-31.0-x86_64-linux-gnu.tar.gz\n")
    sums.raise_for_status.return_value = None
    return release, sums


def _version_output(value: str) -> mock.MagicMock:
    return mock.MagicMock(stdout=value)


def test_install_release_validates_both_staged_binaries_before_stopping(tmp_path):
    payload = _release("31.0")
    release, sums = _responses(payload)
    stop = mock.Mock()

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            side_effect=[
                _version_output("Bitcoin Core daemon version v31.0.0 bitcoind\n"),
                _version_output("Bitcoin Core RPC client version v30.0.0 bitcoin-cli\n"),
            ],
        ),
    ):
        with pytest.raises(ValueError, match="bitcoin-cli.*expected 31.0"):
            bitcoin.install_release(
                "31.0",
                tmp_path / "bitcoind",
                tmp_path / "bitcoin-cli",
                is_running=lambda: True,
                stop=stop,
                start=mock.Mock(),
                wait_for_running_version=lambda: "v31.0.0",
            )

    stop.assert_not_called()


def test_install_release_rolls_back_when_running_rpc_version_is_wrong(tmp_path):
    daemon = tmp_path / "bitcoind"
    cli = tmp_path / "bitcoin-cli"
    daemon.write_bytes(b"old-daemon")
    cli.write_bytes(b"old-cli")
    payload = _release("31.0")
    release, sums = _responses(payload)
    events = []

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            side_effect=[
                _version_output("Bitcoin Core daemon version v31.0.0 bitcoind\n"),
                _version_output("Bitcoin Core RPC client version v31.0.0 bitcoin-cli\n"),
            ],
        ),
    ):
        with pytest.raises(RuntimeError, match="running Bitcoin Core version.*v30.0.0"):
            bitcoin.install_release(
                "31.0",
                daemon,
                cli,
                is_running=lambda: events.append("running") or True,
                stop=lambda: events.append("stop"),
                start=lambda: events.append("start"),
                wait_for_running_version=lambda: events.append("ready") or "v30.0.0",
            )

    assert daemon.read_bytes() == b"old-daemon"
    assert cli.read_bytes() == b"old-cli"
    assert events == ["running", "stop", "start", "ready", "stop", "start"]


def test_install_release_rolls_back_both_binaries_when_replacement_start_fails(tmp_path):
    daemon = tmp_path / "bitcoind"
    cli = tmp_path / "bitcoin-cli"
    daemon.write_bytes(b"old-daemon")
    cli.write_bytes(b"old-cli")
    payload = _release("31.0")
    release, sums = _responses(payload)
    events = []
    starts = iter((RuntimeError("replacement start failed"), None))

    def start():
        events.append("start")
        failure = next(starts)
        if failure is not None:
            raise failure

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            return_value=mock.MagicMock(stdout="Bitcoin Core daemon version v31.0.0 bitcoind\n"),
        ),
    ):
        with pytest.raises(RuntimeError, match="replacement start failed"):
            bitcoin.install_release(
                "31.0",
                daemon,
                cli,
                is_running=lambda: events.append("running") or True,
                stop=lambda: events.append("stop"),
                start=start,
                wait_for_running_version=lambda: "v31.0.0",
            )

    assert daemon.read_bytes() == b"old-daemon"
    assert cli.read_bytes() == b"old-cli"
    assert events == ["running", "stop", "start", "stop", "start"]


def test_install_release_recovers_old_service_when_initial_stop_fails(tmp_path):
    daemon = tmp_path / "bitcoind"
    cli = tmp_path / "bitcoin-cli"
    daemon.write_bytes(b"old-daemon")
    cli.write_bytes(b"old-cli")
    payload = _release("31.0")
    release, sums = _responses(payload)
    start = mock.Mock()

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            side_effect=[
                _version_output("Bitcoin Core daemon version v31.0.0 bitcoind\n"),
                _version_output("Bitcoin Core RPC client version v31.0.0 bitcoin-cli\n"),
            ],
        ),
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            bitcoin.install_release(
                "31.0",
                daemon,
                cli,
                is_running=lambda: True,
                stop=mock.Mock(side_effect=RuntimeError("stop failed")),
                start=start,
                wait_for_running_version=lambda: "v31.0.0",
            )

    assert daemon.read_bytes() == b"old-daemon"
    assert cli.read_bytes() == b"old-cli"
    start.assert_called_once()


def test_install_release_verifies_checksum_and_version_before_stopping(tmp_path):
    payload = _release("31.0")
    release, sums = _responses(payload)
    events = []

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]) as get,
        mock.patch(
            "bitcoin.sp.run",
            return_value=mock.MagicMock(stdout="Bitcoin Core daemon version v31.0.0 bitcoind\n"),
        ),
    ):
        bitcoin.install_release(
            "31.0",
            tmp_path / "bitcoind",
            tmp_path / "bitcoin-cli",
            is_running=lambda: events.append("running") or True,
            stop=lambda: events.append("stop"),
            start=lambda: events.append("start"),
            wait_for_running_version=lambda: events.append("healthy") or "v31.0.0",
        )

    assert (tmp_path / "bitcoind").read_bytes() == b"new-daemon"
    assert (tmp_path / "bitcoin-cli").read_bytes() == b"new-cli"
    assert events == ["running", "stop", "start", "healthy"]
    assert get.call_args_list[1].args[0].endswith("/bitcoin-core-31.0/SHA256SUMS")


def test_install_release_checksum_failure_preserves_running_binary(tmp_path):
    daemon = tmp_path / "bitcoind"
    daemon.write_bytes(b"old-daemon")
    payload = _release("31.0")
    release, sums = _responses(payload)
    sums.text = f"{'0' * 64}  bitcoin-31.0-x86_64-linux-gnu.tar.gz\n"
    stop = mock.Mock()

    with mock.patch("bitcoin.requests.get", side_effect=[release, sums]):
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            bitcoin.install_release(
                "31.0",
                daemon,
                tmp_path / "bitcoin-cli",
                is_running=lambda: True,
                stop=stop,
                start=mock.Mock(),
                wait_for_running_version=lambda: "v31.0.0",
            )

    assert daemon.read_bytes() == b"old-daemon"
    stop.assert_not_called()


def test_install_release_version_failure_preserves_running_binary(tmp_path):
    daemon = tmp_path / "bitcoind"
    daemon.write_bytes(b"old-daemon")
    payload = _release("31.0")
    release, sums = _responses(payload)
    stop = mock.Mock()

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            return_value=mock.MagicMock(stdout="Bitcoin Core daemon version v30.2.0 bitcoind\n"),
        ),
    ):
        with pytest.raises(ValueError, match="expected 31.0"):
            bitcoin.install_release(
                "31.0",
                daemon,
                tmp_path / "bitcoin-cli",
                is_running=lambda: True,
                stop=stop,
                start=mock.Mock(),
                wait_for_running_version=lambda: "v31.0.0",
            )

    assert daemon.read_bytes() == b"old-daemon"
    stop.assert_not_called()


def test_install_release_rolls_back_both_binaries_when_health_fails(tmp_path):
    daemon = tmp_path / "bitcoind"
    cli = tmp_path / "bitcoin-cli"
    daemon.write_bytes(b"old-daemon")
    cli.write_bytes(b"old-cli")
    payload = _release("31.0")
    release, sums = _responses(payload)
    events = []

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            return_value=mock.MagicMock(stdout="Bitcoin Core daemon version v31.0.0 bitcoind\n"),
        ),
    ):
        with pytest.raises(RuntimeError, match="did not become RPC-ready"):
            bitcoin.install_release(
                "31.0",
                daemon,
                cli,
                is_running=lambda: events.append("running") or True,
                stop=lambda: events.append("stop"),
                start=lambda: events.append("start"),
                wait_for_running_version=lambda: events.append("healthy") or None,
            )

    assert daemon.read_bytes() == b"old-daemon"
    assert cli.read_bytes() == b"old-cli"
    assert events == ["running", "stop", "start", "healthy", "stop", "start"]


def test_install_release_rolls_back_partial_two_binary_swap(tmp_path):
    daemon = tmp_path / "bitcoind"
    cli = tmp_path / "bitcoin-cli"
    daemon.write_bytes(b"old-daemon")
    cli.write_bytes(b"old-cli")
    payload = _release("31.0")
    release, sums = _responses(payload)
    events = []
    real_replace = bitcoin.os.replace

    def replace(source, destination):
        if Path(source).name == "bitcoin-cli" and Path(destination) == cli:
            raise OSError("simulated second swap failure")
        real_replace(source, destination)

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            return_value=mock.MagicMock(stdout="Bitcoin Core daemon version v31.0.0 bitcoind\n"),
        ),
        mock.patch("bitcoin.os.replace", side_effect=replace),
    ):
        with pytest.raises(OSError, match="second swap failure"):
            bitcoin.install_release(
                "31.0",
                daemon,
                cli,
                is_running=lambda: events.append("running") or True,
                stop=lambda: events.append("stop"),
                start=lambda: events.append("start"),
                wait_for_running_version=lambda: "v31.0.0",
            )

    assert daemon.read_bytes() == b"old-daemon"
    assert cli.read_bytes() == b"old-cli"
    assert events == ["running", "stop", "start"]


def test_install_release_preserves_stopped_state(tmp_path):
    payload = _release("31.0")
    release, sums = _responses(payload)
    stop, start, healthy = mock.Mock(), mock.Mock(), mock.Mock()

    with (
        mock.patch("bitcoin.requests.get", side_effect=[release, sums]),
        mock.patch(
            "bitcoin.sp.run",
            return_value=mock.MagicMock(stdout="Bitcoin Core daemon version v31.0.0 bitcoind\n"),
        ),
    ):
        bitcoin.install_release(
            "31.0",
            tmp_path / "bitcoind",
            tmp_path / "bitcoin-cli",
            is_running=lambda: False,
            stop=stop,
            start=start,
            wait_for_running_version=healthy,
        )

    stop.assert_not_called()
    start.assert_not_called()
    healthy.assert_not_called()


def test_install_release_requires_both_expected_members(tmp_path):
    payload = _release("31.0", cli=b"")
    # Replace the archive with one that omits bitcoin-cli.
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("bitcoin-31.0/bin/bitcoind")
        info.size = len(b"daemon")
        archive.addfile(info, io.BytesIO(b"daemon"))
    payload = stream.getvalue()
    release, sums = _responses(payload)

    with mock.patch("bitcoin.requests.get", side_effect=[release, sums]):
        with pytest.raises(ValueError, match="bitcoin-cli"):
            bitcoin.install_release(
                "31.0",
                tmp_path / "bitcoind",
                tmp_path / "bitcoin-cli",
                is_running=lambda: False,
                stop=mock.Mock(),
                start=mock.Mock(),
                wait_for_running_version=lambda: "v31.0.0",
            )


def test_install_release_is_noop_without_version(tmp_path):
    with mock.patch("bitcoin.requests.get") as get:
        bitcoin.install_release(
            "",
            tmp_path / "bitcoind",
            tmp_path / "bitcoin-cli",
            is_running=lambda: False,
            stop=mock.Mock(),
            start=mock.Mock(),
            wait_for_running_version=lambda: "v31.0.0",
        )
    get.assert_not_called()
