# Snowflake CI/CD – Project Instructions for Claude Code

## What this repo is
One repository for all Snowflake deployment work, built for the client (Kumar Saurabh's team).
- `terraform/` – Snowflake administration: databases, warehouses, roles, grants, BACKUP schema, CICD objects.
- `dbt/` – Data development: schemas, tables, views (dbt-core + dbt-snowflake).
- `pipelines/` – Azure DevOps YAML pipelines. Each folder deploys only when its own files change.
- `docs/implementation-guide.md` – The agreed design (Phases 1–5). Follow it. If something in it is wrong or unsafe, say so and propose a fix before changing direction.

## Environments
DEV, TEST, UAT, STAGE, PROD. Build and demo on DEV and TEST first.
Branch mapping (current): `dev` → DEV, `test` → TEST, `master` → STAGE/PROD with approval gates.
Naming: `ANALYTICS_<ENV>`, `TRANSFORM_WH_<ENV>`, `DBT_DEPLOYER_<ENV>`, `CICD.STATE.DBT_STATE`, `CICD.AUDIT.BACKUP_LOG`.

## Non-negotiable rules
1. **No secrets in code, ever.** Credentials come from environment variables, Azure DevOps variable groups (Key Vault-linked) and Secure Files. Never write keys, passwords, account URLs or tokens into any file, test or example.
2. **Never run against the client's Snowflake or Azure from this machine.** Do not run `terraform apply`, `dbt run`, `dbt build`, `snow`, or `az` commands that change remote state. Local checks only: `terraform fmt/validate/plan` against a sandbox, `dbt parse/compile`, linters, unit tests.
3. **Least privilege.** Each pipeline uses its own role. Never use ACCOUNTADMIN in code. Flag any grant broader than needed.
4. **Idempotent and re-runnable.** Every script and SQL statement must be safe to run twice (`IF NOT EXISTS`, `CREATE OR REPLACE` only where data loss is impossible).
5. **Safe SQL.** Quote Snowflake identifiers; never build SQL from untrusted strings without validation. Values go through bind parameters.
6. **Fail loudly.** Bash scripts start with `set -euo pipefail`. Python exits non-zero on any failure with a clear message. The dbt manifest is uploaded only after a successful build.
7. **Pin versions.** Python packages in `requirements.txt` with exact versions; Terraform provider and Terraform version pinned; pipeline tasks pinned to major versions.

## Code standards
- **Python:** 3.11, type hints, `logging` (not print) with run ID in every message, functions small and testable, `pytest` tests with the Snowflake connector mocked. Format with `ruff format`, lint with `ruff check`.
- **Bash:** passes `shellcheck`. Functions for repeated logic. Clear usage message.
- **SQL / dbt:** passes `sqlfluff` (dialect snowflake, templater dbt). Every model has a description and at least `not_null`/`unique` tests on its key in `schema.yml`.
- **Terraform:** `terraform fmt`, `tflint`, and `checkov` clean. Reusable logic in `terraform/modules/`, environments in `terraform/envs/<env>/`. Every variable has a type and description. No hard-coded environment names inside modules.
- **Azure Pipelines:** shared steps live in `pipelines/templates/` and are reused, not copied. Every step has a `displayName`. Secrets passed via `env:` only.

## How to work
- For any non-trivial task: plan first, list the files you will create or change, then wait for approval.
- Work one phase at a time. After each phase: run all local checks, list what was verified and what still needs real Snowflake/Azure access to test.
- Small, focused commits with conventional messages (`feat(dbt): ...`, `fix(terraform): ...`).
- Update `docs/implementation-guide.md` and `README.md` when behavior changes.
- When unsure about a client decision (retention period, branching model, role names), ask; do not guess. Keep a running list in `docs/open-questions.md`.

## Local checks (run before saying a task is done)
```bash
pre-commit run --all-files
pytest -q
cd dbt && dbt parse && cd ..
cd terraform/envs/dev && terraform init -backend=false && terraform validate && cd ../../..
```
