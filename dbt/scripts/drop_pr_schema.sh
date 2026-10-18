#!/usr/bin/env bash
# Drops the temporary CI schema CI_PR_<pr number> in the environment's database.
# Safe to run twice (DROP SCHEMA IF EXISTS). Refuses to touch anything but CI_PR_<digits>.
#
# Usage: drop_pr_schema.sh <pr number>
# Needs SNOWFLAKE_DATABASE plus the connection variables described in lib.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

usage() {
  echo "Usage: $0 <pr number>   e.g. $0 42" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
PR_NUMBER="$1"
[[ "${PR_NUMBER}" =~ ^[0-9]+$ && "${PR_NUMBER}" != "0" ]] || die "invalid PR number '${PR_NUMBER}'"
require_env SNOWFLAKE_DATABASE
validate_identifier SNOWFLAKE_DATABASE "${SNOWFLAKE_DATABASE}"
build_snow_conn

# Identifiers are validated above, then double-quoted. dbt creates the schema unquoted,
# which Snowflake stores in upper case, so the quoted name is upper case too.
DB_UPPER="${SNOWFLAKE_DATABASE^^}"
SQL="DROP SCHEMA IF EXISTS \"${DB_UPPER}\".\"CI_PR_${PR_NUMBER}\""

log "running: ${SQL}"
"${SNOW_BIN}" sql --query "${SQL}" "${SNOW_CONN[@]}" >&2 \
  || die "could not drop schema CI_PR_${PR_NUMBER}"
log "schema CI_PR_${PR_NUMBER} dropped (or did not exist)"
