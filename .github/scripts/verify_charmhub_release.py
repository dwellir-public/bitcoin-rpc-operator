#!/usr/bin/env python3
"""Verify one Charmhub revision, channel, and base mapping."""

import argparse
import json
import sys
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--base-name", required=True)
    parser.add_argument("--base-channel", required=True)
    parser.add_argument("--architecture", required=True)
    return parser.parse_args()


def _matches(status: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    if not isinstance(status, list):
        return []
    expected_base = {
        "name": args.base_name,
        "channel": args.base_channel,
        "architecture": args.architecture,
    }
    matches = []
    for track in status:
        if not isinstance(track, dict):
            continue
        for mapping in track.get("mappings", []):
            if not isinstance(mapping, dict) or mapping.get("base") != expected_base:
                continue
            for release in mapping.get("releases", []):
                if not isinstance(release, dict):
                    continue
                if (
                    release.get("status") == "open"
                    and release.get("channel") == args.channel
                    and release.get("revision") == args.revision
                ):
                    matches.append(release)
    return matches


def main() -> int:
    """Validate Charmcraft status JSON from standard input."""
    args = _arguments()
    try:
        status = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"invalid Charmhub status JSON: {exc}", file=sys.stderr)
        return 1
    matches = _matches(status, args)
    if len(matches) != 1:
        print(
            "Charmhub status does not contain exactly one open release for the requested revision, channel, and base",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
