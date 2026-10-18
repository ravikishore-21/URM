"""Tests for dbt/scripts/*.sh using a fake `snow` executable. Nothing touches Snowflake."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "dbt" / "scripts"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

# Fake snow: records its arguments, behaviour chosen with FAKE_MODE.
FAKE_SNOW = """#!/usr/bin/env bash
echo "$@" >> "${FAKE_LOG}"
case "$1 $2" in
  "stage list-files")
    [[ "${FAKE_MODE}" == "list_fail" ]] && { echo "auth error" >&2; exit 1; }
    if [[ "${FAKE_MODE}" == "found" || "${FAKE_MODE}" == "copy_fail" ]]; then
      echo '[{"name": "dbt_state/dev/manifest.json"}]'
    else
      echo '[]'
    fi
    ;;
  "stage copy")
    [[ "${FAKE_MODE}" == "copy_fail" ]] && exit 1
    if [[ "$3" == @* ]]; then echo '{}' > "${4}manifest.json"; fi
    ;;
  "sql --query")
    [[ "${FAKE_MODE}" == "sql_fail" ]] && exit 1
    ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    fake = tmp_path / "snow"
    fake.write_text(FAKE_SNOW, encoding="utf-8", newline="\n")
    key = tmp_path / "key.txt"
    key.write_text("not-a-real-key", encoding="utf-8")
    env = {
        **os.environ,
        "SNOW_BIN": fake.as_posix(),
        "FAKE_LOG": (tmp_path / "calls.log").as_posix(),
        "FAKE_MODE": "found",
        "SNOWFLAKE_ACCOUNT": "acct",
        "SNOWFLAKE_USER": "user",
        "SNOWFLAKE_KEY_PATH": key.as_posix(),
        "SNOWFLAKE_ROLE": "role",
        "SNOWFLAKE_WAREHOUSE": "wh",
        "SNOWFLAKE_DATABASE": "ANALYTICS_DEV",
        "RUN_ID": "1",
    }
    return {"env": env, "cwd": tmp_path, "log": tmp_path / "calls.log"}


def run(
    sandbox: dict, script: str, *args: str, **env_overrides: str
) -> subprocess.CompletedProcess:
    env = {**sandbox["env"], **env_overrides}
    return subprocess.run(  # noqa: S603
        [BASH, (SCRIPTS / script).as_posix(), *args],
        cwd=sandbox["cwd"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def calls(sandbox: dict) -> str:
    return sandbox["log"].read_text(encoding="utf-8") if sandbox["log"].exists() else ""


# fetch_state.sh


def test_fetch_downloads_manifest(sandbox: dict) -> None:
    result = run(sandbox, "fetch_state.sh", "dev")
    assert result.returncode == 0, result.stderr
    assert (sandbox["cwd"] / "state" / "manifest.json").is_file()
    assert "@CICD.STATE.DBT_STATE/dev/manifest.json" in calls(sandbox)


def test_fetch_returns_10_when_no_manifest(sandbox: dict) -> None:
    result = run(sandbox, "fetch_state.sh", "dev", FAKE_MODE="empty")
    assert result.returncode == 10
    assert not (sandbox["cwd"] / "state" / "manifest.json").exists()


def test_fetch_listing_error_is_not_first_run(sandbox: dict) -> None:
    result = run(sandbox, "fetch_state.sh", "dev", FAKE_MODE="list_fail")
    assert result.returncode == 1


def test_fetch_download_error_fails(sandbox: dict) -> None:
    result = run(sandbox, "fetch_state.sh", "dev", FAKE_MODE="copy_fail")
    assert result.returncode == 1


@pytest.mark.parametrize("bad", ["DEV", "dev/../x", "dev;drop", "dev test"])
def test_fetch_rejects_bad_env_name(sandbox: dict, bad: str) -> None:
    result = run(sandbox, "fetch_state.sh", bad)
    assert result.returncode == 1
    assert calls(sandbox) == ""


def test_fetch_requires_argument(sandbox: dict) -> None:
    assert run(sandbox, "fetch_state.sh").returncode == 2


def test_fetch_requires_credentials(sandbox: dict) -> None:
    result = run(sandbox, "fetch_state.sh", "dev", SNOWFLAKE_ACCOUNT="")
    assert result.returncode == 1
    assert "SNOWFLAKE_ACCOUNT" in result.stderr


# upload_state.sh


def test_upload_copies_manifest(sandbox: dict) -> None:
    target = sandbox["cwd"] / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    result = run(sandbox, "upload_state.sh", "dev")
    assert result.returncode == 0, result.stderr
    recorded = calls(sandbox)
    assert "@CICD.STATE.DBT_STATE/dev/" in recorded
    assert "--overwrite" in recorded


def test_upload_fails_without_manifest(sandbox: dict) -> None:
    result = run(sandbox, "upload_state.sh", "dev")
    assert result.returncode == 1
    assert calls(sandbox) == ""


def test_upload_rejects_invalid_json(sandbox: dict) -> None:
    target = sandbox["cwd"] / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{not json", encoding="utf-8")
    result = run(sandbox, "upload_state.sh", "dev")
    assert result.returncode == 1
    assert calls(sandbox) == ""


def test_upload_failure_propagates(sandbox: dict) -> None:
    target = sandbox["cwd"] / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    assert run(sandbox, "upload_state.sh", "dev", FAKE_MODE="copy_fail").returncode == 1


# drop_pr_schema.sh


def test_drop_schema_sql(sandbox: dict) -> None:
    result = run(sandbox, "drop_pr_schema.sh", "42")
    assert result.returncode == 0, result.stderr
    assert 'DROP SCHEMA IF EXISTS "ANALYTICS_DEV"."CI_PR_42"' in calls(sandbox)


@pytest.mark.parametrize("bad", ["0", "abc", "42; drop database x", "-1", ""])
def test_drop_schema_rejects_bad_pr_number(sandbox: dict, bad: str) -> None:
    result = run(sandbox, "drop_pr_schema.sh", bad)
    assert result.returncode == 1
    assert calls(sandbox) == ""


def test_drop_schema_rejects_bad_database(sandbox: dict) -> None:
    result = run(sandbox, "drop_pr_schema.sh", "42", SNOWFLAKE_DATABASE='X"; DROP DATABASE Y; --')
    assert result.returncode == 1
    assert calls(sandbox) == ""


def test_drop_schema_failure_propagates(sandbox: dict) -> None:
    assert run(sandbox, "drop_pr_schema.sh", "42", FAKE_MODE="sql_fail").returncode == 1
