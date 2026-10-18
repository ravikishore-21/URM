#!/usr/bin/env bash
# Uploads target/manifest.json as the new state for an environment.
# Call it only after a successful `dbt build`; the pipeline's `set -e` guarantees that.
#
# Usage: upload_state.sh <env>
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

MANIFEST="target/manifest.json"
[[ -s "${MANIFEST}" ]] || die "${MANIFEST} not found or empty: run dbt build first"
python - "${MANIFEST}" <<'PY' || die "${MANIFEST} is not valid JSON"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    json.load(handle)
PY

build_snow_conn
REMOTE="${STATE_STAGE}/${ENV_NAME}/"

log "uploading ${MANIFEST} to ${REMOTE}"
"${SNOW_BIN}" stage copy "${MANIFEST}" "${REMOTE}" --overwrite "${SNOW_CONN[@]}" >&2 \
  || die "upload to ${REMOTE} failed"
log "state for '${ENV_NAME}' updated"
