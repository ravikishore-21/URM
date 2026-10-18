#!/usr/bin/env bash
# Shared helpers for the state scripts. Source this file; do not run it.
# shellcheck disable=SC2034  # variables here are used by the scripts that source this file
#
# Credentials come only from environment variables (never from files in the repo):
#   SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_KEY_PATH, SNOWFLAKE_ROLE,
#   SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE
# Optional: SNOW_BIN (path to the snow executable, default "snow"),
#           RUN_ID (shown in every log line), PRIVATE_KEY_PASSPHRASE (encrypted keys).

# Exit code fetch_state.sh uses for "no manifest on the stage yet" (first run).
# Any other non-zero code is a real failure and must stop the pipeline.
readonly EXIT_NO_STATE=10

SNOW_BIN="${SNOW_BIN:-snow}"
STATE_STAGE="@CICD.STATE.DBT_STATE"

log() {
  echo "[run ${RUN_ID:-local}] $*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

require_env() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || die "environment variable ${name} is not set"
  done
}

# Environment names become stage paths, so allow lowercase letters only.
validate_env_name() {
  [[ "$1" =~ ^[a-z]+$ ]] || die "invalid environment name '$1' (expected lowercase letters, e.g. dev)"
}

# Snowflake identifiers are double-quoted in SQL; allow only simple names.
validate_identifier() {
  [[ "$2" =~ ^[A-Za-z0-9_]+$ ]] || die "invalid $1 '$2' (letters, digits and underscore only)"
}

# Builds the key-pair connection flags for `snow` in the array SNOW_CONN.
# --temporary-connection means no config file with account details is needed.
build_snow_conn() {
  require_env SNOWFLAKE_ACCOUNT SNOWFLAKE_USER SNOWFLAKE_KEY_PATH SNOWFLAKE_ROLE SNOWFLAKE_WAREHOUSE
  [[ -r "${SNOWFLAKE_KEY_PATH}" ]] || die "private key file is not readable: ${SNOWFLAKE_KEY_PATH}"
  SNOW_CONN=(
    --temporary-connection
    --account "${SNOWFLAKE_ACCOUNT}"
    --user "${SNOWFLAKE_USER}"
    --authenticator SNOWFLAKE_JWT
    --private-key-file "${SNOWFLAKE_KEY_PATH}"
    --role "${SNOWFLAKE_ROLE}"
    --warehouse "${SNOWFLAKE_WAREHOUSE}"
  )
}
