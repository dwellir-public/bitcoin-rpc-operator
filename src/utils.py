#!/usr/bin/env python3

"""Helper functions for installing and operating the Bitcoin client."""

import logging
import os
import re
import shutil
import subprocess as sp
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import ops
import requests

import bitcoin
import constants as c

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FileState:
    data: bytes | None
    mode: int | None


@dataclass(frozen=True)
class _ConfigTransactionSnapshot:
    files: dict[Path, _FileState]
    running: dict[str, bool]


# CONFIG AND INSTALLATION


def chown() -> None:
    """Recursively set ownership of the charm's home directory to its user."""
    sp.run(["chown", "-R", f"{c.USER}:{c.USER}", c.HOME_DIR])


def create_user():
    """Create the charm's user."""
    sp.run(["addgroup", "--system", c.USER])
    sp.run(["adduser", "--system", "--home", c.HOME_DIR, "--disabled-password", "--ingroup", c.USER, c.USER])
    sp.run(["chmod", "700", c.HOME_DIR])
    chown()


def install_bitcoin(version: str, config: Mapping[str, object], *, binary_url: str = ""):
    """Install the bitcoind daemon and the bitcoin-cli RPC client.

    Both binaries come from the same release tarball: bitcoind runs as the node
    service, and bitcoin-cli lets operators reach RPCs the proxy blocks (e.g.
    loadtxoutset) directly on bitcoind's loopback RPC.

    A no-op when version is empty (the config default), so a deploy without
    `version` set lands in BlockedStatus instead of erroring the install hook.
    """
    bitcoin.install_release(
        version,
        c.BINARY_PATH,
        c.CLI_PATH,
        binary_url=binary_url,
        is_running=lambda: get_status(c.SERVICE_NAME),
        stop=stop_service,
        start=start_service,
        wait_for_running_version=lambda: wait_for_running_version(config),
    )
    if not version:
        return
    chown()


def install_dependencies() -> None:
    """Install the charm's apt and pip dependencies."""
    # apt
    logger.info("Installing dependencies via apt.")
    sp.run(["apt", "update"], check=False)
    command = ["apt", "install", "-y"]
    packages = c.APT_PACKAGES
    command.extend(packages)
    sp.run(command, check=True)
    # pip (into a dedicated venv; the system Python is externally managed on
    # Ubuntu 24.04, so a bare `pip install` would fail PEP 668).
    logger.info("Installing monitor dependencies into a venv.")
    c.MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    sp.run(["python3", "-m", "venv", str(c.MONITOR_VENV_DIR)], check=True)
    command = [str(c.MONITOR_VENV_PIP), "install"]
    command.extend(c.PIP_PACKAGES)
    sp.run(command, check=True)
    chown()


def install_service_file(source_path: str, service_name: str) -> bool:
    """Install a systemd service file, reloading systemd. Return True if it changed.

    A no-op (and no daemon-reload) when the installed unit already matches the
    source, so callers can skip a service restart on an unchanged unit.
    """
    logger.debug(f"Installing service file '{service_name.lower()}.service'")
    target_path = Path(f"/etc/systemd/system/{service_name.lower()}.service")
    new_content = Path(source_path).read_text(encoding="utf-8")
    if target_path.exists() and target_path.read_text(encoding="utf-8") == new_content:
        return False
    shutil.copyfile(source_path, target_path)
    sp.run(["systemctl", "daemon-reload"], check=False)
    return True


def install_bitcoind_monitor(config: ops.ConfigData, restart_service: bool) -> None:
    """Install the bitcoind monitor script and write its environment file."""
    c.MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(f"templates/{c.MONITOR_SCRIPT_NAME}", c.MONITOR_SCRIPT_PATH)
    write_monitor_env_file(config)
    if restart_service:
        restart_monitor()


def install_rpc_proxy(version: str) -> None:
    """Download and install the bitcoin-rpc-proxy binary by version.

    A no-op when version is empty, so the install hook can lay down the proxy's
    service unit unconditionally without forcing a binary download.
    """
    if not version:
        logger.info("No rpc-proxy-version set; skipping proxy binary download.")
        return
    url = c.RPC_PROXY_DL_URL.replace("VERSION", version)
    # Download into memory and only write on success, so a failed download
    # never leaves a partial file that rpc_proxy_binary_installed() would
    # mistake for a working binary.
    response = requests.get(url, timeout=600)
    response.raise_for_status()
    # Write to a temp file in the same directory and atomically replace, so we
    # never write in place over the running binary (the kernel rejects that with
    # ETXTBSY). os.replace swaps the directory entry to a fresh inode; the running
    # process keeps its old, now-unlinked inode until it is restarted.
    dest = c.RPC_PROXY_BINARY_PATH
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(response.content)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    chown()


def install_rpc_proxy_service(config: ops.ConfigData, restart_service: bool) -> None:
    """Install the proxy's systemd unit and environment file; optionally restart."""
    install_service_file(f"templates/{c.RPC_PROXY_SERVICE_NAME}.service", c.RPC_PROXY_SERVICE_NAME)
    write_rpc_proxy_env_file(config)
    if restart_service:
        restart_rpc_proxy()


def _transaction_paths() -> tuple[Path, ...]:
    """Return every file a config transaction can replace."""
    return (
        c.BINARY_PATH,
        c.CLI_PATH,
        c.RPC_PROXY_BINARY_PATH,
        Path(f"/etc/default/{c.SERVICE_NAME}"),
        Path(f"/etc/default/{c.MONITOR_SERVICE_NAME}"),
        Path(f"/etc/default/{c.RPC_PROXY_SERVICE_NAME}"),
        c.MONITOR_SCRIPT_PATH,
        Path(f"/etc/systemd/system/{c.SERVICE_NAME}.service"),
        Path(f"/etc/systemd/system/{c.MONITOR_SERVICE_NAME}.service"),
        Path(f"/etc/systemd/system/{c.RPC_PROXY_SERVICE_NAME}.service"),
    )


def _capture_config_transaction() -> _ConfigTransactionSnapshot:
    files = {
        path: _FileState(
            data=path.read_bytes() if path.exists() else None,
            mode=(path.stat().st_mode & 0o7777) if path.exists() else None,
        )
        for path in _transaction_paths()
    }
    running = {
        service: get_status(service) for service in (c.SERVICE_NAME, c.MONITOR_SERVICE_NAME, c.RPC_PROXY_SERVICE_NAME)
    }
    return _ConfigTransactionSnapshot(files=files, running=running)


def _restore_file(path: Path, state: _FileState) -> None:
    if state.data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.rollback-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(state.data)
        if state.mode is not None:
            os.chmod(temporary, state.mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _restored_service_errors(snapshot: _ConfigTransactionSnapshot, previous_config: Mapping[str, object]) -> list[str]:
    """Return readiness errors for services that ran before the transaction."""
    errors = []
    if snapshot.running[c.SERVICE_NAME]:
        try:
            if wait_for_running_version(previous_config) is None:
                errors.append("node readiness: Bitcoin Core RPC did not recover")
        except BaseException as exc:
            errors.append(f"node readiness: {exc}")
    for service, name in (
        (c.MONITOR_SERVICE_NAME, "monitor readiness"),
        (c.RPC_PROXY_SERVICE_NAME, "proxy readiness"),
    ):
        if snapshot.running[service] and not service_running(service):
            errors.append(f"{name}: service did not recover")
    return errors


def _rollback_config_transaction(
    snapshot: _ConfigTransactionSnapshot,
    cause: BaseException,
    previous_config: Mapping[str, object],
) -> None:
    errors = []
    operations = (
        ("stop proxy", stop_rpc_proxy),
        ("stop monitor", stop_monitor),
        ("stop node", stop_service),
    )
    for name, operation in operations:
        try:
            operation()
        except BaseException as exc:
            errors.append(f"{name}: {exc}")
    for path, state in snapshot.files.items():
        try:
            _restore_file(path, state)
        except BaseException as exc:
            errors.append(f"restore {path.name}: {exc}")
    try:
        sp.run(["systemctl", "daemon-reload"], check=True)
    except BaseException as exc:
        errors.append(f"daemon-reload: {exc}")
    starts = (
        (c.SERVICE_NAME, "start node", start_service),
        (c.MONITOR_SERVICE_NAME, "start monitor", start_monitor),
        (c.RPC_PROXY_SERVICE_NAME, "start proxy", start_rpc_proxy),
    )
    for service, name, operation in starts:
        if not snapshot.running[service]:
            continue
        try:
            operation()
        except BaseException as exc:
            errors.append(f"{name}: {exc}")
    errors.extend(_restored_service_errors(snapshot, previous_config))
    if errors:
        raise RuntimeError(f"config rollback failed ({'; '.join(errors)})") from cause


def _write_config_generation(
    config: Mapping[str, object],
    changed_keys: set[str],
    *,
    credentials_changed: bool,
    node_changed: bool,
    proxy_changed: bool,
) -> None:
    """Write one internally consistent set of managed files."""
    if "rpc-proxy-version" in changed_keys:
        install_rpc_proxy(str(config.get("rpc-proxy-version") or ""))
    if node_changed:
        update_service_args(config, restart_service=False)  # type: ignore[arg-type]
    if credentials_changed:
        install_bitcoind_monitor(config, restart_service=False)  # type: ignore[arg-type]
    if proxy_changed:
        install_rpc_proxy_service(config, restart_service=False)  # type: ignore[arg-type]


def _activate_config_generation(
    config: Mapping[str, object],
    changed_keys: set[str],
    snapshot: _ConfigTransactionSnapshot,
    *,
    credentials_changed: bool,
    node_changed: bool,
    proxy_changed: bool,
) -> None:
    """Activate one written generation and prove each prior service recovered."""
    if {"version", "binary-url"} & changed_keys:
        install_bitcoin(
            str(config.get("version") or ""),
            config,
            binary_url=str(config.get("binary-url") or ""),
        )
    elif node_changed and snapshot.running[c.SERVICE_NAME]:
        restart_service()
        if wait_for_running_version(config) is None:
            raise RuntimeError("Bitcoin Core did not become RPC-ready after config change")

    if credentials_changed and snapshot.running[c.MONITOR_SERVICE_NAME]:
        restart_monitor()
        if not service_running(c.MONITOR_SERVICE_NAME):
            raise RuntimeError("bitcoind monitor did not become ready after config change")
    if proxy_changed and snapshot.running[c.RPC_PROXY_SERVICE_NAME]:
        if not rpc_proxy_binary_installed():
            raise RuntimeError("RPC proxy binary is missing after config change")
        restart_rpc_proxy()
        if not service_running(c.RPC_PROXY_SERVICE_NAME):
            raise RuntimeError("RPC proxy did not become ready after config change")
    if snapshot.running[c.SERVICE_NAME] and not service_running(c.SERVICE_NAME):
        raise RuntimeError("Bitcoin Core did not remain running after config change")


def apply_config_transaction(
    config: Mapping[str, object],
    *,
    previous_config: Mapping[str, object],
    changed_keys: set[str],
) -> None:
    """Apply one config generation and restore every managed file on failure."""
    if not changed_keys:
        return
    snapshot = _capture_config_transaction()
    credentials_changed = bool({"rpc-user", "rpc-password"} & changed_keys)
    node_changed = bool(
        {"rpc-user", "rpc-password", "service-args", "disable-wallet", "version", "binary-url"} & changed_keys
    )
    proxy_changed = credentials_changed or bool(
        {
            "rpc-proxy-filter",
            "rpc-proxy-version",
            "rpc-proxy-listen",
            "rpc-proxy-extend-allowlist",
        }
        & changed_keys
    )
    try:
        _write_config_generation(
            config,
            changed_keys,
            credentials_changed=credentials_changed,
            node_changed=node_changed,
            proxy_changed=proxy_changed,
        )
        _activate_config_generation(
            config,
            changed_keys,
            snapshot,
            credentials_changed=credentials_changed,
            node_changed=node_changed,
            proxy_changed=proxy_changed,
        )
    except BaseException as exc:
        _rollback_config_transaction(snapshot, exc, previous_config)
        raise


# SERVICES


def get_status(service_name: str = c.SERVICE_NAME) -> bool:
    """Return True if the named systemd service reports a running status."""
    service_status = sp.run(["service", service_name.lower(), "status"], capture_output=True, check=False)
    if service_status.returncode != 0:
        return False
    return True


def restart_service():
    """Restart the bitcoind service."""
    sp.run(["systemctl", "restart", c.SERVICE_NAME], check=True)


def start_service():
    """Start the bitcoind service."""
    sp.run(["systemctl", "start", c.SERVICE_NAME], check=True)


def stop_service():
    """Stop the bitcoind service."""
    sp.run(["systemctl", "stop", c.SERVICE_NAME], check=True)


def restart_monitor():
    """Restart the bitcoind monitor service."""
    sp.run(["systemctl", "restart", c.MONITOR_SERVICE_NAME], check=True)


def start_monitor():
    """Start the bitcoind monitor service."""
    sp.run(["systemctl", "start", c.MONITOR_SERVICE_NAME], check=True)


def stop_monitor():
    """Stop the bitcoind monitor service."""
    sp.run(["systemctl", "stop", c.MONITOR_SERVICE_NAME], check=True)


def restart_rpc_proxy():
    """Restart the bitcoin-rpc-proxy service."""
    sp.run(["systemctl", "restart", c.RPC_PROXY_SERVICE_NAME], check=True)


def start_rpc_proxy():
    """Start the bitcoin-rpc-proxy service."""
    sp.run(["systemctl", "start", c.RPC_PROXY_SERVICE_NAME], check=True)


def stop_rpc_proxy():
    """Stop the bitcoin-rpc-proxy service."""
    sp.run(["systemctl", "stop", c.RPC_PROXY_SERVICE_NAME], check=True)


def service_running(service_name: str, iterations: int = 4) -> bool:
    """Poll the named service and return True if it comes up within `iterations` tries."""
    for _ in range(iterations):
        if get_status(service_name):
            return True
        time.sleep(1)
    return False


def wait_for_running_version(config: Mapping[str, object], attempts: int = 30, interval: float = 2.0) -> str | None:
    """Poll loopback JSON-RPC and return the running client's version."""
    for attempt in range(attempts):
        try:
            response = requests.post(
                f"http://127.0.0.1:{c.BITCOIND_RPC_PORT}",
                auth=(str(config.get("rpc-user") or ""), str(config.get("rpc-password") or "")),
                json={"jsonrpc": "2.0", "id": "activation", "method": "getnetworkinfo", "params": []},
                timeout=5,
            )
            response.raise_for_status()
            body = response.json()
            result = body.get("result") if isinstance(body, dict) and not body.get("error") else None
            subversion = result.get("subversion") if isinstance(result, dict) else None
            match = re.search(r"Satoshi:([^/]+)/?", str(subversion or ""))
            if match:
                return match.group(1)
        except (requests.RequestException, TypeError, ValueError):
            logger.debug("Bitcoin Core RPC is not ready during binary activation")
        if attempt + 1 < attempts:
            time.sleep(interval)
    return None


def update_service_args(config: ops.ConfigData, restart_service: bool) -> bool:
    """Update the Bitcoin service arguments. Return True if the rendered file changed.

    When restart_service is True bitcoind is stopped before and started after the
    rewrite unconditionally (the version-change path relies on this to bring a
    deliberately stopped node back up). The returned change flag lets callers that
    pass restart_service=False decide whether a restart is actually warranted.
    """
    if restart_service:
        stop_service()
    # Get service args from config
    service_args = str(config.get("service-args") or "")

    # When the proxy is active, pin bitcoind's RPC to loopback (and optionally
    # disable the wallet) before appending credentials.
    service_args = harden_service_args(service_args, config)

    # Add rpc user and password if they exist
    # Note: will overwrite any values set for rpcuser and rpcpassword in service-args
    rpc_user = config.get("rpc-user")
    rpc_password = config.get("rpc-password")
    if rpc_user and rpc_password:
        service_args += f" -rpcuser={rpc_user} -rpcpassword={rpc_password}"

    env_path = Path(f"/etc/default/{c.SERVICE_NAME}")
    new_content = f'{c.SERVICE_NAME.upper()}_CLI_ARGS="{service_args}"'
    changed = not env_path.exists() or env_path.read_text(encoding="utf-8") != new_content
    env_path.write_text(new_content, encoding="utf-8")

    if restart_service:
        start_service()
    return changed


# bitcoind options that are additive (multiple values accumulate), so they must be
# removed from operator service-args rather than just overridden, before the charm
# forces its own loopback binding (D12). -rpcport is pinned too: the proxy upstream
# and the monitor target c.BITCOIND_RPC_PORT, so an operator override would silently
# break both.
_RPC_BIND_OPTS = {"rpcbind", "rpcallowip", "rpcport"}
# Wallet options that conflict with -disablewallet (D15).
_WALLET_OPTS = {"disablewallet", "wallet"}


def _opt_name(token: str) -> str:
    """Return a bitcoind option's bare name, dropping leading dashes, a 'no' negation prefix, and any =value."""
    name = token.lstrip("-").split("=", 1)[0]
    if name.startswith("no") and len(name) > 2:
        # bitcoind treats -noX as the negation of -X; normalize so both forms strip.
        name = name[2:]
    return name


def bitcoind_binary_installed() -> bool:
    """Return True when the bitcoind binary is present on disk."""
    return c.BINARY_PATH.exists()


def rpc_proxy_binary_installed() -> bool:
    """Return True when the proxy binary is present on disk.

    The proxy is always the node's RPC front door, but it can only be started
    once its binary has been downloaded. Service start/restart and unit status
    gate on this; the loopback pin does not (bitcoind is pinned unconditionally).
    """
    return c.RPC_PROXY_BINARY_PATH.exists()


def harden_service_args(service_args: str, config: ops.ConfigData) -> str:
    """Pin bitcoind RPC to loopback and optionally disable the wallet.

    The proxy is always bitcoind's front door, so this runs unconditionally:
    strips any additive -rpcbind/-rpcallowip and forces -rpcbind to both loopback
    families (127.0.0.1 and [::1], since naming one drops bitcoind's default dual
    bind); pins -rpcport to c.BITCOIND_RPC_PORT (the proxy upstream and monitor
    target it); when disable-wallet is set, strips conflicting wallet flags and
    adds -disablewallet.
    """
    tokens = [t for t in service_args.split() if _opt_name(t) not in _RPC_BIND_OPTS]
    tokens.append("-rpcbind=127.0.0.1")
    tokens.append("-rpcbind=[::1]")
    tokens.append(f"-rpcport={c.BITCOIND_RPC_PORT}")

    if config.get("disable-wallet"):
        tokens = [t for t in tokens if _opt_name(t) not in _WALLET_OPTS]
        tokens.append("-disablewallet")

    return " ".join(tokens)


def write_rpc_proxy_env_file(config: ops.ConfigData) -> None:
    """Write the proxy's environment file from RPC_PROXY_ENV with config overrides.

    PROXY_ADMIN_LISTEN is intentionally not overridable; it stays at the frozen
    RPC_PROXY_ADMIN_PORT default.
    """
    overrides = {
        "PROXY_LISTEN": config.get("rpc-proxy-listen"),
        "PROXY_EXTEND_ALLOWLIST": config.get("rpc-proxy-extend-allowlist"),
        "PROXY_UPSTREAM_USER": config.get("rpc-user"),
        "PROXY_UPSTREAM_PASSWORD": config.get("rpc-password"),
    }
    # PROXY_FILTER is a bool, so it is set explicitly (not via truthy override,
    # which would drop a deliberate "false").
    filter_value = "true" if config.get("rpc-proxy-filter") else "false"
    with open(f"/etc/default/{c.RPC_PROXY_SERVICE_NAME}", "w") as f:
        for key, value in c.RPC_PROXY_ENV.items():
            if key == "PROXY_FILTER":
                value = filter_value
            elif overrides.get(key):
                value = overrides[key]
            f.write(f"{key}={value}\n")


def write_monitor_env_file(config: ops.ConfigData) -> None:
    """Write the monitor's environment file from MONITOR_ENV with config overrides."""
    with open(f"/etc/default/{c.MONITOR_SERVICE_NAME}", "w") as f:
        for key, value in c.MONITOR_ENV.items():
            if key == "BITCOIN_RPC_PASSWORD" and config.get("rpc-password"):
                value = config.get("rpc-password")
            if key == "BITCOIN_RPC_USER" and config.get("rpc-user"):
                value = config.get("rpc-user")
            f.write(f"{key}={value}\n")


# HELPERS


def get_client_help_output() -> str:
    """Return the output of the client binary's --help, or an error message."""
    if c.BINARY_PATH.exists():
        process = sp.run([str(c.BINARY_PATH), "--help"], stdout=sp.PIPE, check=False)
        if process.returncode == 0:
            return process.stdout.decode("utf-8").strip()
        return "Could not parse client binary '--help' command"
    return "Client binary not found"


def get_client_proc_cmdline() -> str:
    """Return the running client's process command line, or an empty string."""
    command = ["pgrep", c.SERVICE_NAME]
    pgrep_output = sp.run(command, capture_output=True, check=False).stdout.decode("utf-8").split()
    if pgrep_output:
        proc_id = pgrep_output[0]
        command = ["cat", f"/proc/{proc_id}/cmdline"]  # Uses NUL bytes as delimiter
        cat_output = sp.run(command, capture_output=True, check=False).stdout.decode().split("\x00")
        str_output = " ".join(cat_output)
        return str_output
    return ""


def get_disk_usage(*paths: Path) -> str:
    """Return human-readable disk usage for each existing path."""
    disk_usages = []
    for path in paths:
        if not path.exists():
            logger.warning("Path '%s' not found while getting disk usage.", path)
            continue
        command = ["du", str(path), "-hs"]
        output = sp.run(command, stdout=sp.PIPE, check=False).stdout.decode("utf-8")
        match = re.search(r"(\d+(\.\d+)?[GKMT])", output)
        if match is None:
            logger.warning("Couldn't parse return from 'du' command: %s", output)
            continue
        disk_usages.append((str(path), match.group(1)))
    if disk_usages:
        return ", ".join(f"{path}: {size}" for path, size in disk_usages)
    return "error parsing disk usage"


def _finite_int(value: str) -> int | None:
    if value.casefold() == "infinity":
        return None
    parsed = int(value)
    return None if parsed >= 2**63 else parsed


def _timespan_microseconds(value: str) -> float | None:
    if value.casefold() == "infinity":
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h)?", value)
    if match is None:
        return None
    multipliers = {None: 1.0, "us": 1.0, "ms": 1_000.0, "s": 1_000_000.0, "min": 60_000_000.0, "h": 3_600_000_000.0}
    return float(match.group(1)) * multipliers[match.group(2)]


def get_systemd_limits(service_name: str) -> dict[str, int | float | None]:
    """Return normalized systemd resource limits for a workload service."""
    result = sp.run(
        [
            "systemctl",
            "show",
            service_name,
            "--property=MemoryMax,MemoryHigh,CPUQuotaPerSecUSec,TasksMax",
            "--no-pager",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    quota_us = _timespan_microseconds(values.get("CPUQuotaPerSecUSec", "infinity"))
    return {
        "memory_max_bytes": _finite_int(values.get("MemoryMax", "infinity")),
        "memory_high_bytes": _finite_int(values.get("MemoryHigh", "infinity")),
        "cpu_quota_percent": None if quota_us is None else quota_us / 10_000.0,
        "tasks_max": _finite_int(values.get("TasksMax", "infinity")),
    }


def get_readme() -> str:
    """Return the contents of the charm's README, or an empty string if missing."""
    path = Path("README.md")
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    logger.warning("README file not found.")
    return ""


def get_service_args() -> str:
    """Return the contents of the bitcoind service environment file."""
    command = ["cat", f"/etc/default/{c.SERVICE_NAME}"]
    cat_output = sp.run(command, capture_output=True, check=False).stdout.decode("utf-8").strip()
    return cat_output


def get_rpc_proxy_env() -> str:
    """Return the contents of the rpc-proxy service environment file."""
    command = ["cat", f"/etc/default/{c.RPC_PROXY_SERVICE_NAME}"]
    return sp.run(command, capture_output=True, check=False).stdout.decode("utf-8").strip()


def get_version() -> str:
    """Return the installed Bitcoin Core version, or an empty string if it can't be determined."""
    try:
        output = sp.run([c.BINARY_PATH, "--version"], check=True, capture_output=True, text=True).stdout
    except (FileNotFoundError, PermissionError, sp.CalledProcessError) as e:
        logger.error("Failed to get version from %s. Exception: %s", c.BINARY_PATH, e)
        return ""
    # Newer releases print "Bitcoin Core daemon version v31.0.0 bitcoind"; older
    # ones "Bitcoin Core version v22.0.0". Match both and capture just the version
    # token, dropping any trailing binary name.
    match = re.search(r"^Bitcoin Core (?:daemon )?version (\S+)", output, flags=re.MULTILINE)
    if match is None:
        logger.error("Failed to parse version from %s.", output)
        return ""
    return match.groups()[0]


def get_charm_version() -> str:
    """Return the charm's release version and commit, e.g. "v0.1.0-6-gd0d4771".

    `charm_version` is stamped at pack time by charmcraft's override-build (git
    describe). It falls back to the bundled VERSION file (tag only, no commit)
    when git was unavailable in the build sandbox, then to "unknown".
    """
    # Both filenames live at the charm root; hook CWD is the charm dir (see get_readme).
    for name in ("charm_version", "VERSION"):
        path = Path(name)
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return "unknown"


def get_rpc_proxy_version() -> str:
    """Return the installed bitcoin-rpc-proxy version, or an empty string if it can't be determined."""
    try:
        output = sp.run([c.RPC_PROXY_BINARY_PATH, "--version"], check=True, capture_output=True, text=True).stdout
    except (FileNotFoundError, PermissionError, sp.CalledProcessError) as e:
        logger.error("Failed to get version from %s. Exception: %s", c.RPC_PROXY_BINARY_PATH, e)
        return ""
    # Output: "bitcoin-rpc-proxy version 0.1.0 (commit 0324086, build time ...)".
    match = re.search(r"^bitcoin-rpc-proxy version (\S+)", output, flags=re.MULTILINE)
    if match is None:
        logger.error("Failed to parse proxy version from %s.", output)
        return ""
    return match.groups()[0]
