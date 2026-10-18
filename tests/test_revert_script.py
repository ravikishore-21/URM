"""Tests for pipelines/scripts/create_revert_pr.sh using a real throwaway git repo and a fake `az`.

Nothing here touches Azure DevOps: `origin` is a bare repository in a temp directory.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "pipelines" / "scripts" / "create_revert_pr.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="bash or git is not available")

# Fake az: records its arguments. FAKE_ACTIVE_PR is the id returned by `pr list` (empty = none).
FAKE_AZ = """#!/usr/bin/env bash
echo "$@" >> "${FAKE_LOG}"
if [[ "$1 $2 $3" == "repos pr list" ]]; then
  echo "${FAKE_ACTIVE_PR:-}"
elif [[ "$1 $2 $3" == "repos pr create" ]]; then
  echo "42"
fi
exit 0
"""

SHA_RE = r"^[0-9a-f]{40}$"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [GIT, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    return result.stdout.strip()


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    """origin (bare) + a clone on `dev` with one base commit, pushed."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "dev", origin.as_posix())
    git(tmp_path, "clone", "-q", origin.as_posix(), work.as_posix())
    git(work, "checkout", "-q", "-b", "dev")
    commit_file(work, "base.txt", "base\n", "base")
    git(work, "push", "-q", "origin", "dev")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    az = bindir / "az"
    az.write_text(FAKE_AZ, encoding="utf-8", newline="\n")
    az.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": (tmp_path / "az.log").as_posix(),
        "GIT_BOT_NAME": "cicd-bot",
        "GIT_BOT_EMAIL": "bot@example.invalid",
        "ORG_URL": "https://dev.azure.com/org/",
        "PROJECT": "My Project",
        "REPO_NAME": "snowflake",
        "AZURE_DEVOPS_EXT_PAT": "fake-token",
        "RUN_ID": "77",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return {
        "work": work,
        "origin": origin,
        "env": env,
        "azlog": tmp_path / "az.log",
        "out": tmp_path / "result.env",
    }


def squash_commit(repo: dict) -> str:
    work = repo["work"]
    sha = commit_file(work, "model.sql", "select 1\n", "Merged PR 12: add model")
    git(work, "push", "-q", "origin", "dev")
    git(work, "checkout", "-q", "--detach", sha)  # like the pipeline checkout
    return sha


def merge_commit(repo: dict) -> str:
    work = repo["work"]
    git(work, "checkout", "-q", "-b", "feature")
    commit_file(work, "feature.sql", "select 2\n", "feature work")
    git(work, "checkout", "-q", "dev")
    git(work, "merge", "-q", "--no-ff", "-m", "Merged PR 13: feature", "feature")
    sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "dev")
    git(work, "checkout", "-q", "--detach", sha)
    return sha


def run(repo: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [BASH, SCRIPT.as_posix(), *args],
        cwd=repo["work"],
        env={**repo["env"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


def azlog(repo: dict) -> str:
    return repo["azlog"].read_text(encoding="utf-8") if repo["azlog"].exists() else ""


def remote_branches(repo: dict) -> list[str]:
    out = git(repo["origin"], "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return out.splitlines()


def test_squash_commit_is_reverted_and_pr_opened(repo: dict) -> None:
    sha = squash_commit(repo)
    result = run(repo, "--branch", "dev", "--sha", sha, "--output-file", repo["out"].as_posix())
    assert result.returncode == 0, result.stderr
    branch = f"revert/dev-{sha[:7]}"
    assert branch in remote_branches(repo)
    # the revert removed the file the squash commit added
    assert "model.sql" not in git(repo["origin"], "ls-tree", "--name-only", branch)
    log = azlog(repo)
    assert "repos pr create" in log
    assert f"--source-branch {branch}" in log
    assert "--target-branch dev" in log
    out = repo["out"].read_text(encoding="utf-8")
    assert "REVERT_STATUS=created" in out
    assert "pullrequest/42" in out
    assert "My%20Project" in out


def test_merge_commit_is_reverted_with_mainline_parent(repo: dict) -> None:
    sha = merge_commit(repo)
    result = run(repo, "--branch", "dev", "--sha", sha)
    assert result.returncode == 0, result.stderr
    tree = git(repo["origin"], "ls-tree", "--name-only", f"revert/dev-{sha[:7]}")
    assert "feature.sql" not in tree
    assert "base.txt" in tree


def test_pr_is_never_merged_or_auto_completed(repo: dict) -> None:
    sha = squash_commit(repo)
    assert run(repo, "--branch", "dev", "--sha", sha).returncode == 0
    log = azlog(repo)
    assert "auto-complete" not in log
    assert "repos pr update" not in log
    assert "complete" not in log.replace("auto-complete", "")


def test_dry_run_pushes_nothing_and_calls_no_az(repo: dict) -> None:
    sha = squash_commit(repo)
    result = run(
        repo, "--branch", "dev", "--sha", sha, "--dry-run", "--output-file", repo["out"].as_posix()
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stderr
    assert remote_branches(repo) == ["dev"]
    assert azlog(repo) == ""
    assert "REVERT_STATUS=dry-run" in repo["out"].read_text(encoding="utf-8")
    # the work tree is back where it started and the local branch is gone
    assert git(repo["work"], "rev-parse", "HEAD") == sha
    assert git(repo["work"], "branch", "--list", "revert/*") == ""


def test_dry_run_needs_no_azure_settings(repo: dict) -> None:
    sha = squash_commit(repo)
    result = run(
        repo, "--branch", "dev", "--sha", sha, "--dry-run",
        ORG_URL="", PROJECT="", REPO_NAME="", AZURE_DEVOPS_EXT_PAT="",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def test_rerun_reuses_existing_branch_and_pr(repo: dict) -> None:
    sha = squash_commit(repo)
    assert run(repo, "--branch", "dev", "--sha", sha).returncode == 0
    repo["azlog"].unlink()
    result = run(
        repo, "--branch", "dev", "--sha", sha, "--output-file", repo["out"].as_posix(),
        FAKE_ACTIVE_PR="42",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "repos pr create" not in azlog(repo)
    assert "REVERT_STATUS=reused" in repo["out"].read_text(encoding="utf-8")


def test_rerun_after_pr_was_abandoned_opens_a_new_pr_for_the_same_branch(repo: dict) -> None:
    sha = squash_commit(repo)
    assert run(repo, "--branch", "dev", "--sha", sha).returncode == 0
    repo["azlog"].unlink()
    assert run(repo, "--branch", "dev", "--sha", sha).returncode == 0
    assert "repos pr create" in azlog(repo)


def test_conflict_exits_3_and_leaves_repo_clean(repo: dict) -> None:
    work = repo["work"]
    sha = commit_file(work, "model.sql", "select 1\n", "Merged PR 12: add model")
    commit_file(work, "model.sql", "select 999\n", "Merged PR 14: change model")
    git(work, "push", "-q", "origin", "dev")
    git(work, "checkout", "-q", "--detach", sha)
    result = run(repo, "--branch", "dev", "--sha", sha, "--output-file", repo["out"].as_posix())
    assert result.returncode == 3, result.stderr
    assert "by hand" in result.stderr
    assert remote_branches(repo) == ["dev"]
    assert azlog(repo) == ""
    assert git(work, "status", "--porcelain", "--untracked-files=no") == ""
    assert "REVERT_STATUS=conflict" in repo["out"].read_text(encoding="utf-8")


def test_rebase_style_commit_is_refused(repo: dict) -> None:
    work = repo["work"]
    sha = commit_file(work, "model.sql", "select 1\n", "add model")  # no "Merged PR" prefix
    git(work, "push", "-q", "origin", "dev")
    git(work, "checkout", "-q", "--detach", sha)
    result = run(repo, "--branch", "dev", "--sha", sha, "--output-file", repo["out"].as_posix())
    assert result.returncode == 4
    assert remote_branches(repo) == ["dev"]
    assert "REVERT_STATUS=unrecognised-commit" in repo["out"].read_text(encoding="utf-8")


def test_unrecognised_commit_can_be_allowed(repo: dict) -> None:
    work = repo["work"]
    sha = commit_file(work, "model.sql", "select 1\n", "add model")
    git(work, "push", "-q", "origin", "dev")
    git(work, "checkout", "-q", "--detach", sha)
    result = run(repo, "--branch", "dev", "--sha", sha, "--allow-unrecognised-commit")
    assert result.returncode == 0, result.stderr


def test_commit_not_on_branch_is_refused(repo: dict) -> None:
    work = repo["work"]
    git(work, "checkout", "-q", "-b", "other")
    sha = commit_file(work, "x.txt", "x\n", "Merged PR 15: elsewhere")
    git(work, "checkout", "-q", "--detach", sha)
    result = run(repo, "--branch", "dev", "--sha", sha)
    assert result.returncode == 1
    assert "not on origin/dev" in result.stderr


@pytest.mark.parametrize("bad", ["", "abc", "Z" * 40, "a" * 39, "a" * 40 + ";rm"])
def test_bad_sha_rejected(repo: dict, bad: str) -> None:
    result = run(repo, "--branch", "dev", "--sha", bad)
    assert result.returncode in (2,)
    assert azlog(repo) == ""


@pytest.mark.parametrize("bad", ["../x", "-x", "a b", "dev;x", "a..b"])
def test_bad_branch_rejected(repo: dict, bad: str) -> None:
    result = run(repo, "--branch", bad, "--sha", "a" * 40)
    assert result.returncode == 2


def test_usage_without_arguments(repo: dict) -> None:
    result = run(repo)
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_requires_bot_identity(repo: dict) -> None:
    sha = squash_commit(repo)
    result = run(repo, "--branch", "dev", "--sha", sha, GIT_BOT_EMAIL="")
    assert result.returncode == 1
    assert "GIT_BOT_EMAIL" in result.stderr


def test_dirty_work_tree_is_refused(repo: dict) -> None:
    sha = squash_commit(repo)
    (repo["work"] / "base.txt").write_text("changed\n", encoding="utf-8")
    result = run(repo, "--branch", "dev", "--sha", sha)
    assert result.returncode == 1
    assert "uncommitted" in result.stderr
