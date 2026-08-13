"""Install and replace Bitcoin Core release binaries safely."""

import hashlib
import os
import re
import subprocess as sp
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

import requests

import constants as c


def _release_urls(version: str) -> tuple[str, str]:
    release_url = c.DL_URL.replace("VERSION", version)
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
            f"staged Bitcoin Core version does not match expected {expected_version}: "
            f"{result.stdout.splitlines()[0] if result.stdout else 'empty output'}"
        )


def _restore(destinations: dict[Path, Path | None]) -> None:
    for destination, backup in destinations.items():
        destination.unlink(missing_ok=True)
        if backup is not None and backup.exists():
            os.replace(backup, destination)


def _activate(
    version: str,
    destinations: dict[Path, Path],
    stage_dir: Path,
    *,
    is_running: Callable[[], bool],
    stop: Callable[[], None],
    start: Callable[[], None],
    is_healthy: Callable[[], bool],
) -> None:
    was_running = is_running()
    if was_running:
        stop()

    backups: dict[Path, Path | None] = {}
    activated = False
    try:
        for destination, source in destinations.items():
            backup = stage_dir / f"{destination.name}.previous" if destination.exists() else None
            if backup is not None:
                os.replace(destination, backup)
            backups[destination] = backup
            os.replace(source, destination)
        activated = True
        if was_running:
            start()
            if not is_healthy():
                raise RuntimeError(f"Bitcoin Core {version} failed its health check")
    except BaseException:
        if activated and was_running:
            try:
                stop()
            except BaseException:
                pass
        _restore(backups)
        if was_running:
            start()
        raise


def install_release(
    version: str,
    binary_path: Path,
    cli_path: Path,
    *,
    is_running: Callable[[], bool],
    stop: Callable[[], None],
    start: Callable[[], None],
    is_healthy: Callable[[], bool],
) -> None:
    """Verify, stage, activate, and health-check one Bitcoin Core release.

    Downloads and validation finish before service interruption. A failed swap,
    start, or health check restores both previous binaries. A stopped service
    remains stopped.
    """
    if not version:
        return
    release_url, checksum_url = _release_urls(version)
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

        destinations = {binary_path: staged[c.BINARY_NAME], cli_path: staged[c.CLI_NAME]}
        _activate(
            version,
            destinations,
            stage_dir,
            is_running=is_running,
            stop=stop,
            start=start,
            is_healthy=is_healthy,
        )
