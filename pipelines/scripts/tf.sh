#!/usr/bin/env bash
# Runs Terraform for one environment folder (terraform/envs/<env>).
#
# Usage: tf.sh checks
#        tf.sh init  <env>
#        tf.sh plan  <env>
#        tf.sh apply <env>
#
#   checks  no credentials needed: fmt -check, init -backend=false + validate for every
#           environment, tflint for every module and environment, checkov
#   init    init with the azurerm backend (partial configuration, read-only lock file)
#   plan    init, then plan -out=tfplan; writes the plan as text to ${OUT_DIR}
#   apply   init, then apply the saved tfplan in terraform/envs/<env>. Never plans on its own
#           and never passes -auto-approve: a saved plan is already approved by the pipeline.
#
# Environment (all passed through env:, none is printed):
#   RUN_ID                       shown in every log line
#   TFSTATE_RESOURCE_GROUP, TFSTATE_STORAGE_ACCOUNT, TFSTATE_CONTAINER   state backend
#   ARM_*                        Azure AD sign-in for the backend (set by the AzureCLI@2 step)
#   SNOWFLAKE_ORGANIZATION_NAME, SNOWFLAKE_ACCOUNT_NAME, SNOWFLAKE_TF_USER
#   SNOWFLAKE_KEY_PATH           path of the Terraform user's private key (Secure File)
#   TF_OBJECTS_ROLE, TF_SECURITY_ROLE   optional provider role overrides (default SYSADMIN / SECURITYADMIN)
#   TF_DEPLOYER_USERS            JSON list of users that get the deployer role, e.g. ["SVC_DBT_DEV"]
#   OUT_DIR                      where plan text is written (default ./plan-output)
#   PR_NUMBER                    shown in the plan summary header when set
set -euo pipefail

ENV_DIRS=()
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${OUT_DIR:-${PWD}/plan-output}"

log() { echo "[run ${RUN_ID:-local}] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 2
}

env_dir() {
  local env="$1"
  [[ "${env}" =~ ^[a-z]+$ ]] || die "environment '${env}' must be lower-case letters"
  [[ -d "${ROOT}/terraform/envs/${env}" ]] || die "no folder terraform/envs/${env}"
  echo "${ROOT}/terraform/envs/${env}"
}

# Fills the ENV_DIRS array with every terraform/envs/<env> folder.
load_env_dirs() {
  local d
  ENV_DIRS=()
  for d in "${ROOT}"/terraform/envs/*/; do
    ENV_DIRS+=("${d%/}")
  done
  [[ ${#ENV_DIRS[@]} -gt 0 ]] || die "no environment folders found"
}

run_checks() {
  local d
  load_env_dirs
  log "terraform fmt -check"
  terraform -chdir="${ROOT}/terraform" fmt -check -recursive -diff
  for d in "${ENV_DIRS[@]}"; do
    log "validate ${d#"${ROOT}"/}"
    terraform -chdir="${d}" init -backend=false -input=false -lockfile=readonly >/dev/null
    terraform -chdir="${d}" validate
  done
  for d in "${ROOT}"/terraform/modules/*/ "${ENV_DIRS[@]}"; do
    log "tflint ${d#"${ROOT}"/}"
    tflint --chdir="${d}" --config="${ROOT}/.tflint.hcl"
  done
  log "checkov"
  checkov --directory "${ROOT}/terraform" --framework terraform --quiet --compact
  log "All Terraform checks passed"
}

do_init() {
  local dir="$1"
  : "${TFSTATE_RESOURCE_GROUP:?TFSTATE_RESOURCE_GROUP is not set}"
  : "${TFSTATE_STORAGE_ACCOUNT:?TFSTATE_STORAGE_ACCOUNT is not set}"
  : "${TFSTATE_CONTAINER:?TFSTATE_CONTAINER is not set}"
  log "terraform init ${dir#"${ROOT}"/}"
  terraform -chdir="${dir}" init -input=false -reconfigure -lockfile=readonly \
    -backend-config="resource_group_name=${TFSTATE_RESOURCE_GROUP}" \
    -backend-config="storage_account_name=${TFSTATE_STORAGE_ACCOUNT}" \
    -backend-config="container_name=${TFSTATE_CONTAINER}"
}

# Provider inputs reach Terraform as TF_VAR_* so nothing is written to disk or a command line.
export_provider_vars() {
  : "${SNOWFLAKE_ORGANIZATION_NAME:?SNOWFLAKE_ORGANIZATION_NAME is not set}"
  : "${SNOWFLAKE_ACCOUNT_NAME:?SNOWFLAKE_ACCOUNT_NAME is not set}"
  : "${SNOWFLAKE_TF_USER:?SNOWFLAKE_TF_USER is not set}"
  : "${SNOWFLAKE_KEY_PATH:?SNOWFLAKE_KEY_PATH is not set}"
  : "${TF_DEPLOYER_USERS:?TF_DEPLOYER_USERS is not set (use [] for none)}"
  [[ -r "${SNOWFLAKE_KEY_PATH}" ]] || die "private key file is not readable"
  # The key goes to the provider through its own environment variable, never through a Terraform
  # variable: variable values are written into the saved plan, which is published as an artifact.
  SNOWFLAKE_PRIVATE_KEY="$(<"${SNOWFLAKE_KEY_PATH}")"
  TF_VAR_organization_name="${SNOWFLAKE_ORGANIZATION_NAME}"
  TF_VAR_account_name="${SNOWFLAKE_ACCOUNT_NAME}"
  TF_VAR_tf_user="${SNOWFLAKE_TF_USER}"
  TF_VAR_deployer_user_names="${TF_DEPLOYER_USERS}"
  # Optional role overrides (PR plans use read-only roles); ACCOUNTADMIN is refused by variables.tf.
  if [[ -n "${TF_OBJECTS_ROLE:-}" ]]; then
    TF_VAR_objects_role="${TF_OBJECTS_ROLE}"
    export TF_VAR_objects_role
  fi
  if [[ -n "${TF_SECURITY_ROLE:-}" ]]; then
    TF_VAR_security_role="${TF_SECURITY_ROLE}"
    export TF_VAR_security_role
  fi
  export SNOWFLAKE_PRIVATE_KEY TF_VAR_organization_name TF_VAR_account_name TF_VAR_tf_user TF_VAR_deployer_user_names
}

run_plan() {
  local env="$1" dir rc=0
  dir="$(env_dir "${env}")"
  export_provider_vars
  do_init "${dir}"
  log "terraform plan ${env}"
  terraform -chdir="${dir}" plan -input=false -lock-timeout=5m -detailed-exitcode -out=tfplan || rc=$?
  case "${rc}" in
    0) log "No changes for ${env}" ;;
    2) log "Changes found for ${env}" ;;
    *) die "terraform plan failed with exit code ${rc}" ;;
  esac
  # Text only: the binary plan file is not published by the PR pipeline.
  mkdir -p "${OUT_DIR}"
  terraform -chdir="${dir}" show -no-color tfplan >"${OUT_DIR}/plan-${env}.txt"
  {
    echo "## Terraform plan: ${env}${PR_NUMBER:+ (PR ${PR_NUMBER})}"
    echo
    echo '```'
    cat "${OUT_DIR}/plan-${env}.txt"
    echo '```'
  } >"${OUT_DIR}/plan-${env}.md"
  log "Plan written to ${OUT_DIR}/plan-${env}.txt"
}

run_apply() {
  local env="$1" dir
  dir="$(env_dir "${env}")"
  [[ -f "${dir}/tfplan" ]] || die "no saved plan at terraform/envs/${env}/tfplan; run the Plan stage first"
  export_provider_vars
  do_init "${dir}"
  log "terraform apply (saved plan) ${env}"
  terraform -chdir="${dir}" apply -input=false -lock-timeout=5m tfplan
  log "Apply finished for ${env}"
}

main() {
  [[ $# -ge 1 ]] || usage
  case "$1" in
    checks) run_checks ;;
    init)
      [[ $# -eq 2 ]] || usage
      do_init "$(env_dir "$2")"
      ;;
    plan)
      [[ $# -eq 2 ]] || usage
      run_plan "$2"
      ;;
    apply)
      [[ $# -eq 2 ]] || usage
      run_apply "$2"
      ;;
    *) usage ;;
  esac
}

main "$@"
