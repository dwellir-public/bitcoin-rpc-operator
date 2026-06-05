# Copyright 2024 Jakob Ersson
# See LICENSE file for licensing details.

from unittest import mock

import pytest
import requests

import utils

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
