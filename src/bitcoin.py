"""Install and replace Bitcoin Core release binaries safely."""

import hashlib
import os
import re
import subprocess as sp
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import requests

import constants as c


def _release_urls(version: str, binary_url: str = "") -> tuple[str, str]:
    if re.fullmatch(r"\d+(?:\.\d+){1,2}", version) is None:
        raise ValueError("version must contain two or three numeric components")
    expected_filename = f"bitcoin-{version}-x86_64-linux-gnu.tar.gz"
    release_url = binary_url.strip() or c.DL_URL.replace("VERSION", version)
    parsed = urlsplit(release_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or Path(parsed.path).name != expected_filename
    ):
        raise ValueError(
            f"binary-url must be an HTTPS URL without credentials, query, or fragment, ending in {expected_filename}"
        )
    checksum_url = release_url.rsplit("/", 1)[0] + "/SHA256SUMS"
    return release_url, checksum_url


def _expected_checksum(payload: str, filename: str) -> str:
    for line in payload.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match and match.group(2) == filename:
            return match.group(1).casefold()
    raise ValueError(f"SHA256SUMS has no entry for {filename}")


def _extract_binaries(archive_path: Path, stage_dir: Path, version: str) -> dict[str, Path]:
    expected = {c.BINARY_NAME, c.CLI_NAME}
    extracted: dict[str, Path] = {}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.name not in expected or path.parent.name != "bin" or not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            target = stage_dir / path.name
            with target.open("wb") as output:
                output.write(source.read())
            target.chmod(0o755)
            extracted[path.name] = target
    missing = sorted(expected - extracted.keys())
    if missing:
        raise ValueError(f"Bitcoin Core {version} archive is missing {', '.join(missing)}")
    return extracted


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.search(r"(?:^|\s)v?(\d+(?:\.\d+)+)", value)
    if match is None:
        raise ValueError(f"cannot parse Bitcoin Core version from {value!r}")
    parts = [int(part) for part in match.group(1).split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _validate_version(binary_path: Path, expected_version: str) -> None:
    result = sp.run([binary_path, "--version"], capture_output=True, check=True, text=True)
    if _version_parts(result.stdout) != _version_parts(expected_version):
        raise ValueError(
            f"staged {binary_path.name} version does not match expected {expected_version}: "
            f"{result.stdout.splitlines()[0] if result.stdout else 'empty output'}"
        )


def _restore(destinations: dict[Path, Path | None]) -> None:
    for destination, backup in destinations.items():
        destination.unlink(missing_ok=True)
        if backup is not None and backup.exists():
            os.replace(backup, destination)


def _stop_for_activation(stop: Callable[[], None], start: Callable[[], None]) -> None:
    try:
        stop()
    except BaseException as exc:
        try:
            start()
        except BaseException as recovery_exc:
            raise RuntimeError(f"failed to recover Bitcoin Core after stop failure: {recovery_exc}") from exc
        raise


def _swap_binaries(destinations: dict[Path, Path], stage_dir: Path, backups: dict[Path, Path | None]) -> None:
    for destination, source in destinations.items():
        backup = stage_dir / f"{destination.name}.previous" if destination.exists() else None
        if backup is not None:
            os.replace(destination, backup)
        backups[destination] = backup
        os.replace(source, destination)


def _verify_running_version(version: str, wait_for_running_version: Callable[[], str | None]) -> None:
    running_version = wait_for_running_version()
    if running_version is None:
        raise RuntimeError(f"Bitcoin Core {version} did not become RPC-ready")
    if _version_parts(running_version) != _version_parts(version):
        raise RuntimeError(f"running Bitcoin Core version does not match expected {version}: {running_version}")


def _rollback(
    backups: dict[Path, Path | None],
    *,
    stop_replacement: bool,
    start_previous: bool,
    stop: Callable[[], None],
    start: Callable[[], None],
    cause: BaseException,
) -> None:
    rollback_errors = []
    operations = []
    if stop_replacement:
        operations.append(("stop replacement", stop))
    operations.append(("restore binaries", lambda: _restore(backups)))
    if start_previous:
        operations.append(("start previous version", start))
    for name, operation in operations:
        try:
            operation()
        except BaseException as rollback_exc:
            rollback_errors.append(f"{name}: {rollback_exc}")
    if rollback_errors:
        raise RuntimeError(f"Bitcoin Core rollback failed ({'; '.join(rollback_errors)})") from cause


def _activate(
    version: str,
    destinations: dict[Path, Path],
    stage_dir: Path,
    *,
    is_running: Callable[[], bool],
    stop: Callable[[], None],
    start: Callable[[], None],
    wait_for_running_version: Callable[[], str | None],
) -> None:
    was_running = is_running()
    if was_running:
        _stop_for_activation(stop, start)

    backups: dict[Path, Path | None] = {}
    activated = False
    try:
        _swap_binaries(destinations, stage_dir, backups)
        activated = True
        if was_running:
            start()
            _verify_running_version(version, wait_for_running_version)
    except BaseException as exc:
        _rollback(
            backups,
            stop_replacement=activated and was_running,
            start_previous=was_running,
            stop=stop,
            start=start,
            cause=exc,
        )
        raise


def install_release(
    version: str,
    binary_path: Path,
    cli_path: Path,
    *,
    binary_url: str = "",
    is_running: Callable[[], bool],
    stop: Callable[[], None],
    start: Callable[[], None],
    wait_for_running_version: Callable[[], str | None],
) -> None:
    """Verify, stage, activate, and health-check one Bitcoin Core release.

    Downloads and validation finish before service interruption. A failed swap,
    start, or health check restores both previous binaries. A stopped service
    remains stopped.
    """
    if not version:
        return
    release_url, checksum_url = _release_urls(version, binary_url)
    release = requests.get(release_url, timeout=600)
    release.raise_for_status()
    sums = requests.get(checksum_url, timeout=60)
    sums.raise_for_status()
    filename = release_url.rsplit("/", 1)[1]
    expected = _expected_checksum(sums.text, filename)
    actual = hashlib.sha256(release.content).hexdigest()
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {filename}: expected {expected}, got {actual}")

    binary_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=binary_path.parent, prefix=".bitcoin-stage-") as tmp:
        stage_dir = Path(tmp)
        archive_path = stage_dir / filename
        archive_path.write_bytes(release.content)
        staged = _extract_binaries(archive_path, stage_dir, version)
        _validate_version(staged[c.BINARY_NAME], version)
        _validate_version(staged[c.CLI_NAME], version)

        destinations = {binary_path: staged[c.BINARY_NAME], cli_path: staged[c.CLI_NAME]}
        _activate(
            version,
            destinations,
            stage_dir,
            is_running=is_running,
            stop=stop,
            start=start,
            wait_for_running_version=wait_for_running_version,
        )
