# Snowflake CI/CD

One repository for all Snowflake deployment work: administration with Terraform and data development with dbt, deployed through Azure DevOps pipelines. The agreed design is in [docs/implementation-guide.md](docs/implementation-guide.md); open decisions are in [docs/open-questions.md](docs/open-questions.md).

## Layout

```
snowflake/
├── pipelines/            # Azure DevOps YAML pipelines
│   ├── templates/        # shared steps, reused by every pipeline
│   └── scripts/          # create_revert_pr.sh, notify_teams.sh (bash, shellcheck clean)
├── dbt/                  # data development (dbt-core + dbt-snowflake)
│   ├── models/           # staging (views) and marts (tables)
│   ├── seeds/            # sample CSV data for the demo
│   ├── macros/
│   ├── setup/            # one-time SQL (CICD database, state stage, backup log)
│   └── scripts/          # state fetch/upload, PR schema cleanup (Snowflake CLI);
│                         # backup, restore, cleanup_backups (Python, snowflake connector)
├── terraform/            # Snowflake administration
│   ├── modules/          # reusable modules (no hard-coded environment names)
│   └── envs/<env>/       # one folder per environment (dev, test, ...)
├── tests/                # pytest unit tests (Snowflake connector mocked)
└── docs/                 # implementation guide, open questions
```

| Folder | Owns |
| --- | --- |
| `terraform/` | Databases, warehouses, roles, grants, `BACKUP` schema, `CICD` database and stage |
| `dbt/` | Schemas, tables, views, tests and documentation |

## Environments

DEV, TEST, UAT, STAGE, PROD. Naming: `ANALYTICS_<ENV>`, `TRANSFORM_WH_<ENV>`, `DBT_DEPLOYER_<ENV>`, `CICD.STATE.DBT_STATE`, `CICD.AUDIT.BACKUP_LOG`.

| Branch | Environment | Approval |
| --- | --- | --- |
| `dev` | DEV | None |
| `test` | TEST | None |
| `master` | STAGE, PROD (later) | Manual approval gate |

The final branching model is still open (see open questions).

## Local setup

Requires Python 3.11, Terraform 1.12.2, Git, [tflint](https://github.com/terraform-linters/tflint) 0.64 and checkov (`requirements-tf.txt`, own virtual environment).

```bash
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pre-commit install
```

The Snowflake CLI (`snow`) pins its own copies of click, protobuf and others, which conflict with dbt-core, so it lives in a separate environment:

```bash
python3.11 -m venv .venv-cli
.venv-cli/bin/pip install -r requirements-cli.txt     # Windows: .venv-cli\Scripts\pip
# run as .venv-cli/bin/snow ... (pipelines: `pipx install` or a second venv)
```

Local checks, run before every commit:

```bash
pre-commit run --all-files
pytest -q
cd dbt && dbt parse && cd ..
cd terraform/envs/dev && terraform init -backend=false && terraform validate && cd ../../..
```

**Credentials never go in the repo.** Locally, export them as environment variables (or keep them in an untracked `.env`). In pipelines they come from Azure DevOps variable groups (`snowflake-dev`, `snowflake-test`, Key Vault-linked) and Secure Files. Do not run `terraform apply`, `dbt run`/`dbt build` or `snow` against the client's accounts from a developer machine.

## How pipelines trigger

Each folder deploys only when its own files change.

| Pipeline | Runs on | Path filter |
| --- | --- | --- |
| `dbt-ci.yml` | Pull request (build validation policy) | `/dbt/*` |
| `dbt-deploy.yml` | Merge to `dev` or `test` | `dbt/*` |
| `tf-plan.yml` | Pull request (build validation policy) | `/terraform/*` |
| `tf-apply.yml` | Merge to `dev` or `test` | `terraform/*` |

## dbt state-based deployment (Phase 2)

- One-time: run `dbt/setup/cicd_objects.sql` (creates `CICD.STATE.DBT_STATE`).
- Deploy: `fetch_state.sh <env>` downloads the last manifest; `dbt build --select state:modified+ --state ./state` builds only changes; `upload_state.sh <env>` saves the new manifest after a green build.
- `fetch_state.sh` exits **10** when no manifest exists yet (first run, full build). Any other non-zero code is a real failure and stops the pipeline.
- PR checks build changed models into `CI_PR_<PR id>` with `--defer`, then `drop_pr_schema.sh` removes the schema.
- Scripts read credentials from `SNOWFLAKE_*` environment variables only.

## Terraform (Phase 5)

- `terraform/modules/environment`: `ANALYTICS_<ENV>` database, `BACKUP` schema, `TRANSFORM_WH_<ENV>`, role `DBT_DEPLOYER_<ENV>` and its grants.
- `terraform/modules/cicd`: shared `CICD` database, `STATE` and `AUDIT` schemas, `DBT_STATE` stage, `BACKUP_LOG` table, and grants for the deployer roles. Only `envs/dev` owns these objects (`manage_objects = true`); `envs/test` adds grants only, so apply dev first.
- `terraform/envs/<env>`: one root per environment, own state key (`snowflake/<env>/terraform.tfstate`) in Azure Blob, Terraform and provider versions pinned exactly, lock file committed.
- Sign-in is key-pair (`SNOWFLAKE_JWT`) with two aliased providers: `SYSADMIN` for objects, `SECURITYADMIN` for roles and grants. The key reaches the provider through the `SNOWFLAKE_PRIVATE_KEY` environment variable (from a Secure File), never a Terraform variable, so it cannot end up in a saved plan. PR builds use separate read-only identities; deploys only run from branches named exactly `dev` or `test`, one run at a time (`batch: true`); a missing dbt state stops the deploy unless `allowFullBuild` is ticked.
- Pipelines: `tf-plan.yml` runs checks then `plan` on PRs and publishes the plan as text. `tf-apply.yml` plans, then applies the saved plan through the Environment `snowflake-<env>` (approvals for STAGE/PROD).
- Local checks: `bash pipelines/scripts/tf.sh checks` (fmt, validate, tflint, checkov). First-time setup and imports: [docs/azure-devops-setup.md](docs/azure-devops-setup.md) section 7; bootstrap user: `terraform/bootstrap/create_terraform_user.sql`.

## Automatic table backup (Phase 3)

- One-time setup: none by hand. Terraform creates `CICD.AUDIT.BACKUP_LOG`, the stage and the `<DB>.BACKUP` schemas (`dbt/setup/*.sql` is only for a sandbox without Terraform).
- Deploy: `dbt-deploy.yml` parses, lists changed models, then runs `dbt/scripts/backup.py changed_models.txt` **before** `dbt build`. It zero-copy clones each changed `table`/`incremental` model that already exists into `<DB>.BACKUP.<TABLE>__<RUN_ID>` and logs it. Views and brand-new tables are skipped. A backup failure stops the deploy.
- Restore: `python dbt/scripts/restore.py --run-id <id> | --pr-number <n> [--table T] [--dry-run] [--confirm]`. Without `--confirm` it only prints the plan. With it, a safety copy of the current table is taken first.
- Cleanup: `python dbt/scripts/cleanup_backups.py --days N --environment dev [--dry-run]` (or `BACKUP_RETENTION_DAYS`). There is no default; the period is an open client decision.
- Exit codes (all three scripts): `0` ok, `2` bad input or config, `3` cannot connect (3 attempts), `4` SQL failure.
- Even `--dry-run` opens a Snowflake connection, so do not run these from a developer machine against the client's account. The tests mock the connector.

## Failure handling and auto-revert (Phase 4)

- `dbt-deploy.yml` order: validate (`dbt deps`, `parse`, `compile`) -> fetch state -> back up -> `dbt build` -> upload manifest. The manifest upload is its own step with the default `succeeded()` condition, so a failed validation, backup or build never replaces the last good state.
- If `dbt build` fails, the `Revert` stage runs `pipelines/scripts/create_revert_pr.sh`. It reverts the failed commit (merge commits with `-m 1`, squash commits plainly) onto the current branch tip in `revert/<branch>-<sha7>`, pushes it and opens a PR into the deployed branch. It never merges and never sets auto-complete. Failures before the build started (install, key, validation, backup) deployed nothing, so no revert PR is opened.
- Commits that are not `Merged PR <n>: ...` (rebase merges, direct pushes) are refused with exit code 4, because the last commit alone is not the whole change. `--allow-unrecognised-commit` overrides it.
- `notify_teams.sh` posts to a Teams (Workflows) webhook held in the secret `TEAMS_WEBHOOK_URL`, with the run link and the revert PR link.
- Try the revert locally without pushing: `GIT_BOT_NAME=bot GIT_BOT_EMAIL=bot@example.invalid bash pipelines/scripts/create_revert_pr.sh --branch dev --sha <40-hex> --dry-run` (needs an `origin` remote with that branch). Exit codes: `0` ok, `1` failure, `2` bad usage, `3` revert conflict, `4` unrecognised commit.
- A revert PR restores code only. Restore damaged table data with `restore.py` (Phase 3).

Azure Repos ignores the YAML `pr:` trigger, so PR checks are configured as build validation branch policies with path filters, not in the YAML.

The dbt and Terraform pipelines are real as of Phase 5. Shared steps are in `pipelines/templates/`, and `templates/variables.yml` picks the variable group `snowflake-<branch>` (for PR pipelines, the PR target branch). Setup steps: [docs/azure-devops-setup.md](docs/azure-devops-setup.md).
