"""Shared fixtures: a fake Snowflake connector so no test opens a real connection."""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "dbt" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import backup_lib  # noqa: E402
from snowflake.connector import errors as sf_errors  # noqa: E402


class FakeCursor:
    """Records every statement. Answers table-exists and log queries from test data."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.existing: set[tuple[str, str]] = set()  # (schema, table) in INFORMATION_SCHEMA
        self.log_rows: list[tuple[Any, ...]] = []  # rows returned for BACKUP_LOG selects
        self.fail_on: str | None = None  # substring of a statement that raises a SQL error
        self._result: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> "FakeCursor":
        self.statements.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise sf_errors.ProgrammingError("boom")
        if "INFORMATION_SCHEMA.TABLES" in sql:
            assert params is not None
            self._result = [(1,)] if (params[0], params[1]) in self.existing else []
        elif sql.startswith("SELECT run_id"):
            self._result = list(self.log_rows)
        else:
            self._result = []
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)

    def close(self) -> None:
        self.closed = True

    def sql(self, prefix: str) -> list[tuple[str, tuple[Any, ...] | None]]:
        """Statements that start with prefix (e.g. 'CREATE TABLE')."""
        return [s for s in self.statements if s[0].startswith(prefix)]

    def writes(self) -> list[tuple[str, tuple[Any, ...] | None]]:
        verbs = ("CREATE", "INSERT", "UPDATE", "DROP", "ALTER")
        return [s for s in self.statements if s[0].startswith(verbs)]


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def cursor(monkeypatch: pytest.MonkeyPatch) -> FakeCursor:
    """Patch snowflake.connector.connect to return a connection around one FakeCursor."""
    fake = FakeCursor()
    monkeypatch.setattr(
        backup_lib.snowflake.connector, "connect", lambda **_kwargs: FakeConnection(fake)
    )
    return fake


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Retries must not slow the tests down; the list records the requested delays."""
    delays: list[float] = []
    monkeypatch.setattr(backup_lib.time, "sleep", delays.append)
    return delays


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A complete, fake environment. Returns the directory for test files."""
    key = tmp_path / "key.p8"
    key.write_text("not-a-real-key", encoding="utf-8")
    for name in (
        "PR_NUMBER",
        "COMMIT_SHA",
        "PRIVATE_KEY_PASSPHRASE",
        "BACKUP_RETENTION_DAYS",
        "DBT_TARGET",
    ):
        monkeypatch.delenv(name, raising=False)
    values = {
        "SNOWFLAKE_ACCOUNT": "acct",
        "SNOWFLAKE_USER": "user",
        "SNOWFLAKE_KEY_PATH": str(key),
        "SNOWFLAKE_ROLE": "role",
        "SNOWFLAKE_WAREHOUSE": "wh",
        "RUN_ID": "77",
        "TARGET_ENV": "dev",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def make_node(name: str, materialized: str, alias: str | None = None) -> dict[str, Any]:
    return {
        "resource_type": "model",
        "name": name,
        "alias": alias or name,
        "database": "analytics_dev",
        "schema": "analytics",
        "config": {"materialized": materialized},
    }


@pytest.fixture
def write_inputs(env: Path) -> Callable[[list[dict[str, Any]], list[str]], tuple[Path, Path]]:
    """Write manifest.json and changed_models.txt; returns (changed_file, manifest_file)."""

    def _write(nodes: list[dict[str, Any]], changed: list[str]) -> tuple[Path, Path]:
        manifest = env / "manifest.json"
        manifest.write_text(
            json.dumps({"nodes": {f"model.p.{n['name']}": n for n in nodes}}), encoding="utf-8"
        )
        changed_file = env / "changed_models.txt"
        changed_file.write_text("\n".join(changed) + "\n", encoding="utf-8")
        return changed_file, manifest

    return _write
