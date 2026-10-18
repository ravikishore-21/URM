#!/usr/bin/env bash
# Creates a revert branch for a commit whose deployment failed and opens a pull request for it.
# It never merges and never sets auto-complete: a person reviews and approves the PR.
#
# Usage: create_revert_pr.sh --branch <name> --sha <40-hex> [--output-file <path>]
#                            [--dry-run] [--allow-unrecognised-commit]
#
#   --branch   branch that was deployed and is the PR target (for example dev)
#   --sha      full SHA of the commit to revert (merge commit or squash commit)
#   --output-file  writes REVERT_STATUS / REVERT_BRANCH / REVERT_PR_URL lines (for the notify step)
#   --dry-run  does every local step (including the revert, to prove it applies) but pushes
#              nothing and calls no `az` command; it prints what it would run instead
#   --allow-unrecognised-commit  revert commits whose message is not "Merged PR <n>: ...".
#              Without it the script stops: a rebase merge adds several commits and reverting
#              only the last one would leave the others deployed.
#
# Environment:
#   GIT_BOT_NAME, GIT_BOT_EMAIL   identity for the revert commit (required)
#   GIT_AUTH_TOKEN                token for fetch/push, sent as a bearer header (not in argv)
#   ORG_URL, PROJECT, REPO_NAME   Azure DevOps organization URL, project and repository (not dry-run)
#   AZURE_DEVOPS_EXT_PAT          token read by `az devops` (not dry-run)
#   RUN_ID                        shown in every log line and in the PR description
#
# Exit codes: 0 ok, 1 failure, 2 bad usage, 3 revert does not apply cleanly, 4 unrecognised commit.
set -euo pipefail

readonly EXIT_USAGE=2
readonly EXIT_CONFLICT=3
readonly EXIT_UNRECOGNISED=4

BRANCH=""
SHA=""
OUTPUT_FILE=""
DRY_RUN=false
ALLOW_UNRECOGNISED=false
REVERT_BRANCH=""
ORIGINAL_HEAD=""

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
  exit "${EXIT_USAGE}"
}

log() {
  echo "[run ${RUN_ID:-local}] $*" >&2
}

die() {
  local code="$1"
  shift
  log "ERROR: $*"
  exit "${code}"
}

require_env() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || die 1 "environment variable ${name} is not set"
  done
}

write_result() {
  local status="$1" pr_url="${2:-}"
  [[ -n "${OUTPUT_FILE}" ]] || return 0
  printf 'REVERT_STATUS=%s\nREVERT_BRANCH=%s\nREVERT_PR_URL=%s\n' \
    "${status}" "${REVERT_BRANCH}" "${pr_url}" >"${OUTPUT_FILE}"
}

# Runs git against the remote. The token goes through GIT_CONFIG_* environment variables so it
# never appears in the process list or in .git/config.
git_remote() {
  if [[ -n "${GIT_AUTH_TOKEN:-}" ]]; then
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader \
      GIT_CONFIG_VALUE_0="AUTHORIZATION: bearer ${GIT_AUTH_TOKEN}" git "$@"
  else
    git "$@"
  fi
}

git_bot() {
  git -c "user.name=${GIT_BOT_NAME}" -c "user.email=${GIT_BOT_EMAIL}" "$@"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --branch) [[ $# -ge 2 ]] || usage; BRANCH="$2"; shift 2 ;;
      --sha) [[ $# -ge 2 ]] || usage; SHA="$2"; shift 2 ;;
      --output-file) [[ $# -ge 2 ]] || usage; OUTPUT_FILE="$2"; shift 2 ;;
      --dry-run) DRY_RUN=true; shift ;;
      --allow-unrecognised-commit) ALLOW_UNRECOGNISED=true; shift ;;
      -h | --help) usage ;;
      *) log "unknown argument: $1"; usage ;;
    esac
  done
  [[ -n "${BRANCH}" && -n "${SHA}" ]] || usage
}

# The branch and SHA end up in branch names and git arguments: allow simple values only.
validate_inputs() {
  [[ "${BRANCH}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "${BRANCH}" != *..* ]] \
    || die "${EXIT_USAGE}" "invalid branch '${BRANCH}'"
  [[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "${EXIT_USAGE}" "invalid SHA '${SHA}' (need 40 hex characters)"
  REVERT_BRANCH="revert/${BRANCH}-${SHA:0:7}"
  require_env GIT_BOT_NAME GIT_BOT_EMAIL
  if [[ "${DRY_RUN}" == false ]]; then
    require_env ORG_URL PROJECT REPO_NAME AZURE_DEVOPS_EXT_PAT
  fi
}

check_repository() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 1 "not inside a git work tree"
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] \
    || die 1 "working tree has uncommitted changes"
  git rev-parse --verify --quiet "${SHA}^{commit}" >/dev/null \
    || die 1 "commit ${SHA} not found (is the checkout shallow? use fetchDepth: 0)"
  ORIGINAL_HEAD="$(git rev-parse HEAD)"
}

# Prints "merge" or "single". Squash merges and rebase merges give single-parent commits.
commit_kind() {
  local words
  words="$(git rev-list --parents -n 1 "${SHA}" | wc -w)"
  case "${words}" in
    2) echo single ;;
    3) echo merge ;;
    *) die 1 "commit ${SHA} has an unsupported number of parents ($((words - 1)))" ;;
  esac
}

check_commit_is_recognised() {
  local subject
  subject="$(git log -1 --format=%s "${SHA}")"
  if [[ "${subject}" =~ ^Merged\ PR\ [0-9]+: ]]; then
    return 0
  fi
  if [[ "${ALLOW_UNRECOGNISED}" == true ]]; then
    log "WARNING: commit message is not 'Merged PR <n>: ...'; continuing (--allow-unrecognised-commit)"
    return 0
  fi
  write_result unrecognised-commit
  die "${EXIT_UNRECOGNISED}" "commit ${SHA:0:7} does not look like a pull request merge." \
    "A rebase merge or a direct push may contain several commits; revert it by hand."
}

remote_branch_exists() {
  local rc=0
  git_remote ls-remote --exit-code --heads origin "refs/heads/${REVERT_BRANCH}" >/dev/null 2>&1 || rc=$?
  case "${rc}" in
    0) return 0 ;;
    2) return 1 ;;
    *) die 1 "could not query the remote for ${REVERT_BRANCH} (git exit code ${rc})" ;;
  esac
}

restore_checkout() {
  git checkout --quiet --detach "${ORIGINAL_HEAD}"
  git branch --quiet -D "${REVERT_BRANCH}" 2>/dev/null || true
}

# Reverts onto the current branch tip (not onto the failed commit) so later commits are
# respected and conflicts show up here instead of in the PR.
create_revert_commit() {
  local kind="$1"
  local revert_args=(--no-edit)
  [[ "${kind}" == merge ]] && revert_args+=(-m 1)

  git checkout --quiet -B "${REVERT_BRANCH}" "origin/${BRANCH}"
  if ! git_bot revert "${revert_args[@]}" "${SHA}" >&2; then
    git revert --abort 2>/dev/null || true
    restore_checkout
    write_result conflict
    die "${EXIT_CONFLICT}" "reverting ${SHA:0:7} on origin/${BRANCH} did not apply cleanly (or is already reverted); revert it by hand"
  fi
  log "created revert commit $(git rev-parse --short HEAD) on ${REVERT_BRANCH} (${kind} commit)"
}

pr_url_for() {
  local encoded="${PROJECT// /%20}"
  echo "${ORG_URL%/}/${encoded}/_git/${REPO_NAME// /%20}/pullrequest/$1"
}

az_pr() {
  az repos pr "$@" --organization "${ORG_URL}" --project "${PROJECT}" --repository "${REPO_NAME}"
}

# Prints the id of an active PR for the revert branch, or nothing.
find_active_pr() {
  local id
  id="$(az_pr list --source-branch "${REVERT_BRANCH}" --target-branch "${BRANCH}" \
    --status active --query '[0].pullRequestId' --output tsv)" || die 1 "az repos pr list failed"
  case "${id}" in
    "" | None) ;;
    *[!0-9]*) die 1 "unexpected pull request id from az: '${id}'" ;;
    *) echo "${id}" ;;
  esac
}

open_pr() {
  local id title description
  id="$(find_active_pr)"
  if [[ -n "${id}" ]]; then
    log "an active revert PR already exists (#${id}); reusing it"
    write_result reused "$(pr_url_for "${id}")"
    return 0
  fi
  title="Revert ${SHA:0:7}: deployment to ${BRANCH} failed"
  description="Auto-created after failed run ${RUN_ID:-unknown}. Review before merging; this PR is never merged automatically. Backups for this run are logged in CICD.AUDIT.BACKUP_LOG (data is not restored by this PR)."
  id="$(az_pr create --source-branch "${REVERT_BRANCH}" --target-branch "${BRANCH}" \
    --title "${title}" --description "${description}" \
    --query pullRequestId --output tsv)" || die 1 "az repos pr create failed"
  [[ "${id}" =~ ^[0-9]+$ ]] || die 1 "unexpected pull request id from az: '${id}'"
  log "opened revert PR #${id}: $(pr_url_for "${id}")"
  write_result created "$(pr_url_for "${id}")"
}

main() {
  parse_args "$@"
  validate_inputs
  write_result failed
  check_repository
  check_commit_is_recognised

  git_remote fetch --quiet origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" \
    || die 1 "could not fetch origin/${BRANCH}"
  git merge-base --is-ancestor "${SHA}" "origin/${BRANCH}" \
    || die 1 "commit ${SHA:0:7} is not on origin/${BRANCH}"

  local kind
  kind="$(commit_kind)"

  if [[ "${DRY_RUN}" == false ]] && remote_branch_exists; then
    log "${REVERT_BRANCH} already exists on the remote; skipping the revert"
  else
    create_revert_commit "${kind}"
    if [[ "${DRY_RUN}" == true ]]; then
      git --no-pager log -1 --stat --format='%h %s' >&2
      restore_checkout
      log "DRY RUN: would run: git push origin ${REVERT_BRANCH}"
      log "DRY RUN: would run: az repos pr create --source-branch ${REVERT_BRANCH} --target-branch ${BRANCH}"
      write_result dry-run
      return 0
    fi
    git_remote push --quiet origin "${REVERT_BRANCH}" || die 1 "push of ${REVERT_BRANCH} failed"
    log "pushed ${REVERT_BRANCH}"
  fi

  open_pr
}

main "$@"
