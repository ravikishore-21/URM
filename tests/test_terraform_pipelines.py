"""Structure checks for the Terraform project, its pipelines and pipelines/scripts/tf.sh.

No Snowflake or Azure access is needed: these tests read files and run tf.sh only up to the
point where it refuses bad input.
"""

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TF = ROOT / "terraform"
PIPELINES = ROOT / "pipelines"
TF_SH = PIPELINES / "scripts" / "tf.sh"
INSTALL_SH = PIPELINES / "scripts" / "install_tf_tools.sh"
ENVS = sorted(p.name for p in (TF / "envs").iterdir() if p.is_dir())
TF_PIPELINES = [PIPELINES / "tf-plan.yml", PIPELINES / "tf-apply.yml"]
TF_TEMPLATES = [
    PIPELINES / "templates" / "terraform-setup.yml",
    PIPELINES / "templates" / "terraform-run.yml",
]
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code(path: Path) -> str:
    """File text without comment lines and without human-readable text attributes."""
    skip = re.compile(r"^\s*(#|comment\s*=|description\s*=|error_message\s*=)")
    return "\n".join(ln for ln in read(path).splitlines() if not skip.match(ln))


def load(path: Path) -> Any:
    return yaml.safe_load(read(path))


def steps(node: Any) -> Iterator[dict]:
    """Yield every step-like mapping (script, task, checkout, download, template) in a YAML tree."""
    if isinstance(node, dict):
        if any(k in node for k in ("script", "task", "checkout", "download", "template")):
            yield node
        for value in node.values():
            yield from steps(value)
    elif isinstance(node, list):
        for item in node:
            yield from steps(item)


def test_environments_present() -> None:
    assert ENVS == ["dev", "test"]


@pytest.mark.parametrize("env", ENVS)
def test_state_key_is_per_environment(env: str) -> None:
    text = read(TF / "envs" / env / "versions.tf")
    assert f'key              = "snowflake/{env}/terraform.tfstate"' in text


def test_state_keys_differ() -> None:
    keys = {
        re.search(r'key\s+= "([^"]+)"', read(TF / "envs" / e / "versions.tf")).group(1)
        for e in ENVS
    }  # type: ignore[union-attr]
    assert len(keys) == len(ENVS)


@pytest.mark.parametrize("env", ENVS)
def test_versions_are_pinned_exactly(env: str) -> None:
    text = read(TF / "envs" / env / "versions.tf")
    assert re.search(r'required_version\s*=\s*"\d+\.\d+\.\d+"', text)
    assert re.search(r'version\s*=\s*"\d+\.\d+\.\d+"', text)


def test_terraform_version_matches_installer() -> None:
    installed = re.search(r'TERRAFORM_VERSION="([^"]+)"', read(INSTALL_SH)).group(1)  # type: ignore[union-attr]
    for env in ENVS:
        pinned = re.search(
            r'required_version\s*=\s*"([^"]+)"', read(TF / "envs" / env / "versions.tf")
        ).group(1)  # type: ignore[union-attr]
        assert pinned == installed


def test_provider_version_is_the_same_in_every_environment() -> None:
    versions = {
        re.search(
            r'snowflake = \{[^}]*version\s*=\s*"([^"]+)"',
            read(TF / "envs" / e / "versions.tf"),
            re.S,
        ).group(1)  # type: ignore[union-attr]
        for e in ENVS
    }
    assert len(versions) == 1


def test_checkov_is_pinned() -> None:
    lines = [
        ln
        for ln in read(ROOT / "requirements-tf.txt").splitlines()
        if ln and not ln.startswith("#")
    ]
    assert lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==\d+(\.\d+)*", ln) for ln in lines)


@pytest.mark.parametrize("env", ENVS)
def test_private_key_is_not_a_terraform_variable(env: str) -> None:
    """Variable values are stored in a saved plan, which the apply pipeline publishes."""
    for name in ("variables.tf", "providers.tf"):
        assert "private_key" not in code(TF / "envs" / env / name)


def test_tf_script_passes_key_by_provider_env_var_only() -> None:
    text = code(TF_SH)
    assert "SNOWFLAKE_PRIVATE_KEY" in text
    assert "TF_VAR_private_key" not in text


@pytest.mark.parametrize("env", ENVS)
def test_provider_uses_jwt_and_never_accountadmin(env: str) -> None:
    providers = read(TF / "envs" / env / "providers.tf")
    assert providers.count("authenticator") == 2
    assert providers.count('"SNOWFLAKE_JWT"') == 2
    assert "ACCOUNTADMIN" not in providers
    variables = read(TF / "envs" / env / "variables.tf")
    assert 'default     = "SYSADMIN"' in variables
    assert 'default     = "SECURITYADMIN"' in variables


def test_every_terraform_variable_has_type_and_description() -> None:
    for path in TF.rglob("variables.tf"):
        text = read(path)
        for match in re.finditer(r'variable "(\w+)" \{(.*?)\n\}', text, re.S):
            body = match.group(2)
            assert re.search(r"^\s*type\s*=", body, re.M), (
                f"{path.name}: {match.group(1)} has no type"
            )
            assert re.search(r"^\s*description\s*=", body, re.M), (
                f"{path.name}: {match.group(1)} has no description"
            )


def test_modules_have_no_hard_coded_environment_names() -> None:
    for path in (TF / "modules").rglob("*.tf"):
        text = code(path)
        for name in ("dev", "test", "uat", "stage", "prod"):
            assert not re.search(rf'"[^"]*\b{name}\b[^"]*"', text), f"{path}: '{name}' hard-coded"


def test_lock_files_are_committed_with_linux_hashes() -> None:
    for env in ENVS:
        lock = TF / "envs" / env / ".terraform.lock.hcl"
        assert lock.exists(), f"{env}: .terraform.lock.hcl missing"
        text = read(lock)
        assert 'version     = "' in text
        assert "zh:" in text, (
            f"{env}: lock file has no linux (zh:) hashes; run terraform providers lock"
        )


def test_no_secrets_in_terraform_files() -> None:
    for path in TF.rglob("*"):
        if path.is_file() and ".terraform" not in path.parts and path.name != ".terraform.lock.hcl":
            assert "-----BEGIN" not in read(path), path


def test_destroy_protection_on_data_holding_resources() -> None:
    env_main = read(TF / "modules" / "environment" / "main.tf")
    cicd_main = read(TF / "modules" / "cicd" / "main.tf")
    assert env_main.count("prevent_destroy = true") == 2  # database, BACKUP schema
    assert cicd_main.count("prevent_destroy = true") == 3  # database, stage, backup log


def test_only_dev_manages_cicd_objects() -> None:
    assert "manage_objects      = true" in read(TF / "envs" / "dev" / "main.tf")
    assert "manage_objects      = false" in read(TF / "envs" / "test" / "main.tf")


@pytest.mark.parametrize("path", [*TF_PIPELINES, *TF_TEMPLATES], ids=lambda p: p.name)
def test_every_step_has_a_display_name(path: Path) -> None:
    for step in steps(load(path)):
        if "template" in step:
            continue
        assert "displayName" in step, f"{path.name}: step without displayName: {step}"


@pytest.mark.parametrize(
    "path", [*TF_PIPELINES, *TF_TEMPLATES, TF_SH, INSTALL_SH], ids=lambda p: p.name
)
def test_no_auto_approve_and_no_inline_secrets(path: Path) -> None:
    text = code(path)
    assert "-auto-approve" not in text
    assert "-var " not in text and "-var=" not in text, (
        "variables must reach Terraform as TF_VAR_* only"
    )
    assert "-----BEGIN" not in text


def test_apply_uses_saved_plan_only() -> None:
    text = code(TF_SH)
    assert "apply -input=false -lock-timeout=5m tfplan" in text
    assert len(re.findall(r"^\s*terraform\b.*\bapply\b", text, re.M)) == 1


def test_plan_pipeline_publishes_text_only() -> None:
    publish = [
        s
        for s in steps(load(PIPELINES / "tf-plan.yml"))
        if s.get("task", "").startswith("PublishPipelineArtifact")
    ]
    assert publish
    for step in publish:
        assert "plan-output" in step["inputs"]["targetPath"]
        assert "tfplan" not in step["inputs"]["targetPath"].replace("terraform-plan", "")


def test_plan_pipeline_checks_before_plan() -> None:
    stages = load(PIPELINES / "tf-plan.yml")["stages"]
    assert [s["stage"] for s in stages] == ["Validate", "Plan"]
    assert stages[1]["dependsOn"] == "Validate"


def test_apply_pipeline_gates_through_environment() -> None:
    stages = load(PIPELINES / "tf-apply.yml")["stages"]
    assert [s["stage"] for s in stages] == ["Plan", "Apply"]
    job = stages[1]["jobs"][0]
    assert "deployment" in job
    assert str(job["environment"]).startswith("snowflake-")


def test_tasks_are_pinned_to_a_major_version() -> None:
    for path in [*TF_PIPELINES, *TF_TEMPLATES]:
        for step in steps(load(path)):
            if "task" in step:
                assert re.fullmatch(r"\w+@\d+", step["task"]), f"{path.name}: {step['task']}"


def test_installer_checks_checksums() -> None:
    text = read(INSTALL_SH)
    assert re.search(r'TERRAFORM_SHA256="[0-9a-f]{64}"', text)
    assert re.search(r'TFLINT_SHA256="[0-9a-f]{64}"', text)
    assert "set -euo pipefail" in text


@pytest.mark.parametrize("path", [TF_SH, INSTALL_SH], ids=lambda p: p.name)
def test_scripts_fail_loudly(path: Path) -> None:
    assert "set -euo pipefail" in read(path).splitlines()[:40]


def run_tf_sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [BASH, TF_SH.as_posix(), *args], capture_output=True, text=True, check=False
    )


@needs_bash
def test_tf_sh_without_arguments_prints_usage() -> None:
    result = run_tf_sh()
    assert result.returncode == 2
    assert "Usage: tf.sh" in result.stdout


@needs_bash
@pytest.mark.parametrize("bad", ["Dev", "dev;ls", "../x", "prod"])
def test_tf_sh_rejects_bad_environment(bad: str) -> None:
    result = run_tf_sh("plan", bad)
    assert result.returncode == 1
    assert "ERROR" in result.stderr


@needs_bash
def test_tf_sh_apply_refuses_without_saved_plan() -> None:
    plan = TF / "envs" / "dev" / "tfplan"
    assert not plan.exists()
    result = run_tf_sh("apply", "dev")
    assert result.returncode == 1
    assert "no saved plan" in result.stderr


def test_branch_to_environment_is_pinned_to_full_branch_names() -> None:
    """A branch such as feature/dev must never resolve to the dev group (variables.yml)."""
    template = load(PIPELINES / "templates" / "variables.yml")
    branch = next(p for p in template["parameters"] if p["name"] == "branch")
    assert branch["values"] == ["dev", "test"]
    for name in ("dbt-deploy.yml", "tf-apply.yml"):
        text = code(PIPELINES / name)
        assert "SourceBranchName" not in text
        assert "variables['Build.SourceBranch']" in text


@pytest.mark.parametrize("name", ["dbt-deploy.yml", "tf-apply.yml"])
def test_deploy_pipelines_run_one_at_a_time(name: str) -> None:
    assert load(PIPELINES / name)["trigger"]["batch"] is True


def test_dbt_deploy_refuses_full_build_unless_allowed() -> None:
    pipeline = load(PIPELINES / "dbt-deploy.yml")
    param = next(p for p in pipeline["parameters"] if p["name"] == "allowFullBuild")
    assert param["default"] is False
    assert "Refusing a full build" in read(PIPELINES / "dbt-deploy.yml")


def test_pr_pipelines_do_not_use_the_deploy_identity() -> None:
    plan = code(PIPELINES / "tf-plan.yml")
    assert "snowflake_tf_plan_key.p8" in plan and "snowflake_tf_key.p8" not in plan
    ci = code(PIPELINES / "dbt-ci.yml")
    assert "SNOWFLAKE_PR_ROLE" in ci and "$(SNOWFLAKE_ROLE)" not in ci
    assert "$(SNOWFLAKE_USER)" not in ci
