"""Tests for dbt/scripts/restore.py and cleanup_backups.py with the connector mocked."""

import backup_lib
import cleanup_backups
import pytest
import restore
from conftest import FakeCursor

SRC = "ANALYTICS_DEV.ANALYTICS.FCT_ORDERS"
BKP = "ANALYTICS_DEV.BACKUP.FCT_ORDERS__77"


def log_row(run_id: str = "77", backup: str = BKP, source: str = SRC) -> tuple:
    return (run_id, "42", "dev", source, backup)


# ------------------------------------------------------------------ restore


def test_restore_without_confirm_is_a_dry_run(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row()]
    cursor.existing = {("BACKUP", "FCT_ORDERS__77")}
    assert backup_lib.run_script(restore.main, ["--run-id", "77"]) == 0
    assert cursor.writes() == []


def test_restore_dry_run_flag_wins_over_confirm(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row()]
    cursor.existing = {("BACKUP", "FCT_ORDERS__77")}
    assert backup_lib.run_script(restore.main, ["--run-id", "77", "--confirm", "--dry-run"]) == 0
    assert cursor.writes() == []


def test_restore_with_confirm_makes_safety_copy_then_replaces(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row()]
    cursor.existing = {("BACKUP", "FCT_ORDERS__77"), ("ANALYTICS", "FCT_ORDERS")}
    assert backup_lib.run_script(restore.main, ["--run-id", "77", "--confirm"]) == 0

    creates = [s[0] for s in cursor.sql("CREATE")]
    assert creates[0].startswith(
        'CREATE TABLE IF NOT EXISTS "ANALYTICS_DEV"."BACKUP"."FCT_ORDERS__RESTORE_'
    )
    assert creates[1] == (
        'CREATE OR REPLACE TABLE "ANALYTICS_DEV"."ANALYTICS"."FCT_ORDERS" '
        'CLONE "ANALYTICS_DEV"."BACKUP"."FCT_ORDERS__77" COPY GRANTS'
    )
    _, params = cursor.sql("INSERT")[0]
    assert params[0].startswith("RESTORE_") and params[1] == "42"  # logged, so cleanup drops it


def test_restore_recreates_a_missing_table_without_safety_copy(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row()]
    cursor.existing = {("BACKUP", "FCT_ORDERS__77")}
    assert backup_lib.run_script(restore.main, ["--run-id", "77", "--confirm"]) == 0
    assert len(cursor.sql("CREATE")) == 1 and "OR REPLACE" in cursor.sql("CREATE")[0][0]


def test_restore_refuses_pr_with_several_runs(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row("77"), log_row("78", "ANALYTICS_DEV.BACKUP.FCT_ORDERS__78")]
    assert backup_lib.run_script(restore.main, ["--pr-number", "42", "--confirm"]) == 2
    assert cursor.writes() == []


def test_restore_changes_nothing_if_any_backup_is_gone(cursor: FakeCursor) -> None:
    other = "ANALYTICS_DEV.BACKUP.DIM_X__77"
    cursor.log_rows = [log_row(), log_row(backup=other, source="ANALYTICS_DEV.ANALYTICS.DIM_X")]
    cursor.existing = {("BACKUP", "FCT_ORDERS__77")}  # DIM_X backup was dropped by hand
    assert backup_lib.run_script(restore.main, ["--run-id", "77", "--confirm"]) == 4
    assert cursor.writes() == []


def test_restore_with_no_matching_backup_fails(cursor: FakeCursor) -> None:
    assert backup_lib.run_script(restore.main, ["--run-id", "999", "--confirm"]) == 2


def test_restore_passes_values_as_bind_parameters(cursor: FakeCursor) -> None:
    assert backup_lib.run_script(restore.main, ["--run-id", "77", "--table", "fct_orders"]) == 2
    sql, params = cursor.statements[0]
    assert "77" not in sql and "FCT_ORDERS" not in sql
    assert params == ("77", "FCT_ORDERS")


def test_restore_rejects_backup_outside_backup_schema(cursor: FakeCursor) -> None:
    evil = "ANALYTICS_DEV.ANALYTICS.FCT_ORDERS"
    cursor.log_rows = [log_row(backup=evil)]
    assert backup_lib.run_script(restore.main, ["--run-id", "77", "--confirm"]) == 2
    assert cursor.writes() == []


def test_restore_requires_a_selection() -> None:
    with pytest.raises(SystemExit) as exc:
        restore.main([])
    assert exc.value.code == 2


# ------------------------------------------------------------------ cleanup


def test_cleanup_needs_a_retention_period(cursor: FakeCursor) -> None:
    assert backup_lib.run_script(cleanup_backups.main, []) == 2
    assert cursor.statements == []


@pytest.mark.parametrize("days", ["0", "-1", "abc", "99999", "1.5"])
def test_cleanup_rejects_bad_retention(cursor: FakeCursor, days: str) -> None:
    assert backup_lib.run_script(cleanup_backups.main, ["--days", days]) == 2


def test_cleanup_reads_retention_from_environment(
    cursor: FakeCursor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "14")
    assert backup_lib.run_script(cleanup_backups.main, []) == 0
    assert cursor.statements[0][1] == ("dev", 14)


def test_cleanup_drops_and_marks_expired_backups(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row()]
    assert backup_lib.run_script(cleanup_backups.main, ["--days", "14"]) == 0
    assert (
        cursor.sql("DROP")[0][0] == 'DROP TABLE IF EXISTS "ANALYTICS_DEV"."BACKUP"."FCT_ORDERS__77"'
    )
    assert cursor.sql("UPDATE")[0][1] == ("77", BKP)


def test_cleanup_dry_run_changes_nothing(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row()]
    assert backup_lib.run_script(cleanup_backups.main, ["--days", "14", "--dry-run"]) == 0
    assert cursor.writes() == []


def test_cleanup_never_drops_outside_backup_schema(cursor: FakeCursor) -> None:
    cursor.log_rows = [log_row(backup="ANALYTICS_DEV.ANALYTICS.FCT_ORDERS")]
    assert backup_lib.run_script(cleanup_backups.main, ["--days", "14"]) == 4
    assert cursor.sql("DROP") == []


def test_cleanup_continues_after_a_failed_drop_then_exits_4(cursor: FakeCursor) -> None:
    second = "ANALYTICS_DEV.BACKUP.DIM_X__77"
    cursor.log_rows = [log_row(), log_row(backup=second, source="ANALYTICS_DEV.ANALYTICS.DIM_X")]
    cursor.fail_on = '"FCT_ORDERS__77"'
    assert backup_lib.run_script(cleanup_backups.main, ["--days", "14"]) == 4
    assert any("DIM_X__77" in s[0] for s in cursor.sql("DROP"))


def test_cleanup_requires_an_environment(
    cursor: FakeCursor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TARGET_ENV")
    assert backup_lib.run_script(cleanup_backups.main, ["--days", "14"]) == 2
