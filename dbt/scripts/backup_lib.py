"""Shared helpers for backup.py, restore.py and cleanup_backups.py.

Credentials come only from environment variables (never from files in the repo):
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_KEY_PATH, SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE
Optional: PRIVATE_KEY_PASSPHRASE (encrypted keys), RUN_ID (shown in every log line).

Safe SQL rules used by all three scripts:
  * identifiers must match ^[A-Za-z0-9_]+$, are upper-cased (dbt-snowflake creates unquoted,
    therefore upper-case, objects) and then double-quoted;
  * every value (run id, PR number, commit, names compared in WHERE clauses) is a bind parameter.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import snowflake.connector
from snowflake.connector import errors as sf_errors

EXIT_OK = 0
EXIT_USAGE = 2  # bad arguments, bad environment variables, unreadable input files
EXIT_CONNECTION = 3  # could not connect to Snowflake (after retries)
EXIT_SQL = 4  # a SQL statement failed, or a backup that should exist is missing

BACKUP_SCHEMA = "BACKUP"
LOG_TABLE = "CICD.AUDIT.BACKUP_LOG"
CONNECT_ATTEMPTS = 3
CONNECT_BASE_DELAY_SECONDS = 2.0
NAME_SEPARATOR = "__"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_ENVIRONMENT = re.compile(r"^[a-z]+$")
_PR_NUMBER = re.compile(r"^[0-9]{1,9}$")
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Connection problems worth retrying. SQL errors (ProgrammingError etc.) are never retried.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    sf_errors.OperationalError,
    sf_errors.InterfaceError,
    ConnectionError,
    TimeoutError,
)

logger = logging.getLogger("backup")


class ScriptError(Exception):
    """A failure that ends the script with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(run_id: str) -> None:
    """Log to stderr with the run ID in every message."""
    handler = logging.StreamHandler()
    handler.addFilter(_RunIdFilter(run_id))
    handler.setFormatter(
        logging.Formatter("%(asctime)s [run %(run_id)s] %(levelname)s %(message)s")
    )
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ---------------------------------------------------------------- validation and quoting


def validate_identifier(kind: str, value: str) -> str:
    """Return the upper-cased identifier, or raise a usage error."""
    if not _IDENTIFIER.fullmatch(value):
        raise ScriptError(
            f"invalid {kind} {value!r} (letters, digits and underscore only)", EXIT_USAGE
        )
    return value.upper()


def quote_ident(value: str) -> str:
    """Double-quote an identifier that already passed validate_identifier."""
    return '"' + value.replace('"', '""') + '"'


def qualified(*parts: str) -> str:
    """Quoted, dot-joined name from validated parts."""
    return ".".join(quote_ident(p) for p in parts)


def split_qualified(kind: str, value: str, schema: str | None = None) -> tuple[str, str, str]:
    """Split 'DB.SCHEMA.TABLE' (as stored in the log) into validated parts."""
    parts = value.split(".")
    if len(parts) != 3:
        raise ScriptError(f"{kind} {value!r} is not DB.SCHEMA.TABLE", EXIT_USAGE)
    db, sch, table = (validate_identifier(kind, p) for p in parts)
    if schema is not None and sch != schema:
        raise ScriptError(f"{kind} {value!r} is not in the {schema} schema", EXIT_USAGE)
    return db, sch, table


def validate_run_id(value: str | None) -> str:
    if not value or not _RUN_ID.fullmatch(value):
        raise ScriptError(
            "RUN_ID must be set to 1-64 letters, digits or underscores "
            f"(got {value!r}); it becomes part of the backup table name",
            EXIT_USAGE,
        )
    return value


def validate_environment(value: str | None) -> str:
    if not value or not _ENVIRONMENT.fullmatch(value):
        raise ScriptError(
            f"environment must be lowercase letters, e.g. dev (got {value!r})", EXIT_USAGE
        )
    return value


def optional_pr_number(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not _PR_NUMBER.fullmatch(value):
        raise ScriptError(f"PR number must be digits only (got {value!r})", EXIT_USAGE)
    return value


def optional_commit_sha(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not _COMMIT_SHA.fullmatch(value):
        raise ScriptError("COMMIT_SHA must be a hex commit id", EXIT_USAGE)
    return value


def backup_table_parts(database: str, table: str, suffix: str) -> tuple[str, str, str]:
    """(database, BACKUP, '<TABLE>__<suffix>') for a source table."""
    return database, BACKUP_SCHEMA, f"{table}{NAME_SEPARATOR}{suffix.upper()}"


# ---------------------------------------------------------------- connection


def load_connection_config() -> dict[str, Any]:
    """Build connector arguments from environment variables; never log the values."""
    required = (
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_KEY_PATH",
        "SNOWFLAKE_ROLE",
        "SNOWFLAKE_WAREHOUSE",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ScriptError(f"environment variable(s) not set: {', '.join(missing)}", EXIT_USAGE)
    key_path = os.environ["SNOWFLAKE_KEY_PATH"]
    if not os.path.isfile(key_path):
        raise ScriptError(f"private key file is not readable: {key_path}", EXIT_USAGE)
    config: dict[str, Any] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "private_key_file": key_path,
        "role": os.environ["SNOWFLAKE_ROLE"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "login_timeout": 60,
        "network_timeout": 300,
        "application": "dbt-backup",
    }
    passphrase = os.environ.get("PRIVATE_KEY_PASSPHRASE")
    if passphrase:
        config["private_key_file_pwd"] = passphrase
    return config


def connect_with_retry(
    config: dict[str, Any],
    attempts: int = CONNECT_ATTEMPTS,
    base_delay: float = CONNECT_BASE_DELAY_SECONDS,
) -> Any:
    """Connect to Snowflake, retrying transient errors (default max 3 attempts)."""
    for attempt in range(1, attempts + 1):
        try:
            return snowflake.connector.connect(**config)
        except TRANSIENT_ERRORS as exc:
            if attempt == attempts:
                raise ScriptError(
                    f"could not connect to Snowflake after {attempts} attempts: {exc}",
                    EXIT_CONNECTION,
                ) from exc
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning(
                "connection attempt %d/%d failed (%s); retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
        except sf_errors.Error as exc:
            # Authentication, authorization or configuration problems will not fix themselves.
            raise ScriptError(f"Snowflake connection refused: {exc}", EXIT_CONNECTION) from exc
    raise AssertionError("unreachable")  # pragma: no cover


@contextmanager
def snowflake_cursor() -> Iterator[Any]:
    """Open a connection and yield a cursor; always close both."""
    conn = connect_with_retry(load_connection_config())
    try:
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
    finally:
        conn.close()


def execute(cursor: Any, sql: str, params: tuple[Any, ...] | None = None) -> Any:
    """Run one statement; turn Snowflake SQL errors into a clear exit-4 failure."""
    try:
        cursor.execute(sql, params) if params is not None else cursor.execute(sql)
    except sf_errors.Error as exc:
        raise ScriptError(f"SQL failed: {exc}\nStatement: {sql}", EXIT_SQL) from exc
    return cursor


# ---------------------------------------------------------------- shared queries


def table_exists(cursor: Any, database: str, schema: str, table: str) -> bool:
    """True if a base table exists. Names are matched with bind parameters."""
    sql = (
        f"SELECT 1 FROM {quote_ident(database)}.INFORMATION_SCHEMA.TABLES "  # noqa: S608
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_TYPE = 'BASE TABLE'"
    )
    return execute(cursor, sql, (schema, table)).fetchone() is not None


@dataclass(frozen=True)
class LogRow:
    run_id: str
    pr_number: str | None
    environment: str
    source_table: str
    backup_table: str


def insert_log_row(
    cursor: Any,
    *,
    run_id: str,
    pr_number: str | None,
    commit_sha: str | None,
    environment: str,
    source_table: str,
    backup_table: str,
) -> None:
    """Idempotent log insert: one row per (run_id, source_table). created_at is the default."""
    sql = (
        f"INSERT INTO {LOG_TABLE} "  # noqa: S608
        "(run_id, pr_number, commit_sha, environment, source_table, backup_table) "
        "SELECT %s, %s, %s, %s, %s, %s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {LOG_TABLE} WHERE run_id = %s AND source_table = %s)"
    )
    params = (
        run_id,
        pr_number,
        commit_sha,
        environment,
        source_table,
        backup_table,
        run_id,
        source_table,
    )
    execute(cursor, sql, params)


def run_script(main: Any, argv: list[str] | None = None) -> int:
    """Run main(argv) and translate ScriptError into its exit code."""
    try:
        return int(main(argv))
    except ScriptError as exc:
        logger.error("%s", exc)
        return exc.code
