"""Tests for pipelines/scripts/notify_teams.sh (fake curl) and the structure of dbt-deploy.yml."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
NOTIFY = ROOT / "pipelines" / "scripts" / "notify_teams.sh"
DEPLOY_YML = ROOT / "pipelines" / "dbt-deploy.yml"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")

# Fake curl: saves the config read from stdin and the body file given as --data-binary @file.
FAKE_CURL = """#!/usr/bin/env bash
cat > "${FAKE_DIR}/curl_config"
echo "$@" > "${FAKE_DIR}/curl_args"
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--data-binary" ]]; then cp "${2#@}" "${FAKE_DIR}/body.json"; fi
  shift
done
[[ "${FAKE_FAIL:-}" == "1" ]] && exit 22
exit 0
"""

WEBHOOK = "https://example.invalid/webhook/SECRET-TOKEN"


@pytest.fixture
def box(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8", newline="\n")
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": tmp_path.as_posix(),
        "TEAMS_WEBHOOK_URL": WEBHOOK,
        "RUN_ID": "77",
    }
    return {"env": env, "dir": tmp_path}


def notify(box: dict, *extra: str, **env: str) -> subprocess.CompletedProcess:
    args = [
        "--run-url", "https://dev.azure.com/org/My Project/_build/results?buildId=77",
        "--env", "dev",
        "--branch", "dev",
        "--sha", "a" * 40,
        *extra,
    ]  # fmt: skip
    return subprocess.run(  # noqa: S603
        [BASH, NOTIFY.as_posix(), *args],
        env={**box["env"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


def revert_file(box: dict, status: str, url: str) -> str:
    path = box["dir"] / "revert_result.env"
    path.write_text(
        f"REVERT_STATUS={status}\nREVERT_BRANCH=revert/dev-aaaaaaa\nREVERT_PR_URL={url}\n",
        encoding="utf-8",
    )
    return path.as_posix()


def card(box: dict) -> dict:
    body = json.loads((box["dir"] / "body.json").read_text(encoding="utf-8"))
    return body["attachments"][0]["content"]


@needs_bash
def test_notification_has_run_and_revert_links(box: dict) -> None:
    pr = "https://dev.azure.com/org/p/_git/r/pullrequest/42"
    result = notify(box, "--revert-file", revert_file(box, "created", pr))
    assert result.returncode == 0, result.stderr
    urls = {a["title"]: a["url"] for a in card(box)["actions"]}
    assert urls["Open revert PR"] == pr
    assert urls["Open pipeline run"].endswith("buildId=77")
    assert " " not in urls["Open pipeline run"]


@needs_bash
def test_webhook_url_never_in_arguments_or_output(box: dict) -> None:
    result = notify(box)
    assert result.returncode == 0, result.stderr
    assert "SECRET-TOKEN" not in (box["dir"] / "curl_args").read_text(encoding="utf-8")
    assert "SECRET-TOKEN" not in result.stdout + result.stderr
    assert WEBHOOK in (box["dir"] / "curl_config").read_text(encoding="utf-8")


@needs_bash
def test_without_revert_file_says_no_revert_pr(box: dict) -> None:
    assert notify(box).returncode == 0
    assert "Open revert PR" not in {a["title"] for a in card(box)["actions"]}
    assert "No revert PR" in json.dumps(card(box))


@needs_bash
def test_conflict_status_has_no_revert_link(box: dict) -> None:
    result = notify(box, "--revert-file", revert_file(box, "conflict", ""))
    assert result.returncode == 0, result.stderr
    assert len(card(box)["actions"]) == 1
    assert "by hand" in json.dumps(card(box))


@needs_bash
def test_missing_webhook_warns_and_exits_zero(box: dict) -> None:
    result = notify(box, TEAMS_WEBHOOK_URL="")
    assert result.returncode == 0
    assert "not set" in result.stderr
    assert not (box["dir"] / "body.json").exists()


@needs_bash
def test_failed_post_exits_nonzero(box: dict) -> None:
    result = notify(box, FAKE_FAIL="1")
    assert result.returncode == 1
    assert "SECRET-TOKEN" not in result.stderr


@needs_bash
def test_non_https_webhook_rejected(box: dict) -> None:
    result = notify(box, TEAMS_WEBHOOK_URL="http://example.invalid/x")
    assert result.returncode == 1
    assert not (box["dir"] / "body.json").exists()


@needs_bash
def test_dry_run_prints_payload_without_sending(box: dict) -> None:
    result = notify(box, "--dry-run")
    assert result.returncode == 0
    assert json.loads(result.stdout)["type"] == "message"
    assert not (box["dir"] / "body.json").exists()


@needs_bash
def test_hostile_revert_file_values_are_data_only(box: dict) -> None:
    path = box["dir"] / "revert_result.env"
    path.write_text(
        'REVERT_STATUS=created"; touch pwned\nREVERT_PR_URL=javascript:alert(1)\n', encoding="utf-8"
    )
    result = notify(box, "--revert-file", path.as_posix())
    assert result.returncode == 0, result.stderr
    assert not (box["dir"] / "pwned").exists()
    assert len(card(box)["actions"]) == 1


# dbt-deploy.yml structure


def load_deploy() -> dict:
    return yaml.safe_load(DEPLOY_YML.read_text(encoding="utf-8"))


def deploy_steps() -> list[dict]:
    return load_deploy()["stages"][0]["jobs"][0]["steps"]


def label(step: dict) -> str:
    return step.get("displayName") or step.get("parameters", {}).get("displayName", "")


def index_of(fragment: str) -> int:
    for i, step in enumerate(deploy_steps()):
        if fragment in label(step):
            return i
    raise AssertionError(f"no step with '{fragment}'")


def script_of(step: dict) -> str:
    return step.get("script") or step.get("parameters", {}).get("script", "")


def test_validation_runs_before_backup_and_build() -> None:
    validate = index_of("Validate project")
    script = script_of(deploy_steps()[validate])
    for command in ("dbt deps", "dbt parse", "dbt compile"):
        assert command in script
    assert validate < index_of("back up") < index_of("Deploy changed models")


def test_manifest_upload_is_last_and_only_on_success() -> None:
    steps = deploy_steps()
    upload = index_of("Upload manifest")
    assert upload == len(steps) - 1
    assert upload > index_of("Deploy changed models") and upload > index_of("Mark build finished")
    params = steps[upload]["parameters"]
    assert params.get("condition", "succeeded()") == "succeeded()"
    assert "upload_state.sh" in params["script"]
    # nothing before the upload may run on failure, or the marker would not mean "deployed"
    for step in steps:
        assert "always()" not in str(step.get("condition", ""))
        assert "failed()" not in str(step.get("condition", ""))


def test_upload_only_in_the_upload_step() -> None:
    uploads = [s for s in deploy_steps() if "upload_state.sh" in script_of(s)]
    assert len(uploads) == 1


def test_revert_stage_runs_on_failure_and_has_no_merge() -> None:
    revert = load_deploy()["stages"][1]
    assert revert["stage"] == "Revert"
    assert revert["condition"] == "failed()"
    assert revert["dependsOn"] == "Deploy"
    text = yaml.safe_dump(revert)
    assert "create_revert_pr.sh" in text
    assert "complete" not in text.lower()
    assert "TEAMS_WEBHOOK_URL" in text


def test_every_step_has_a_display_name() -> None:
    for step in deploy_steps():
        # plain includes (install tools, download key) name their own steps
        if "template" in step and "parameters" not in step:
            continue
        assert label(step), step
    for step in load_deploy()["stages"][1]["jobs"][0]["steps"]:
        assert step.get("displayName"), step


def test_no_secret_literals_in_pipeline() -> None:
    text = DEPLOY_YML.read_text(encoding="utf-8")
    assert "hooks.office.com" not in text
    assert "webhook.office.com" not in text
