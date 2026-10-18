#!/usr/bin/env bash
# Downloads the last good dbt manifest for an environment into ./state/manifest.json.
#
# Usage: fetch_state.sh <env>        (env = dev | test | ...)
#
# Exit codes:
#   0   manifest downloaded to ./state/manifest.json
#   10  no manifest on the stage yet (first run): caller should do a full build
#   1   anything else (bad input, auth, network, ...): caller must stop
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

usage() {
  echo "Usage: $0 <env>   e.g. $0 dev" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
ENV_NAME="$1"
validate_env_name "${ENV_NAME}"
build_snow_conn

REMOTE="${STATE_STAGE}/${ENV_NAME}/manifest.json"

# Ask the stage first, so only a confirmed "not found" counts as a first run.
# A failure of this call (auth, network, missing grant) is never treated as a first run.
log "checking ${REMOTE}"
listing="$("${SNOW_BIN}" stage list-files "${REMOTE}" --format json "${SNOW_CONN[@]}")" \
  || die "could not list ${REMOTE}"

if ! grep -qi 'manifest\.json' <<<"${listing}"; then
  log "no manifest found for '${ENV_NAME}': first run"
  exit "${EXIT_NO_STATE}"
fi

rm -rf state
mkdir -p state
"${SNOW_BIN}" stage copy "${REMOTE}" ./state/ "${SNOW_CONN[@]}" >&2 \
  || die "download of ${REMOTE} failed"
[[ -s state/manifest.json ]] || die "downloaded manifest is missing or empty"
log "manifest for '${ENV_NAME}' saved to state/manifest.json"
