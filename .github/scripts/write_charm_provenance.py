#!/usr/bin/env python3
"""Write provenance for one exact integration-tested charm artifact."""

import argparse
import hashlib
import json
import re
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TAG = re.compile(r"^v[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$")


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--revision", required=True, type=_positive)
    parser.add_argument("--integration-run-id", required=True, type=_positive)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    """Validate and write one exact charm provenance document."""
    args = _arguments()
    if not DIGEST.fullmatch(args.artifact_sha256):
        raise SystemExit("artifact SHA-256 must contain 64 lowercase hexadecimal characters")
    if not SHA.fullmatch(args.source_sha):
        raise SystemExit("source SHA must contain 40 lowercase hexadecimal characters")
    if not TAG.fullmatch(args.release_tag):
        raise SystemExit("release tag must be v<SemVer>")
    actual_digest = hashlib.sha256(args.artifact.read_bytes()).hexdigest()
    if actual_digest != args.artifact_sha256:
        raise SystemExit(f"artifact SHA-256 mismatch: expected {args.artifact_sha256}, got {actual_digest}")

    provenance = {
        "artifact": args.artifact.name,
        "artifact_sha256": actual_digest,
        "channel": args.channel,
        "charm": "bitcoin-rpc",
        "charmhub_revision": args.revision,
        "integration_run_id": args.integration_run_id,
        "integration_workflow": ".github/workflows/charm-integration.yml",
        "release_tag": args.release_tag,
        "source_sha": args.source_sha,
    }
    args.output.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
