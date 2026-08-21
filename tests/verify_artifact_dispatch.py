#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def verify_dispatch_with_python_minor_drift(artifact: Path) -> None:
    """Run the packed dispatch after moving its site-packages directory."""
    with tempfile.TemporaryDirectory(prefix="bitcoin-rpc-artifact-") as temporary_directory:
        charm_root = Path(temporary_directory) / "charm"
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(charm_root)
        site_packages = list((charm_root / "venv/lib").glob("python*/site-packages"))
        if len(site_packages) != 1:
            raise SystemExit(f"expected one packaged site-packages directory, found {len(site_packages)}")
        packaged_python = site_packages[0].parent
        runtime_python = f"python{sys.version_info.major}.{sys.version_info.minor}"
        if packaged_python.name == runtime_python:
            packaged_python.rename(packaged_python.with_name("python0.0"))
        (charm_root / "src/charm.py").write_text(
            "import boto3, ops, requests\n"
            "from charms.dwellir.blockchain_common.v1.metadata import CollectorCredentials\n"
            "import bitcoin, bitcoin_metadata, constants, utils\n"
            "import interface_prometheus\n"
            "print('packed dispatch imports passed')\n"
        )
        (charm_root / "venv/bin/python").unlink(missing_ok=True)
        (charm_root / "dispatch").chmod(0o755)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(charm_root / "dispatch")],
            cwd=charm_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"packed dispatch failed with exit code {result.returncode}:\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        if result.stdout.strip() != "packed dispatch imports passed":
            raise SystemExit(f"unexpected dispatch output: {result.stdout!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        raise SystemExit(f"artifact does not exist: {artifact}")
    verify_dispatch_with_python_minor_drift(artifact)


if __name__ == "__main__":
    main()
