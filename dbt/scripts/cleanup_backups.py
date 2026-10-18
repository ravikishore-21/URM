#!/usr/bin/env python3
"""Drop backup tables older than N days and mark them dropped in CICD.AUDIT.BACKUP_LOG.

Usage: python scripts/cleanup_backups.py [--days N] [--environment dev] [--dry-run]

Retention: --days, else the BACKUP_RETENTION_DAYS environment variable. There is deliberately no
built-in default: the retention period is a client decision (docs/open-questions.md, question 3).
Environment: --environment, else TARGET_ENV; only that environment's backups are touched.
Only tables in a BACKUP schema are ever dropped. Log rows are kept (dropped_at is set).

Exit codes: 0 ok, 2 bad input/config, 3 cannot connect, 4 one or more drops failed
(the remaining backups are still processed).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import backup_lib as lib

MAX_RETENTION_DAYS = 3650


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", help="retention in days (default: $BACKUP_RETENTION_DAYS)")
    parser.add_argument("--environment", help="environment to clean (default: $TARGET_ENV)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be dropped")
    return parser.parse_args(argv)


def resolve_retention_days(cli_value: str | None) -> int:
    raw = cli_value or os.environ.get("BACKUP_RETENTION_DAYS")
    if not raw:
        raise lib.ScriptError(
            "retention not set: pass --days or set BACKUP_RETENTION_DAYS "
            "(the period is an open client decision)",
            lib.EXIT_USAGE,
        )
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_RETENTION_DAYS:
        raise lib.ScriptError(
            f"retention must be a whole number of days, 1 to {MAX_RETENTION_DAYS} (got {raw!r})",
            lib.EXIT_USAGE,
        )
    return int(raw)


def find_expired(cursor: Any, days: int, environment: str) -> list[lib.LogRow]:
    sql = (
        "SELECT run_id, pr_number, environment, source_table, backup_table "  # noqa: S608
        f"FROM {lib.LOG_TABLE} "
        "WHERE dropped_at IS NULL AND environment = %s "
        "AND created_at < DATEADD(day, -%s, CURRENT_TIMESTAMP()) "
        "ORDER BY created_at, backup_table"
    )
    rows = lib.execute(cursor, sql, (environment, days)).fetchall()
    return [lib.LogRow(*row) for row in rows]


def drop_backup(cursor: Any, row: lib.LogRow) -> None:
    db, schema, table = lib.split_qualified("backup_table", row.backup_table, lib.BACKUP_SCHEMA)
    lib.execute(cursor, f"DROP TABLE IF EXISTS {lib.qualified(db, schema, table)}")
    lib.execute(
        cursor,
        f"UPDATE {lib.LOG_TABLE} SET dropped_at = CURRENT_TIMESTAMP() "  # noqa: S608
        "WHERE run_id = %s AND backup_table = %s AND dropped_at IS NULL",
        (row.run_id, row.backup_table),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lib.setup_logging(os.environ.get("RUN_ID") or "cleanup")
    days = resolve_retention_days(args.days)
    environment = lib.validate_environment(args.environment or os.environ.get("TARGET_ENV"))

    failures = 0
    with lib.snowflake_cursor() as cursor:
        expired = find_expired(cursor, days, environment)
        lib.logger.info(
            "%d backup(s) in %s older than %d days%s",
            len(expired),
            environment,
            days,
            " (dry run)" if args.dry_run else "",
        )
        for row in expired:
            if args.dry_run:
                lib.logger.info("dry run: would drop %s", row.backup_table)
                continue
            try:
                drop_backup(cursor, row)
                lib.logger.info("dropped %s", row.backup_table)
            except lib.ScriptError as exc:
                failures += 1
                lib.logger.error("could not drop %s: %s", row.backup_table, exc)
    if failures:
        raise lib.ScriptError(f"{failures} backup(s) could not be dropped", lib.EXIT_SQL)
    return lib.EXIT_OK


if __name__ == "__main__":
    sys.exit(lib.run_script(main))
