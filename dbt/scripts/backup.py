#!/usr/bin/env python3
"""Zero-copy clone every changed table/incremental model that already exists, before dbt build.

Usage: python scripts/backup.py changed_models.txt [--manifest target/manifest.json]
                                [--state-manifest state/manifest.json] [--allow-missing] [--dry-run]

A table that Snowflake does not show is normally a new model. INFORMATION_SCHEMA cannot tell
"does not exist" from "exists but this role cannot see it", so with --state-manifest (the manifest
of the last good deploy) a model that was deployed before and now looks missing stops the run
(exit 4) instead of being replaced without a backup. --allow-missing turns that into a warning.

Environment: RUN_ID, TARGET_ENV (or DBT_TARGET), optional PR_NUMBER and COMMIT_SHA, plus the
SNOWFLAKE_* variables described in backup_lib.py.

Exit codes: 0 ok, 2 bad input/config, 3 cannot connect, 4 SQL failure.
Safe to re-run with the same RUN_ID: clones use IF NOT EXISTS and the log insert skips
(run_id, source_table) pairs that are already logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import backup_lib as lib

BACKED_UP_MATERIALIZATIONS = ("table", "incremental")


@dataclass(frozen=True)
class RunContext:
    run_id: str
    pr_number: str | None
    commit_sha: str | None
    environment: str


@dataclass(frozen=True)
class Candidate:
    database: str
    schema: str
    table: str
    unique_id: str = ""

    @property
    def source(self) -> str:
        return f"{self.database}.{self.schema}.{self.table}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("changed_models", type=Path, help="file with one model name per line")
    parser.add_argument(
        "--manifest", type=Path, default=Path("target/manifest.json"), help="dbt manifest.json"
    )
    parser.add_argument(
        "--state-manifest",
        type=Path,
        default=None,
        help="manifest.json of the last good deploy; models in it must have a visible table",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="only warn when a previously deployed table cannot be found",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check what would be backed up (read-only queries); create and log nothing",
    )
    return parser.parse_args(argv)


def build_context() -> RunContext:
    environment = os.environ.get("TARGET_ENV") or os.environ.get("DBT_TARGET")
    return RunContext(
        run_id=lib.validate_run_id(os.environ.get("RUN_ID")),
        pr_number=lib.optional_pr_number(os.environ.get("PR_NUMBER")),
        commit_sha=lib.optional_commit_sha(os.environ.get("COMMIT_SHA")),
        environment=lib.validate_environment(environment),
    )


def read_changed_models(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise lib.ScriptError(
            f"cannot read changed-models file {path}: {exc}", lib.EXIT_USAGE
        ) from exc
    return {line.strip() for line in lines if line.strip()}


def read_deployed_ids(path: Path | None) -> set[str]:
    """unique_ids of the nodes in the last good deploy's manifest (empty without one)."""
    if path is None:
        return set()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return set(manifest["nodes"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise lib.ScriptError(f"cannot read state manifest {path}: {exc}", lib.EXIT_USAGE) from exc


def read_manifest_nodes(path: Path) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return [{**node, "unique_id": uid} for uid, node in manifest["nodes"].items()]
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise lib.ScriptError(f"cannot read dbt manifest {path}: {exc}", lib.EXIT_USAGE) from exc


def select_candidates(nodes: list[dict[str, Any]], changed: set[str]) -> list[Candidate]:
    """Changed models that are materialized as a table or incremental, in manifest order."""
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for node in nodes:
        if node.get("resource_type") != "model" or node.get("name") not in changed:
            continue
        seen.add(node["name"])
        materialized = (node.get("config") or {}).get("materialized")
        if materialized not in BACKED_UP_MATERIALIZATIONS:
            lib.logger.info(
                "skip %s: materialized as %s, nothing to back up", node["name"], materialized
            )
            continue
        candidates.append(
            Candidate(
                database=lib.validate_identifier("database", node["database"]),
                schema=lib.validate_identifier("schema", node["schema"]),
                table=lib.validate_identifier("table", node.get("alias") or node["name"]),
                unique_id=node.get("unique_id", ""),
            )
        )
    for name in sorted(changed - seen):
        lib.logger.warning("changed model %s is not in the manifest; not backed up", name)
    return candidates


def back_up_table(
    cursor: Any,
    candidate: Candidate,
    ctx: RunContext,
    dry_run: bool,
    deployed: frozenset[str] = frozenset(),
    allow_missing: bool = False,
) -> str:
    """Back up one table. Returns 'backed_up', 'would_back_up' or 'skipped_new'."""
    if not lib.table_exists(cursor, candidate.database, candidate.schema, candidate.table):
        if candidate.unique_id in deployed:
            message = (
                f"{candidate.source} was deployed before but is not visible to this role; "
                "it may be missing or owned by another role. "
                "Refusing to replace it without a backup"
            )
            if not allow_missing:
                raise lib.ScriptError(message, lib.EXIT_SQL)
            lib.logger.warning("%s (allowed by --allow-missing)", message)
        lib.logger.info("skip %s: table does not exist yet (new model)", candidate.source)
        return "skipped_new"

    backup = lib.backup_table_parts(candidate.database, candidate.table, ctx.run_id)
    backup_name = ".".join(backup)
    if dry_run:
        lib.logger.info("dry run: would clone %s -> %s", candidate.source, backup_name)
        return "would_back_up"

    lib.execute(
        cursor,
        f"CREATE TABLE IF NOT EXISTS {lib.qualified(*backup)} "
        f"CLONE {lib.qualified(candidate.database, candidate.schema, candidate.table)}",
    )
    lib.insert_log_row(
        cursor,
        run_id=ctx.run_id,
        pr_number=ctx.pr_number,
        commit_sha=ctx.commit_sha,
        environment=ctx.environment,
        source_table=candidate.source,
        backup_table=backup_name,
    )
    lib.logger.info("backed up %s -> %s", candidate.source, backup_name)
    return "backed_up"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = build_context()
    lib.setup_logging(ctx.run_id)

    changed = read_changed_models(args.changed_models)
    if not changed:
        lib.logger.info("no changed models; nothing to back up")
        return lib.EXIT_OK
    candidates = select_candidates(read_manifest_nodes(args.manifest), changed)
    if not candidates:
        lib.logger.info("no changed table/incremental models; nothing to back up")
        return lib.EXIT_OK

    deployed = frozenset(read_deployed_ids(args.state_manifest))
    counts: dict[str, int] = {}
    with lib.snowflake_cursor() as cursor:
        for candidate in candidates:
            outcome = back_up_table(
                cursor, candidate, ctx, args.dry_run, deployed, args.allow_missing
            )
            counts[outcome] = counts.get(outcome, 0) + 1
    lib.logger.info("backup finished%s: %s", " (dry run)" if args.dry_run else "", counts)
    return lib.EXIT_OK


if __name__ == "__main__":
    sys.exit(lib.run_script(main))
