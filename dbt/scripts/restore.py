#!/usr/bin/env python3
"""Restore tables from the backups logged in CICD.AUDIT.BACKUP_LOG.

Usage:
  python scripts/restore.py --run-id 1234 [--table ORDERS] [--dry-run] [--confirm]
  python scripts/restore.py --pr-number 42 [--run-id 1234] [--table ORDERS] [--confirm]

Without --confirm this only prints the plan (same as --dry-run). With --confirm each table is
first cloned to <DB>.BACKUP.<TABLE>__RESTORE_<timestamp> (a safety copy, also logged and
cleaned up by cleanup_backups.py) and then replaced with
CREATE OR REPLACE TABLE ... CLONE ... COPY GRANTS.
All backups are checked before any table is touched. If the table is gone, it is recreated.

Environment: SNOWFLAKE_* variables described in backup_lib.py; optional RUN_ID for log lines.
Exit codes: 0 ok, 2 bad input/config, 3 cannot connect, 4 SQL failure or backup missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from typing import Any

import backup_lib as lib


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", help="pipeline run that created the backups")
    parser.add_argument("--pr-number", help="pull request the backups are logged against")
    parser.add_argument("--table", help="restore only this table (name, or DB.SCHEMA.TABLE)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    parser.add_argument(
        "--confirm", action="store_true", help="really replace the tables (otherwise plan only)"
    )
    args = parser.parse_args(argv)
    if not args.run_id and not args.pr_number:
        parser.error("give --run-id and/or --pr-number")
    return args


def find_backups(
    cursor: Any, run_id: str | None, pr_number: str | None, table: str | None
) -> list[lib.LogRow]:
    """Log rows for the selection whose backup has not been dropped yet."""
    sql = (
        "SELECT run_id, pr_number, environment, source_table, backup_table "  # noqa: S608
        f"FROM {lib.LOG_TABLE} WHERE dropped_at IS NULL"
    )
    params: list[str] = []
    if run_id:
        sql += " AND run_id = %s"
        params.append(lib.validate_run_id(run_id))
    else:
        # Safety copies from earlier restores are only chosen by their explicit RESTORE_<ts> id.
        sql += " AND run_id NOT LIKE 'RESTORE!_%' ESCAPE '!'"
    if pr_number:
        sql += " AND pr_number = %s"
        params.append(lib.optional_pr_number(pr_number) or "")
    if table:
        if "." in table:
            sql += " AND source_table = %s"
            params.append(".".join(lib.split_qualified("table", table)))
        else:
            sql += " AND SPLIT_PART(source_table, '.', 3) = %s"
            params.append(lib.validate_identifier("table", table))
    sql += " ORDER BY created_at, source_table"
    rows = lib.execute(cursor, sql, tuple(params)).fetchall()
    return [lib.LogRow(*row) for row in rows]


def check_single_run(rows: list[lib.LogRow], run_id: str | None) -> None:
    """A PR can have several deploy runs; refuse to guess which one to restore."""
    runs = sorted({row.run_id for row in rows})
    if len(runs) > 1 and not run_id:
        raise lib.ScriptError(
            f"PR matches several runs ({', '.join(runs)}); pass --run-id to choose one",
            lib.EXIT_USAGE,
        )


def preflight(cursor: Any, rows: list[lib.LogRow]) -> None:
    """Fail before changing anything if a logged backup no longer exists."""
    missing = []
    for row in rows:
        db, schema, table = lib.split_qualified("backup_table", row.backup_table, lib.BACKUP_SCHEMA)
        if not lib.table_exists(cursor, db, schema, table):
            missing.append(row.backup_table)
    if missing:
        raise lib.ScriptError(
            f"backup table(s) listed in the log no longer exist: {', '.join(missing)}", lib.EXIT_SQL
        )


def restore_table(cursor: Any, row: lib.LogRow, stamp: str, restore_run_id: str) -> None:
    src_db, src_schema, src_table = lib.split_qualified("source_table", row.source_table)
    bkp_db, bkp_schema, bkp_table = lib.split_qualified(
        "backup_table", row.backup_table, lib.BACKUP_SCHEMA
    )

    if lib.table_exists(cursor, src_db, src_schema, src_table):
        safety = lib.backup_table_parts(src_db, src_table, f"RESTORE_{stamp}")
        lib.execute(
            cursor,
            f"CREATE TABLE IF NOT EXISTS {lib.qualified(*safety)} "
            f"CLONE {lib.qualified(src_db, src_schema, src_table)}",
        )
        lib.insert_log_row(
            cursor,
            run_id=restore_run_id,
            pr_number=row.pr_number,
            commit_sha=None,
            environment=row.environment,
            source_table=row.source_table,
            backup_table=".".join(safety),
        )
        lib.logger.info("safety copy of current %s -> %s", row.source_table, ".".join(safety))

    lib.execute(
        cursor,
        f"CREATE OR REPLACE TABLE {lib.qualified(src_db, src_schema, src_table)} "
        f"CLONE {lib.qualified(bkp_db, bkp_schema, bkp_table)} COPY GRANTS",
    )
    lib.logger.info("restored %s from %s", row.source_table, row.backup_table)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lib.setup_logging(os.environ.get("RUN_ID") or "restore")
    dry_run = args.dry_run or not args.confirm
    if not args.dry_run and not args.confirm:
        lib.logger.warning("--confirm not given: showing the plan only, nothing will change")

    with lib.snowflake_cursor() as cursor:
        rows = find_backups(cursor, args.run_id, args.pr_number, args.table)
        if not rows:
            raise lib.ScriptError("no backups found for that selection", lib.EXIT_USAGE)
        check_single_run(rows, args.run_id)
        preflight(cursor, rows)

        if dry_run:
            for row in rows:
                lib.logger.info(
                    "dry run: would restore %s from %s (run %s)",
                    row.source_table,
                    row.backup_table,
                    row.run_id,
                )
            return lib.EXIT_OK

        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        restore_run_id = f"RESTORE_{stamp}"
        for row in rows:
            restore_table(cursor, row, stamp, restore_run_id)
    lib.logger.info("restored %d table(s)", len(rows))
    return lib.EXIT_OK


if __name__ == "__main__":
    sys.exit(lib.run_script(main))
