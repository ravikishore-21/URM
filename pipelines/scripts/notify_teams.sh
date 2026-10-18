#!/usr/bin/env bash
# Posts a failed-deployment message to a Microsoft Teams (Workflows) webhook as an Adaptive Card.
#
# Usage: notify_teams.sh --run-url <url> --env <name> --branch <name> --sha <sha>
#                        [--revert-file <path>] [--dry-run]
#
#   --revert-file  the file written by create_revert_pr.sh --output-file; if it is missing,
#                  the message says no revert PR was created
#   --dry-run      prints the JSON payload instead of sending it
#
# Environment:
#   TEAMS_WEBHOOK_URL  webhook URL (secret; pass it through env:, it is never printed)
#   RUN_ID             shown in every log line
#
# If TEAMS_WEBHOOK_URL is not set the script warns and exits 0, so a missing webhook cannot hide
# the real failure. A failed POST exits non-zero.
set -euo pipefail

RUN_URL=""
ENV_NAME=""
BRANCH=""
SHA=""
REVERT_FILE=""
DRY_RUN=false
REVERT_STATUS=""
REVERT_BRANCH=""
REVERT_PR_URL=""
PYTHON_BIN=""
PAYLOAD_FILE=""

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
  exit 2
}

log() {
  echo "[run ${RUN_ID:-local}] $*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --run-url) [[ $# -ge 2 ]] || usage; RUN_URL="$2"; shift 2 ;;
      --env) [[ $# -ge 2 ]] || usage; ENV_NAME="$2"; shift 2 ;;
      --branch) [[ $# -ge 2 ]] || usage; BRANCH="$2"; shift 2 ;;
      --sha) [[ $# -ge 2 ]] || usage; SHA="$2"; shift 2 ;;
      --revert-file) [[ $# -ge 2 ]] || usage; REVERT_FILE="$2"; shift 2 ;;
      --dry-run) DRY_RUN=true; shift ;;
      -h | --help) usage ;;
      *) log "unknown argument: $1"; usage ;;
    esac
  done
  [[ -n "${RUN_URL}" && -n "${ENV_NAME}" && -n "${BRANCH}" && -n "${SHA}" ]] || usage
  [[ "${RUN_URL}" =~ ^https?:// ]] || die "--run-url must start with http:// or https://"
  RUN_URL="${RUN_URL// /%20}"
}

# Reads the result file line by line; it is parsed, never sourced.
read_revert_file() {
  [[ -n "${REVERT_FILE}" && -r "${REVERT_FILE}" ]] || return 0
  local key value
  while IFS='=' read -r key value; do
    value="${value%$'\r'}"
    case "${key}" in
      REVERT_STATUS) REVERT_STATUS="${value}" ;;
      REVERT_BRANCH) REVERT_BRANCH="${value}" ;;
      REVERT_PR_URL) [[ "${value}" =~ ^https?:// ]] && REVERT_PR_URL="${value}" ;;
      *) ;;
    esac
  done <"${REVERT_FILE}"
  return 0
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 && "${candidate}" -c 'import sys' >/dev/null 2>&1; then
      PYTHON_BIN="${candidate}"
      return 0
    fi
  done
  die "python is required to build the message"
}

# Values travel in environment variables so the JSON is built without string concatenation.
# The Python program is single-quoted on purpose: the shell must not expand anything in it.
build_payload() {
  # shellcheck disable=SC2016
  NOTIFY_RUN_URL="${RUN_URL}" NOTIFY_ENV="${ENV_NAME}" NOTIFY_BRANCH="${BRANCH}" \
    NOTIFY_SHA="${SHA:0:7}" NOTIFY_RUN_ID="${RUN_ID:-unknown}" \
    NOTIFY_STATUS="${REVERT_STATUS}" NOTIFY_REVERT_BRANCH="${REVERT_BRANCH}" \
    NOTIFY_REVERT_URL="${REVERT_PR_URL}" "${PYTHON_BIN}" -c '
import json
import os

e = os.environ
status = e["NOTIFY_STATUS"]
notes = {
    "created": "A revert PR was opened. It is not merged; please review it.",
    "reused": "A revert PR already existed and was reused. It is not merged; please review it.",
    "conflict": "The revert does not apply cleanly. Revert the commit by hand.",
    "unrecognised-commit": "The commit is not a recognised PR merge. Revert it by hand.",
    "failed": "Creating the revert PR failed. See the run log.",
    "": "No revert PR was created. Check the run log: if the failure was before the build step nothing was deployed, otherwise revert by hand.",
}
facts = [
    {"title": "Environment", "value": e["NOTIFY_ENV"]},
    {"title": "Branch", "value": e["NOTIFY_BRANCH"]},
    {"title": "Commit", "value": e["NOTIFY_SHA"]},
    {"title": "Run", "value": e["NOTIFY_RUN_ID"]},
]
actions = [{"type": "Action.OpenUrl", "title": "Open pipeline run", "url": e["NOTIFY_RUN_URL"]}]
if e["NOTIFY_REVERT_URL"]:
    actions.append(
        {"type": "Action.OpenUrl", "title": "Open revert PR", "url": e["NOTIFY_REVERT_URL"]}
    )
card = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.4",
    "body": [
        {
            "type": "TextBlock",
            "text": "dbt deployment failed on " + e["NOTIFY_ENV"],
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
        {"type": "TextBlock", "text": notes.get(status, notes["failed"]), "wrap": True},
        {
            "type": "TextBlock",
            "text": "Table data is not restored by a revert PR; use the Phase 3 restore if data was damaged.",
            "wrap": True,
            "isSubtle": True,
        },
    ],
    "actions": actions,
}
message = {
    "type": "message",
    "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
    ],
}
print(json.dumps(message))
'
}

send_payload() {
  local url="${TEAMS_WEBHOOK_URL}"
  [[ "${url}" =~ ^https://[^\"[:space:]]+$ ]] || die "TEAMS_WEBHOOK_URL is not a valid https URL"
  # The URL is passed to curl through a config on stdin so it never shows up in the process list.
  printf 'url = "%s"\n' "${url}" | curl --config - --silent --show-error --fail \
    --max-time 30 --retry 2 --header 'Content-Type: application/json' \
    --data-binary "@${PAYLOAD_FILE}" >/dev/null || die "posting to Teams failed"
}

cleanup() {
  [[ -z "${PAYLOAD_FILE}" ]] || rm -f "${PAYLOAD_FILE}"
}

main() {
  parse_args "$@"
  read_revert_file
  find_python
  PAYLOAD_FILE="$(mktemp)"
  trap cleanup EXIT
  build_payload >"${PAYLOAD_FILE}"

  if [[ "${DRY_RUN}" == true ]]; then
    cat "${PAYLOAD_FILE}"
    return 0
  fi
  if [[ -z "${TEAMS_WEBHOOK_URL:-}" ]]; then
    echo "##vso[task.logissue type=warning]TEAMS_WEBHOOK_URL is not set; failure notification skipped"
    log "WARNING: TEAMS_WEBHOOK_URL is not set; notification skipped"
    return 0
  fi
  send_payload
  log "notification sent"
}

main "$@"
