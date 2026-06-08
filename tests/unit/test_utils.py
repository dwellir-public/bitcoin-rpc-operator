# Copyright 2024-2026 Dwellir
# See LICENSE file for licensing details.

from unittest import mock

import pytest
import requests

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


def test_install_bitcoin_downloads_extracts_and_installs():
    with (
        mock.patch("utils.requests.get") as get,
        mock.patch("utils.sp.run") as run,
        mock.patch("utils.chown") as chown,
    ):
        get.return_value.content = b"tarball-bytes"
        utils.install_bitcoin("31.0")

    url = get.call_args.args[0]
    assert "31.0" in url
    assert "VERSION" not in url
    get.return_value.raise_for_status.assert_called_once()

    tar_cmd = run.call_args_list[0].args[0]
    assert tar_cmd[:2] == ["tar", "-xzf"]
    assert tar_cmd[2].endswith(url.split("/")[-1])
    cp_cmd = run.call_args_list[1].args[0]
    assert cp_cmd[0] == "cp"
    chmod_cmd = run.call_args_list[2].args[0]
    assert chmod_cmd[0] == "chmod"
    chown.assert_called_once()


def test_install_bitcoin_is_noop_without_version():
    # version defaults to empty; install must skip cleanly so the unit can
    # block on missing config instead of erroring the hook on a bogus URL.
    with (
        mock.patch("utils.requests.get") as get,
        mock.patch("utils.sp.run") as run,
        mock.patch("utils.chown") as chown,
    ):
        utils.install_bitcoin("")
    get.assert_not_called()
    run.assert_not_called()
    chown.assert_not_called()


def test_install_bitcoin_failed_download_skips_install():
    # A failed download must raise before any extraction or install steps run.
    with (
        mock.patch("utils.requests.get") as get,
        mock.patch("utils.sp.run") as run,
        mock.patch("utils.chown") as chown,
    ):
        get.return_value.raise_for_status.side_effect = requests.HTTPError("404")
        with pytest.raises(requests.HTTPError):
            utils.install_bitcoin("99.99")

    run.assert_not_called()
    chown.assert_not_called()
