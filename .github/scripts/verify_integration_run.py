#!/usr/bin/env python3
"""Verify that one downloaded artifact came from the required integration run."""

import argparse
import json
import sys
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-path", required=True)
    return parser.parse_args()


def _full_name(value: Any) -> str | None:
    return value.get("full_name") if isinstance(value, dict) else None


def main() -> int:
    """Validate the GitHub Actions run document from standard input."""
    args = _arguments()
    try:
        run = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"invalid GitHub Actions run JSON: {exc}", file=sys.stderr)
        return 1

    expected = {
        "id": args.run_id,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": args.source_sha,
        "path": args.workflow_path,
    }
    errors = [f"{key}: expected {value!r}, got {run.get(key)!r}" for key, value in expected.items() if run.get(key) != value]
    for key in ("repository", "head_repository"):
        actual = _full_name(run.get(key))
        if actual != args.repository:
            errors.append(f"{key}: expected {args.repository!r}, got {actual!r}")
    if errors:
        print("integration run provenance mismatch: " + "; ".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
