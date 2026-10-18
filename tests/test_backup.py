"""Tests for dbt/scripts/backup.py with the Snowflake connector mocked."""

import backup
import backup_lib
import pytest
from conftest import FakeCursor, make_node
from snowflake.connector import errors as sf_errors

CLONE_SQL = (
    'CREATE TABLE IF NOT EXISTS "ANALYTICS_DEV"."BACKUP"."FCT_ORDERS__77" '
    'CLONE "ANALYTICS_DEV"."ANALYTICS"."FCT_ORDERS"'
)


def run(write_inputs, nodes, changed, *flags) -> int:
    changed_file, manifest = write_inputs(nodes, changed)
    return backup_lib.run_script(
        backup.main, [str(changed_file), "--manifest", str(manifest), *flags]
    )


def test_view_is_skipped(cursor: FakeCursor, write_inputs) -> None:
    cursor.existing = {("ANALYTICS", "STG_ORDERS")}
    assert run(write_inputs, [make_node("stg_orders", "view")], ["stg_orders"]) == 0
    assert cursor.statements == []  # a view never even reaches Snowflake


def test_new_table_is_skipped(cursor: FakeCursor, write_inputs) -> None:
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 0
    assert len(cursor.sql("SELECT 1")) == 1  # existence was checked
    assert cursor.writes() == []


def test_existing_table_is_cloned_and_logged(
    cursor: FakeCursor, write_inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("COMMIT_SHA", "abc1234")
    cursor.existing = {("ANALYTICS", "FCT_ORDERS")}
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 0

    exists_sql, exists_params = cursor.sql("SELECT 1")[0]
    assert "FCT_ORDERS" not in exists_sql  # values are bound, not interpolated
    assert exists_params == ("ANALYTICS", "FCT_ORDERS")
    assert cursor.sql("CREATE TABLE")[0] == (CLONE_SQL, None)

    insert_sql, params = cursor.sql("INSERT INTO CICD.AUDIT.BACKUP_LOG")[0]
    assert "VALUES" not in insert_sql and "%s" in insert_sql
    assert params == (
        "77",
        "42",
        "abc1234",
        "dev",
        "ANALYTICS_DEV.ANALYTICS.FCT_ORDERS",
        "ANALYTICS_DEV.BACKUP.FCT_ORDERS__77",
        "77",
        "ANALYTICS_DEV.ANALYTICS.FCT_ORDERS",
    )


def test_incremental_uses_alias(cursor: FakeCursor, write_inputs) -> None:
    cursor.existing = {("ANALYTICS", "ORDERS_ALIAS")}
    node = make_node("fct_orders", "incremental", alias="orders_alias")
    assert run(write_inputs, [node], ["fct_orders"]) == 0
    assert '"ORDERS_ALIAS__77"' in cursor.sql("CREATE TABLE")[0][0]


def test_dry_run_changes_nothing(cursor: FakeCursor, write_inputs) -> None:
    cursor.existing = {("ANALYTICS", "FCT_ORDERS")}
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"], "--dry-run") == 0
    assert len(cursor.sql("SELECT 1")) == 1
    assert cursor.writes() == []


def test_only_changed_models_are_considered(cursor: FakeCursor, write_inputs) -> None:
    cursor.existing = {("ANALYTICS", "FCT_ORDERS"), ("ANALYTICS", "DIM_X")}
    nodes = [make_node("fct_orders", "table"), make_node("dim_x", "table")]
    assert run(write_inputs, nodes, ["fct_orders"]) == 0
    assert len(cursor.sql("CREATE TABLE")) == 1


def test_empty_changed_list_never_connects(
    cursor: FakeCursor, write_inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(backup_lib.snowflake.connector, "connect", refuse)
    changed_file, manifest = write_inputs([make_node("fct_orders", "table")], [])
    assert backup_lib.run_script(backup.main, [str(changed_file), "--manifest", str(manifest)]) == 0


def test_rerun_is_idempotent_by_sql(cursor: FakeCursor, write_inputs) -> None:
    cursor.existing = {("ANALYTICS", "FCT_ORDERS")}
    for _ in range(2):
        assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 0
    assert all("IF NOT EXISTS" in s[0] for s in cursor.sql("CREATE TABLE"))
    assert all("WHERE NOT EXISTS" in s[0] for s in cursor.sql("INSERT"))


def test_connection_failure_retries_three_times(
    write_inputs, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    calls: list[int] = []

    def fail(**_kwargs):
        calls.append(1)
        raise sf_errors.OperationalError("network down")

    monkeypatch.setattr(backup_lib.snowflake.connector, "connect", fail)
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 3
    assert len(calls) == 3
    assert no_sleep == [2.0, 4.0]  # backoff between attempts, none after the last


def test_transient_failure_then_success(
    cursor: FakeCursor, write_inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}
    real = backup_lib.snowflake.connector.connect

    def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sf_errors.OperationalError("blip")
        return real(**kwargs)

    monkeypatch.setattr(backup_lib.snowflake.connector, "connect", flaky)
    cursor.existing = {("ANALYTICS", "FCT_ORDERS")}
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 0
    assert attempts["n"] == 3


def test_auth_failure_is_not_retried(write_inputs, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def refuse(**_kwargs):
        calls.append(1)
        raise sf_errors.DatabaseError("bad key")

    monkeypatch.setattr(backup_lib.snowflake.connector, "connect", refuse)
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 3
    assert len(calls) == 1


def test_clone_failure_exits_4_and_logs_nothing(cursor: FakeCursor, write_inputs) -> None:
    cursor.existing = {("ANALYTICS", "FCT_ORDERS")}
    cursor.fail_on = "CREATE TABLE"
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 4
    assert cursor.sql("INSERT") == []


def test_unsafe_identifier_is_rejected(cursor: FakeCursor, write_inputs) -> None:
    node = make_node("fct_orders", "table", alias='x"; DROP TABLE y; --')
    assert run(write_inputs, [node], ["fct_orders"]) == 2
    assert cursor.statements == []


@pytest.mark.parametrize("run_id", ["", "12 34", "1;DROP", "a" * 65])
def test_bad_run_id_is_rejected(
    cursor: FakeCursor, write_inputs, monkeypatch: pytest.MonkeyPatch, run_id: str
) -> None:
    monkeypatch.setenv("RUN_ID", run_id)
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 2


def test_missing_credentials_are_reported(write_inputs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT")
    assert run(write_inputs, [make_node("fct_orders", "table")], ["fct_orders"]) == 2


def test_missing_manifest_is_reported(cursor: FakeCursor, env) -> None:
    changed = env / "changed.txt"
    changed.write_text("fct_orders\n", encoding="utf-8")
    assert (
        backup_lib.run_script(backup.main, [str(changed), "--manifest", str(env / "nope.json")])
        == 2
    )


def _state_manifest(env, names: list[str]):
    import json

    path = env / "state_manifest.json"
    path.write_text(json.dumps({"nodes": {f"model.p.{n}": {} for n in names}}), encoding="utf-8")
    return path


def test_previously_deployed_but_invisible_table_stops_the_run(
    cursor: FakeCursor, write_inputs, env
) -> None:
    state = _state_manifest(env, ["fct_orders"])
    code = run(
        write_inputs,
        [make_node("fct_orders", "table")],
        ["fct_orders"],
        "--state-manifest",
        str(state),
    )
    assert code == backup_lib.EXIT_SQL
    assert cursor.writes() == []


def test_allow_missing_downgrades_to_a_warning(cursor: FakeCursor, write_inputs, env) -> None:
    state = _state_manifest(env, ["fct_orders"])
    code = run(
        write_inputs,
        [make_node("fct_orders", "table")],
        ["fct_orders"],
        "--state-manifest",
        str(state),
        "--allow-missing",
    )
    assert code == 0
    assert cursor.writes() == []


def test_model_not_in_state_is_still_treated_as_new(cursor: FakeCursor, write_inputs, env) -> None:
    state = _state_manifest(env, ["something_else"])
    code = run(
        write_inputs,
        [make_node("fct_orders", "table")],
        ["fct_orders"],
        "--state-manifest",
        str(state),
    )
    assert code == 0


def test_unreadable_state_manifest_is_a_usage_error(cursor: FakeCursor, write_inputs, env) -> None:
    code = run(
        write_inputs,
        [make_node("fct_orders", "table")],
        ["fct_orders"],
        "--state-manifest",
        str(env / "nope.json"),
    )
    assert code == backup_lib.EXIT_USAGE
