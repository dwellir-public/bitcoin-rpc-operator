# Copyright 2024-2026 Dwellir
# See LICENSE file for licensing details.

from typing import cast
from unittest import mock

import ops
import pytest
import requests

import constants as c
import utils


def cfg(disable_wallet=True, service_args="", rpc_proxy_filter=True):
    return cast(
        ops.ConfigData,
        {
            "disable-wallet": disable_wallet,
            "service-args": service_args,
            "rpc-proxy-listen": "0.0.0.0:8331",
            "rpc-proxy-extend-allowlist": "",
            "rpc-proxy-filter": rpc_proxy_filter,
        },
    )


# rpc_proxy_binary_installed


def test_rpc_proxy_binary_installed_tracks_path():
    with mock.patch("utils.c.RPC_PROXY_BINARY_PATH") as binpath:
        binpath.exists.return_value = True
        assert utils.rpc_proxy_binary_installed() is True
        binpath.exists.return_value = False
        assert utils.rpc_proxy_binary_installed() is False


# harden_service_args (unconditional: the proxy is always bitcoind's front door)


def test_harden_pins_loopback_and_strips_rpc_bind_opts():
    args = "-rpcbind=0.0.0.0 -rpcallowip=10.0.0.0/8 -txindex=1"
    out = utils.harden_service_args(args, cfg(disable_wallet=False)).split()
    assert "-rpcbind=127.0.0.1" in out
    assert "-rpcbind=[::1]" in out  # both loopback families bound
    assert "-rpcbind=0.0.0.0" not in out
    assert not any(t.startswith("-rpcallowip") for t in out)
    assert "-txindex=1" in out  # unrelated flags preserved
    assert "-disablewallet" not in out  # wallet untouched when disable-wallet is false


def test_harden_pins_rpcport_and_strips_operator_override():
    args = "-rpcport=18332 -txindex=1"
    out = utils.harden_service_args(args, cfg(disable_wallet=False)).split()
    assert "-rpcport=18332" not in out  # operator override stripped
    assert out.count(f"-rpcport={c.BITCOIND_RPC_PORT}") == 1  # pinned to the proxy/monitor target
    assert "-txindex=1" in out


def test_harden_disables_wallet_and_strips_conflicting_flags():
    args = "-wallet=foo -disablewallet=0 -txindex=1"
    out = utils.harden_service_args(args, cfg(disable_wallet=True)).split()
    assert "-disablewallet" in out
    assert "-disablewallet=0" not in out
    assert "-wallet=foo" not in out
    assert "-rpcbind=127.0.0.1" in out
    assert "-rpcbind=[::1]" in out
    assert "-txindex=1" in out


def test_harden_strips_negation_and_bare_forms():
    args = "-norpcbind -nodisablewallet -rpcbind"
    out = utils.harden_service_args(args, cfg(disable_wallet=True)).split()
    assert "-norpcbind" not in out
    assert "-nodisablewallet" not in out
    assert out.count("-rpcbind=127.0.0.1") == 1
    assert "-rpcbind" not in out  # bare additive form removed too
    assert out.count("-disablewallet") == 1


# update_service_args (change detection drives the conditional upgrade restart)


@mock.patch.object(utils, "start_service")
@mock.patch.object(utils, "stop_service")
def test_update_service_args_reports_change(mock_stop, mock_start):
    with mock.patch("utils.Path") as mock_path:
        env = mock_path.return_value
        # Missing env file renders as a change; capture what we just wrote, then
        # feed it back and confirm an identical re-render reports no change.
        env.exists.return_value = False
        assert utils.update_service_args(cfg(service_args="-txindex=1"), restart_service=False) is True
        written = env.write_text.call_args[0][0]
        env.exists.return_value = True
        env.read_text.return_value = written
        assert utils.update_service_args(cfg(service_args="-txindex=1"), restart_service=False) is False
    mock_stop.assert_not_called()  # restart_service=False never cycles bitcoind
    mock_start.assert_not_called()


@mock.patch.object(utils, "start_service")
@mock.patch.object(utils, "stop_service")
def test_update_service_args_restart_true_always_cycles(mock_stop, mock_start):
    # The version-change path relies on restart_service=True bringing a stopped
    # node back up, so it must stop/start regardless of whether args changed.
    with mock.patch("utils.Path") as mock_path:
        env = mock_path.return_value
        env.exists.return_value = True
        env.read_text.return_value = utils.update_service_args.__doc__  # any non-matching content
        utils.update_service_args(cfg(), restart_service=True)
    mock_stop.assert_called_once()
    mock_start.assert_called_once()


# write_rpc_proxy_env_file


def test_write_env_file_applies_config_overrides_and_keeps_defaults():
    config = cast(
        ops.ConfigData,
        {
            "rpc-proxy-listen": "127.0.0.1:9999",
            "rpc-proxy-extend-allowlist": "sendrawtransaction",
            "rpc-proxy-filter": True,
            "rpc-user": "alice",
            "rpc-password": "s3cret",
        },
    )
    m = mock.mock_open()
    with mock.patch("builtins.open", m):
        utils.write_rpc_proxy_env_file(config)

    m.assert_called_once_with(f"/etc/default/{c.RPC_PROXY_SERVICE_NAME}", "w")
    written = "".join(call.args[0] for call in m().write.call_args_list)
    assert "PROXY_LISTEN=127.0.0.1:9999\n" in written  # override applied
    assert "PROXY_EXTEND_ALLOWLIST=sendrawtransaction\n" in written
    assert "PROXY_FILTER=true\n" in written
    assert "PROXY_UPSTREAM_URL=http://127.0.0.1:8332\n" in written  # default preserved
    assert "PROXY_UPSTREAM_USER=alice\n" in written  # probe creds from rpc-user
    assert "PROXY_UPSTREAM_PASSWORD=s3cret\n" in written  # probe creds from rpc-password
    assert f"PROXY_ADMIN_LISTEN=0.0.0.0:{c.RPC_PROXY_ADMIN_PORT}\n" in written  # frozen, not overridable
    assert "PROXY_LOG_LEVEL=info\n" in written


def test_write_env_file_filter_false_is_written():
    # A deliberate "false" must survive (not be dropped as a falsy override).
    config = cast(
        ops.ConfigData,
        {
            "rpc-proxy-listen": "0.0.0.0:8331",
            "rpc-proxy-extend-allowlist": "",
            "rpc-proxy-filter": False,
        },
    )
    m = mock.mock_open()
    with mock.patch("builtins.open", m):
        utils.write_rpc_proxy_env_file(config)
    written = "".join(call.args[0] for call in m().write.call_args_list)
    assert "PROXY_FILTER=false\n" in written


# install_rpc_proxy


def test_install_rpc_proxy_substitutes_version_and_sets_exec_bit(tmp_path):
    binpath = tmp_path / "bitcoin-rpc-proxy"
    with (
        mock.patch("utils.requests.get") as get,
        mock.patch("utils.c.RPC_PROXY_BINARY_PATH", binpath),
        mock.patch("utils.chown") as chown,
    ):
        get.return_value.content = b"binary-bytes"
        utils.install_rpc_proxy("1.2.3")

    url = get.call_args.args[0]
    assert "1.2.3" in url
    assert "VERSION" not in url
    get.return_value.raise_for_status.assert_called_once()
    assert binpath.read_bytes() == b"binary-bytes"
    assert binpath.stat().st_mode & 0o111
    chown.assert_called_once()


def test_install_rpc_proxy_replaces_running_binary_atomically(tmp_path):
    # The proxy service holds the old binary open; writing in place would fail
    # with ETXTBSY. The atomic replace must swap the file to a fresh inode and
    # leave no temp files behind in the directory.
    binpath = tmp_path / "bitcoin-rpc-proxy"
    binpath.write_bytes(b"old-binary")
    old_inode = binpath.stat().st_ino
    with (
        mock.patch("utils.requests.get") as get,
        mock.patch("utils.c.RPC_PROXY_BINARY_PATH", binpath),
        mock.patch("utils.chown"),
    ):
        get.return_value.content = b"new-binary"
        utils.install_rpc_proxy("1.2.3")

    assert binpath.read_bytes() == b"new-binary"
    assert binpath.stat().st_ino != old_inode
    assert binpath.stat().st_mode & 0o111
    assert list(tmp_path.iterdir()) == [binpath]


def test_install_rpc_proxy_failed_download_leaves_no_file(tmp_path):
    # A 404 (e.g. unreleased version) must not leave a partial file behind,
    # or rpc_proxy_binary_installed() would report a garbage binary as installed.
    binpath = tmp_path / "bitcoin-rpc-proxy"
    with (
        mock.patch("utils.requests.get") as get,
        mock.patch("utils.c.RPC_PROXY_BINARY_PATH", binpath),
        mock.patch("utils.chown") as chown,
    ):
        get.return_value.raise_for_status.side_effect = requests.HTTPError("404")
        with pytest.raises(requests.HTTPError):
            utils.install_rpc_proxy("9.9.9")

    assert not binpath.exists()
    chown.assert_not_called()


def test_install_rpc_proxy_is_noop_without_version():
    # The install hook always calls this; an empty version must not download anything.
    with mock.patch("utils.requests.get") as get, mock.patch("utils.chown") as chown:
        utils.install_rpc_proxy("")
    get.assert_not_called()
    chown.assert_not_called()
