import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
RUN_VERIFIER = ROOT / ".github" / "scripts" / "verify_integration_run.py"
CHARMHUB_VERIFIER = ROOT / ".github" / "scripts" / "verify_charmhub_release.py"
UNIFIED_RELEASE_VERIFIER = ROOT / ".github" / "scripts" / "verify_unified_release.py"
PROVENANCE_WRITER = ROOT / ".github" / "scripts" / "write_charm_provenance.py"
SOURCE_SHA = "1" * 40
CI_WORKFLOW = ROOT / ".github" / "workflows" / "charm-ci.yml"
CHARM_RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "charm-release.yml"


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


def _unified_release_args(tmp_path: Path) -> tuple[str, ...]:
    version_file = tmp_path / "VERSION"
    changelog_file = tmp_path / "CHANGELOG.md"
    version_file.write_text("0.5.0\n", encoding="utf-8")
    changelog_file.write_text(
        "# Changelog\n\n"
        "## [0.5.0] - 2026-08-14\n\n"
        "### Charm\n\n"
        "- Publish exact tested charm artifacts.\n\n"
        "### Proxy\n\n"
        "No changes this release.\n\n"
        "## [0.4.0] - 2026-07-06\n",
        encoding="utf-8",
    )
    return (
        "--source-sha",
        SOURCE_SHA,
        "--tag-commit",
        SOURCE_SHA,
        "--version-file",
        str(version_file),
        "--changelog-file",
        str(changelog_file),
    )


def test_unified_release_verifier_accepts_exact_final_release(tmp_path):
    release = {"tagName": "v0.5.0", "isDraft": False, "isPrerelease": False}

    result = _run(UNIFIED_RELEASE_VERIFIER, release, *_unified_release_args(tmp_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v0.5.0"


@pytest.mark.parametrize(
    "release",
    [
        {"tagName": "v0.5.1", "isDraft": False, "isPrerelease": False},
        {"tagName": "v0.5.0", "isDraft": True, "isPrerelease": False},
        {"tagName": "v0.5.0", "isDraft": False, "isPrerelease": True},
    ],
)
def test_unified_release_verifier_rejects_wrong_release(release, tmp_path):
    result = _run(UNIFIED_RELEASE_VERIFIER, release, *_unified_release_args(tmp_path))

    assert result.returncode != 0


def test_unified_release_verifier_rejects_wrong_tag_commit(tmp_path):
    args = list(_unified_release_args(tmp_path))
    args[args.index("--tag-commit") + 1] = "2" * 40

    result = _run(
        UNIFIED_RELEASE_VERIFIER,
        {"tagName": "v0.5.0", "isDraft": False, "isPrerelease": False},
        *args,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    "changelog",
    [
        "## [0.5.0]\n\n### Charm\n\n- Changed.\n",
        "## [0.5.0]\n\n### Proxy\n\nNo changes this release.\n",
        "## [0.5.0]\n\n### Charm\n\n### Proxy\n\nNo changes this release.\n",
    ],
)
def test_unified_release_verifier_requires_both_changelog_verticals(changelog, tmp_path):
    args = list(_unified_release_args(tmp_path))
    Path(args[args.index("--changelog-file") + 1]).write_text(changelog, encoding="utf-8")

    result = _run(
        UNIFIED_RELEASE_VERIFIER,
        {"tagName": "v0.5.0", "isDraft": False, "isPrerelease": False},
        *args,
    )

    assert result.returncode != 0


def test_charm_publication_requires_unified_release_before_upload():
    workflow = yaml.safe_load(CHARM_RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    commands = [step.get("run", "") for step in steps]
    unified_gate = next(index for index, command in enumerate(commands) if "verify_unified_release.py" in command)
    charmhub_upload = next(index for index, command in enumerate(commands) if "charmcraft upload" in command)
    provenance_write = next(index for index, command in enumerate(commands) if "write_charm_provenance.py" in command)
    release_attach = next(
        index for index, command in enumerate(commands) if 'gh release upload "$RELEASE_TAG"' in command
    )
    charmhub_release = next(index for index, command in enumerate(commands) if "charmcraft release" in command)
    all_commands = "\n".join(commands)

    assert unified_gate < charmhub_upload
    assert charmhub_upload < provenance_write == release_attach < charmhub_release
    assert 'gh release create "charm-r' not in all_commands
    assert 'gh release upload "$RELEASE_TAG"' in all_commands


def test_provenance_writer_records_exact_artifact_binding(tmp_path):
    artifact = tmp_path / "bitcoin-rpc.charm"
    artifact.write_bytes(b"exact artifact")
    output = tmp_path / "provenance.json"
    result = subprocess.run(
        [
            sys.executable,
            str(PROVENANCE_WRITER),
            "--artifact",
            str(artifact),
            "--artifact-sha256",
            "bb655470961f968ede2e5cc439f4472fd1d4ba19e762b9aa71a9f4b90567c7b0",
            "--channel",
            "latest/candidate",
            "--revision",
            "17",
            "--integration-run-id",
            "12345",
            "--release-tag",
            "v0.5.0",
            "--source-sha",
            SOURCE_SHA,
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "artifact": "bitcoin-rpc.charm",
        "artifact_sha256": "bb655470961f968ede2e5cc439f4472fd1d4ba19e762b9aa71a9f4b90567c7b0",
        "channel": "latest/candidate",
        "charm": "bitcoin-rpc",
        "charmhub_revision": 17,
        "integration_run_id": 12345,
        "integration_workflow": ".github/workflows/charm-integration.yml",
        "release_tag": "v0.5.0",
        "source_sha": SOURCE_SHA,
    }


def test_pr_ci_builds_exact_head_and_verifies_packed_dispatch():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert "Record exact source commit" in workflow
    assert "charmcraft pack" in workflow
    assert "tests/verify_artifact_dispatch.py" in workflow
    assert "actions/upload-artifact" in workflow


def test_pr_ci_summary_uses_available_github_runner():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    summary_job = workflow.split("  summary:\n", maxsplit=1)[1]

    assert "    runs-on: ubuntu-24.04\n" in summary_job


def test_pr_ci_charm_tests_use_available_github_runner():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    charm_tests_job = workflow.split("  charm-tests:\n", maxsplit=1)[1].split("  exact-artifact:\n", maxsplit=1)[0]

    assert "    runs-on: ubuntu-24.04\n" in charm_tests_job
