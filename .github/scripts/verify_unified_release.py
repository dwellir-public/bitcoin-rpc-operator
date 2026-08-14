#!/usr/bin/env python3
"""Verify the unified release gate for charm publication."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SEMVER = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--tag-commit", required=True)
    parser.add_argument("--version-file", required=True, type=Path)
    parser.add_argument("--changelog-file", required=True, type=Path)
    return parser.parse_args()


def _release_section(changelog: str, version: str) -> str | None:
    heading = re.search(rf"^## \[{re.escape(version)}\](?:\s+-.*)?\s*$", changelog, re.MULTILINE)
    if heading is None:
        return None
    following = re.search(r"^## \[", changelog[heading.end() :], re.MULTILINE)
    end = heading.end() + following.start() if following else len(changelog)
    return changelog[heading.end() : end]


def _vertical_has_body(section: str, vertical: str) -> bool:
    heading = re.search(rf"^### {re.escape(vertical)}\s*$", section, re.MULTILINE)
    if heading is None:
        return False
    following = re.search(r"^#{1,3} ", section[heading.end() :], re.MULTILINE)
    end = heading.end() + following.start() if following else len(section)
    return bool(section[heading.end() : end].strip())


def _load_release() -> dict[str, Any]:
    release = json.load(sys.stdin)
    if not isinstance(release, dict):
        raise ValueError("GitHub release JSON must be an object")
    return release


def main() -> int:
    """Validate VERSION, changelog, tag commit, and GitHub release state."""
    args = _arguments()
    errors: list[str] = []
    try:
        version = args.version_file.read_text(encoding="utf-8").strip().removeprefix("v")
        changelog = args.changelog_file.read_text(encoding="utf-8")
        release = _load_release()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"invalid unified release input: {exc}", file=sys.stderr)
        return 1

    if not SEMVER.fullmatch(version):
        errors.append(f"VERSION is not SemVer: {version!r}")
    if not SHA.fullmatch(args.source_sha):
        errors.append("source SHA must contain 40 lowercase hexadecimal characters")
    if not SHA.fullmatch(args.tag_commit):
        errors.append("tag commit must contain 40 lowercase hexadecimal characters")
    if args.tag_commit != args.source_sha:
        errors.append(f"unified tag commit {args.tag_commit!r} does not match source {args.source_sha!r}")

    tag = f"v{version}"
    if release.get("tagName") != tag:
        errors.append(f"GitHub release tag must be {tag!r}")
    if release.get("isDraft") is not False:
        errors.append("GitHub release must not be a draft")
    if release.get("isPrerelease") is not False:
        errors.append("GitHub release must be final")

    section = _release_section(changelog, version)
    if section is None:
        errors.append(f"CHANGELOG.md has no [{version}] section")
    else:
        for vertical in ("Charm", "Proxy"):
            if not _vertical_has_body(section, vertical):
                errors.append(f"CHANGELOG.md [{version}] has no non-empty {vertical} section")

    if errors:
        print("unified release mismatch: " + "; ".join(errors), file=sys.stderr)
        return 1
    print(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
