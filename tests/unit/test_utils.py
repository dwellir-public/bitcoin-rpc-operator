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
    with (
        mock.patch("utils.bitcoin.install_release") as install_release,
        mock.patch("utils.chown") as chown,
    ):
        utils.install_bitcoin("31.0")

    assert install_release.call_args.args == ("31.0", c.BINARY_PATH, c.CLI_PATH)
    assert set(install_release.call_args.kwargs) == {"is_running", "stop", "start", "is_healthy"}
    chown.assert_called_once()


def test_install_bitcoin_is_noop_without_version():
    # version defaults to empty; install must skip cleanly so the unit can
    # block on missing config instead of erroring the hook on a bogus URL.
    with (
        mock.patch("utils.bitcoin.install_release") as install_release,
        mock.patch("utils.chown") as chown,
    ):
        utils.install_bitcoin("")
    install_release.assert_called_once()
    chown.assert_not_called()


def test_install_bitcoin_failed_download_skips_install():
    # A failed download must raise before any extraction or install steps run.
    with (
        mock.patch("utils.bitcoin.install_release", side_effect=ValueError("checksum")),
        mock.patch("utils.chown") as chown,
    ):
        with pytest.raises(ValueError, match="checksum"):
            utils.install_bitcoin("99.99")

    chown.assert_not_called()


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
