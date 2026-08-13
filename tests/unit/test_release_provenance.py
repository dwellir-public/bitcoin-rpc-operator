import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RUN_VERIFIER = ROOT / ".github" / "scripts" / "verify_integration_run.py"
CHARMHUB_VERIFIER = ROOT / ".github" / "scripts" / "verify_charmhub_release.py"
SOURCE_SHA = "1" * 40
CI_WORKFLOW = ROOT / ".github" / "workflows" / "charm-ci.yml"


def _run(script: Path, payload: object, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def _integration_run() -> dict:
    return {
        "id": 12345,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": SOURCE_SHA,
        "path": ".github/workflows/charm-integration.yml",
        "repository": {"full_name": "dwellir-public/bitcoin-rpc-operator"},
        "head_repository": {"full_name": "dwellir-public/bitcoin-rpc-operator"},
    }


def _run_args() -> tuple[str, ...]:
    return (
        "--repository",
        "dwellir-public/bitcoin-rpc-operator",
        "--run-id",
        "12345",
        "--source-sha",
        SOURCE_SHA,
        "--workflow-path",
        ".github/workflows/charm-integration.yml",
    )


def test_integration_run_verifier_accepts_exact_successful_main_run():
    result = _run(RUN_VERIFIER, _integration_run(), *_run_args())

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 99999),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("event", "push"),
        ("head_branch", "feature"),
        ("head_sha", "2" * 40),
        ("path", ".github/workflows/other.yml"),
        ("repository", {"full_name": "attacker/fork"}),
        ("head_repository", {"full_name": "attacker/fork"}),
    ],
)
def test_integration_run_verifier_rejects_mismatched_provenance(field, value):
    payload = _integration_run()
    payload[field] = value

    result = _run(RUN_VERIFIER, payload, *_run_args())

    assert result.returncode != 0


def _charmhub_status() -> list[dict]:
    return [
        {
            "track": "latest",
            "mappings": [
                {
                    "base": {"name": "ubuntu", "channel": "24.04", "architecture": "amd64"},
                    "releases": [
                        {
                            "status": "open",
                            "channel": "latest/edge",
                            "version": "0.4.0",
                            "revision": 17,
                            "resources": [],
                            "expires_at": None,
                        }
                    ],
                }
            ],
        }
    ]


def _charmhub_args() -> tuple[str, ...]:
    return (
        "--revision",
        "17",
        "--channel",
        "latest/edge",
        "--base-name",
        "ubuntu",
        "--base-channel",
        "24.04",
        "--architecture",
        "amd64",
    )


def test_charmhub_verifier_binds_revision_channel_and_base():
    result = _run(CHARMHUB_VERIFIER, _charmhub_status(), *_charmhub_args())

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--revision", "18"),
        ("--channel", "latest/stable"),
        ("--base-name", "debian"),
        ("--base-channel", "22.04"),
        ("--architecture", "arm64"),
    ],
)
def test_charmhub_verifier_rejects_wrong_release_binding(argument, value):
    args = list(_charmhub_args())
    args[args.index(argument) + 1] = value

    result = _run(CHARMHUB_VERIFIER, _charmhub_status(), *args)

    assert result.returncode != 0


def test_pr_ci_builds_exact_head_and_verifies_packed_dispatch():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert "Record exact source commit" in workflow
    assert "charmcraft pack" in workflow
    assert "tests/verify_artifact_dispatch.py" in workflow
    assert "actions/upload-artifact" in workflow
