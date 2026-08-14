# Copyright 2024-2026 Dwellir
# See LICENSE file for licensing details.

from unittest import mock

import pytest

import constants as c
import utils

# install_dependencies


def test_install_dependencies_installs_apt_then_pinned_venv():
    with (
        mock.patch("utils.sp.run") as run,
        mock.patch("utils.chown") as chown,
        mock.patch("utils.Path.mkdir") as mkdir,
    ):
        utils.install_dependencies()

    cmds = [call.args[0] for call in run.call_args_list]
    # apt update, apt install, venv creation, then pip install -- in order.
    assert cmds[0] == ["apt", "update"]
    assert cmds[1][:3] == ["apt", "install", "-y"]
    assert "python3-venv" in cmds[1]
    assert cmds[2] == ["python3", "-m", "venv", str(c.MONITOR_VENV_DIR)]

    pip_cmd = cmds[3]
    assert pip_cmd[:2] == [str(c.MONITOR_VENV_PIP), "install"]
    # Dependencies must be installed into the venv pip, version-pinned.
    assert pip_cmd[2:] == c.PIP_PACKAGES
    assert all("==" in pkg for pkg in pip_cmd[2:])

    mkdir.assert_called_once()
    chown.assert_called_once()


def test_install_dependencies_does_not_use_system_pip():
    # PEP 668: a bare `pip install` into the system Python fails on Ubuntu 24.04.
    with (
        mock.patch("utils.sp.run") as run,
        mock.patch("utils.chown"),
        mock.patch("utils.Path.mkdir"),
    ):
        utils.install_dependencies()

    for call in run.call_args_list:
        cmd = call.args[0]
        assert cmd[0] != "pip", "must install via the venv pip, not system pip"


# install_bitcoin


def test_install_bitcoin_delegates_to_verified_release_lifecycle():
    config = {"rpc-user": "alice", "rpc-password": "secret"}
    binary_url = "https://downloads.example.test/bitcoin-31.0-x86_64-linux-gnu.tar.gz"
    with (
        mock.patch("utils.bitcoin.install_release") as install_release,
        mock.patch("utils.chown") as chown,
        mock.patch("utils.wait_for_running_version", return_value="31.0.0") as wait_ready,
    ):
        utils.install_bitcoin("31.0", config, binary_url=binary_url)
        assert install_release.call_args.kwargs["wait_for_running_version"]() == "31.0.0"

    assert install_release.call_args.args == ("31.0", c.BINARY_PATH, c.CLI_PATH)
    assert set(install_release.call_args.kwargs) == {
        "binary_url",
        "is_running",
        "stop",
        "start",
        "wait_for_running_version",
    }
    assert install_release.call_args.kwargs["binary_url"] == binary_url
    wait_ready.assert_called_once_with(config)
    chown.assert_called_once()


def test_install_bitcoin_is_noop_without_version():
    # version defaults to empty; install must skip cleanly so the unit can
    # block on missing config instead of erroring the hook on a bogus URL.
    with (
        mock.patch("utils.bitcoin.install_release") as install_release,
        mock.patch("utils.chown") as chown,
    ):
        utils.install_bitcoin("", {})
    install_release.assert_called_once()
    chown.assert_not_called()


def test_install_bitcoin_failed_download_skips_install():
    # A failed download must raise before any extraction or install steps run.
    with (
        mock.patch("utils.bitcoin.install_release", side_effect=ValueError("checksum")),
        mock.patch("utils.chown") as chown,
    ):
        with pytest.raises(ValueError, match="checksum"):
            utils.install_bitcoin("99.99", {})

    chown.assert_not_called()


def test_config_transaction_applies_one_credential_set_before_binary_readiness(tmp_path, monkeypatch):
    paths = tuple(tmp_path / name for name in ("bitcoind", "bitcoin-cli", "proxy", "node-env", "monitor-env"))
    for path in paths:
        path.write_text(f"old-{path.name}")
    monkeypatch.setattr(utils, "_transaction_paths", lambda: paths)
    monkeypatch.setattr(utils, "get_status", lambda _service: True)
    events = []
    new_config = {
        "rpc-user": "new-user",
        "rpc-password": "new-password",
        "service-args": "-txindex=1",
        "version": "31.0",
        "binary-url": "https://downloads.example.test/bitcoin-31.0-x86_64-linux-gnu.tar.gz",
        "rpc-proxy-version": "0.2.0",
    }
    previous_config = {
        **new_config,
        "rpc-user": "old-user",
        "rpc-password": "old-password",
        "version": "30.0",
        "binary-url": "https://downloads.example.test/bitcoin-30.0-x86_64-linux-gnu.tar.gz",
    }

    monkeypatch.setattr(utils, "install_rpc_proxy", lambda version: events.append(("proxy-binary", version)))
    monkeypatch.setattr(
        utils,
        "update_service_args",
        lambda config, restart_service: events.append(("node-args", config["rpc-user"], restart_service)),
    )
    monkeypatch.setattr(
        utils,
        "install_bitcoind_monitor",
        lambda config, restart_service: events.append(("monitor-env", config["rpc-user"], restart_service)),
    )
    monkeypatch.setattr(
        utils,
        "install_rpc_proxy_service",
        lambda config, restart_service: events.append(("proxy-env", config["rpc-user"], restart_service)),
    )
    monkeypatch.setattr(
        utils,
        "install_bitcoin",
        lambda version, config, *, binary_url: events.append(
            ("bitcoin-ready", version, binary_url, config["rpc-user"])
        ),
    )
    monkeypatch.setattr(utils, "restart_monitor", lambda: events.append(("monitor-restart",)))
    monkeypatch.setattr(utils, "restart_rpc_proxy", lambda: events.append(("proxy-restart",)))
    monkeypatch.setattr(utils, "rpc_proxy_binary_installed", lambda: True)
    monkeypatch.setattr(utils, "service_running", lambda _service: True)

    utils.apply_config_transaction(
        new_config,
        previous_config=previous_config,
        changed_keys={
            "rpc-user",
            "rpc-password",
            "service-args",
            "version",
            "binary-url",
            "rpc-proxy-version",
        },
    )

    assert events == [
        ("proxy-binary", "0.2.0"),
        ("node-args", "new-user", False),
        ("monitor-env", "new-user", False),
        ("proxy-env", "new-user", False),
        (
            "bitcoin-ready",
            "31.0",
            "https://downloads.example.test/bitcoin-31.0-x86_64-linux-gnu.tar.gz",
            "new-user",
        ),
        ("monitor-restart",),
        ("proxy-restart",),
    ]


def test_config_transaction_rolls_back_all_files_and_service_states_on_start_failure(tmp_path, monkeypatch):
    paths = tuple(tmp_path / name for name in ("bitcoind", "bitcoin-cli", "proxy", "node-env", "monitor-env"))
    old_contents = {path: f"old-{path.name}".encode() for path in paths}
    for path, content in old_contents.items():
        path.write_bytes(content)
    monkeypatch.setattr(utils, "_transaction_paths", lambda: paths)
    prior_states = {
        c.SERVICE_NAME: True,
        c.MONITOR_SERVICE_NAME: False,
        c.RPC_PROXY_SERVICE_NAME: True,
    }
    monkeypatch.setattr(utils, "get_status", prior_states.__getitem__)
    service_events = []
    monkeypatch.setattr(utils, "stop_service", lambda: service_events.append("stop-node"))
    monkeypatch.setattr(utils, "stop_monitor", lambda: service_events.append("stop-monitor"))
    monkeypatch.setattr(utils, "stop_rpc_proxy", lambda: service_events.append("stop-proxy"))
    monkeypatch.setattr(utils, "start_service", lambda: service_events.append("start-node"))
    monkeypatch.setattr(utils, "start_monitor", lambda: service_events.append("start-monitor"))
    monkeypatch.setattr(utils, "start_rpc_proxy", lambda: service_events.append("start-proxy"))
    monkeypatch.setattr(utils.sp, "run", lambda *_args, **_kwargs: None)
    previous_config = {"rpc-user": "old-user", "rpc-password": "old-password", "version": "30.0"}
    readiness_configs = []
    monkeypatch.setattr(
        utils,
        "wait_for_running_version",
        lambda config: readiness_configs.append(config) or "30.0",
    )
    monkeypatch.setattr(utils, "service_running", lambda _service: True)

    def replace_files(*_args, **_kwargs):
        for path in paths:
            path.write_text(f"new-{path.name}")

    monkeypatch.setattr(utils, "install_rpc_proxy", lambda _version: paths[2].write_text("new-proxy"))
    monkeypatch.setattr(utils, "update_service_args", replace_files)
    monkeypatch.setattr(utils, "install_bitcoind_monitor", replace_files)
    monkeypatch.setattr(utils, "install_rpc_proxy_service", replace_files)
    monkeypatch.setattr(utils, "install_bitcoin", replace_files)
    monkeypatch.setattr(utils, "restart_monitor", lambda: None)
    monkeypatch.setattr(utils, "rpc_proxy_binary_installed", lambda: True)
    monkeypatch.setattr(
        utils,
        "restart_rpc_proxy",
        lambda: (_ for _ in ()).throw(RuntimeError("proxy start failed")),
    )

    with pytest.raises(RuntimeError, match="proxy start failed"):
        utils.apply_config_transaction(
            {
                "rpc-user": "new-user",
                "rpc-password": "new-password",
                "version": "31.0",
                "rpc-proxy-version": "0.2.0",
            },
            previous_config=previous_config,
            changed_keys={"rpc-user", "rpc-password", "version", "rpc-proxy-version"},
        )

    assert {path: path.read_bytes() for path in paths} == old_contents
    assert service_events == ["stop-proxy", "stop-monitor", "stop-node", "start-node", "start-proxy"]
    assert readiness_configs == [previous_config]


@pytest.mark.parametrize(
    ("operation", "command"),
    [
        (utils.restart_service, ["systemctl", "restart", c.SERVICE_NAME]),
        (utils.start_service, ["systemctl", "start", c.SERVICE_NAME]),
        (utils.stop_service, ["systemctl", "stop", c.SERVICE_NAME]),
        (utils.restart_monitor, ["systemctl", "restart", c.MONITOR_SERVICE_NAME]),
        (utils.start_monitor, ["systemctl", "start", c.MONITOR_SERVICE_NAME]),
        (utils.stop_monitor, ["systemctl", "stop", c.MONITOR_SERVICE_NAME]),
        (utils.restart_rpc_proxy, ["systemctl", "restart", c.RPC_PROXY_SERVICE_NAME]),
        (utils.start_rpc_proxy, ["systemctl", "start", c.RPC_PROXY_SERVICE_NAME]),
        (utils.stop_rpc_proxy, ["systemctl", "stop", c.RPC_PROXY_SERVICE_NAME]),
    ],
)
def test_service_operations_propagate_systemctl_failures(operation, command):
    with mock.patch("utils.sp.run") as run:
        operation()

    run.assert_called_once_with(command, check=True)


def test_wait_for_running_version_retries_rpc_until_it_reports_subversion():
    unavailable = mock.MagicMock()
    unavailable.raise_for_status.side_effect = utils.requests.ConnectionError("not ready")
    ready = mock.MagicMock()
    ready.raise_for_status.return_value = None
    ready.json.return_value = {
        "jsonrpc": "2.0",
        "id": "activation",
        "error": None,
        "result": {"subversion": "/Satoshi:31.0.0/"},
    }
    config = {"rpc-user": "alice", "rpc-password": "secret"}

    with (
        mock.patch("utils.requests.post", side_effect=[unavailable, ready]) as post,
        mock.patch("utils.time.sleep") as sleep,
    ):
        version = utils.wait_for_running_version(config, attempts=2, interval=0.01)

    assert version == "31.0.0"
    assert post.call_count == 2
    sleep.assert_called_once_with(0.01)


def test_wait_for_running_version_is_bounded_when_rpc_never_becomes_ready():
    with (
        mock.patch("utils.requests.post", side_effect=utils.requests.Timeout("not ready")) as post,
        mock.patch("utils.time.sleep") as sleep,
    ):
        version = utils.wait_for_running_version({}, attempts=3, interval=0.01)

    assert version is None
    assert post.call_count == 3
    assert sleep.call_count == 2


def test_get_charm_version_prefers_stamped_file(tmp_path, monkeypatch):
    # The build-time charm_version stamp (tag + commit) wins over the VERSION tag.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "charm_version").write_text("v0.1.0-6-gd0d4771\n")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    assert utils.get_charm_version() == "v0.1.0-6-gd0d4771"


def test_get_charm_version_falls_back_to_version_file(tmp_path, monkeypatch):
    # Without the stamp (e.g. git absent in the build sandbox), fall back to VERSION.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("0.1.0\n")
    assert utils.get_charm_version() == "0.1.0"


def test_get_charm_version_unknown_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert utils.get_charm_version() == "unknown"


# install_service_file (change detection drives the conditional upgrade restart)


def _patch_service_file_paths(source_content, target_exists, target_content):
    """Patch utils.Path so install_service_file sees a controlled source/target pair."""
    src = mock.MagicMock()
    src.read_text.return_value = source_content
    tgt = mock.MagicMock()
    tgt.exists.return_value = target_exists
    tgt.read_text.return_value = target_content

    def fake_path(p):
        return tgt if "systemd" in str(p) else src

    return mock.patch("utils.Path", side_effect=fake_path), src, tgt


def test_install_service_file_skips_when_unchanged():
    patch_path, _src, _tgt = _patch_service_file_paths("UNIT", target_exists=True, target_content="UNIT")
    with patch_path, mock.patch("utils.shutil") as msh, mock.patch("utils.sp") as msp:
        assert utils.install_service_file("templates/bitcoind.service", "bitcoind") is False
        msh.copyfile.assert_not_called()
        msp.run.assert_not_called()  # no redundant daemon-reload on an identical unit


def test_install_service_file_installs_when_changed():
    patch_path, _src, _tgt = _patch_service_file_paths("NEW", target_exists=True, target_content="OLD")
    with patch_path, mock.patch("utils.shutil") as msh, mock.patch("utils.sp") as msp:
        assert utils.install_service_file("templates/bitcoind.service", "bitcoind") is True
        msh.copyfile.assert_called_once()
        msp.run.assert_called_once()  # daemon-reload


def test_install_service_file_installs_when_target_missing():
    patch_path, _src, _tgt = _patch_service_file_paths("UNIT", target_exists=False, target_content="")
    with patch_path, mock.patch("utils.shutil") as msh, mock.patch("utils.sp"):
        assert utils.install_service_file("templates/bitcoind.service", "bitcoind") is True
        msh.copyfile.assert_called_once()


def test_get_systemd_limits_normalizes_infinity_and_cpu_quota():
    output = "\n".join(
        (
            "MemoryMax=8589934592",
            "MemoryHigh=infinity",
            "CPUQuotaPerSecUSec=4s",
            "TasksMax=4915",
        )
    )
    with mock.patch("utils.sp.run") as run:
        run.return_value.stdout = output
        limits = utils.get_systemd_limits("bitcoind")

    assert limits == {
        "memory_max_bytes": 8589934592,
        "memory_high_bytes": None,
        "cpu_quota_percent": 400.0,
        "tasks_max": 4915,
    }
    run.assert_called_once_with(
        [
            "systemctl",
            "show",
            "bitcoind",
            "--property=MemoryMax,MemoryHigh,CPUQuotaPerSecUSec,TasksMax",
            "--no-pager",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
