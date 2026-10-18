# Snowflake CI/CD Implementation Guide – Phases 1 to 5

Oct 17, 2026 · Ravi Kishore

## Overview

The demo goal is to show Kumar one `snowflake` repository where a dbt change deploys only modified models, with an automatic backup and auto-revert on failure, and a Terraform change runs plan on PR and apply on merge.

| Phase | Deliverable | Demo proof |
| --- | --- | --- |
| 1. Repository and triggers | `snowflake` repo with `terraform/` and `dbt/` folders, path-based pipelines | A dbt-only change triggers only the dbt pipeline |
| 2. State-based dbt deployment | Manifest stored per environment, `state:modified+` selection | Change one model; only that model and its children run |
| 3. Automatic backup | Zero-copy clone to a backup schema, logged per run | Backup table and log row visible in Snowflake |
| 4. Failure handling | Auto-created revert branch and PR on failed deployment | Break a model; revert PR appears automatically |
| 5. Terraform pipeline | Terraform project built from scratch, remote state | PR shows plan; merge creates a database and role |

**Assumptions** (to confirm with Jitender and Kumar)

- CI/CD tool: Azure DevOps Pipelines (YAML), with the repository in Azure Repos.
- Environments: Dev, Test, UAT, Stage, Prod. The demo uses Dev and Test only.
- Snowflake objects follow a naming pattern such as `ANALYTICS_DEV`, `ANALYTICS_TEST`.

**Prerequisites**

- A Snowflake trial or sandbox account for building before client access arrives.
- A service user for CI with key-pair authentication, and a deploy role per environment.
- Python 3.11, dbt-core 1.8+ with dbt-snowflake, Terraform 1.6+, Git.
- Storage for state files: a cloud bucket (S3 or Azure Blob) or a Snowflake internal stage.

## Phase 1 – Repository and Pipeline Structure

One `snowflake` repository holds both projects, and each folder has its own pipeline that runs only when files in that folder change.

**Repository layout**

```
snowflake/
├── pipelines/
│   ├── dbt-ci.yml          # PR build validation for dbt
│   ├── dbt-deploy.yml      # deploy on merge + auto-revert
│   ├── tf-plan.yml         # terraform plan on PR
│   └── tf-apply.yml        # terraform apply on merge
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml        # reads credentials from env vars
│   ├── models/
│   ├── macros/
│   └── scripts/
│       ├── fetch_state.sh
│       ├── backup.py
│       └── upload_state.sh
└── terraform/
    ├── modules/
    └── envs/
        ├── dev/
        └── test/
```

**Path-based trigger** (top of `pipelines/dbt-deploy.yml`; Terraform pipelines use `terraform/*`)

```yaml
trigger:
  branches:
    include: [dev, test]
  paths:
    include: [dbt/*]
pr: none
```

Azure Repos ignores the YAML `pr:` trigger. PR checks (`dbt-ci.yml`, `tf-plan.yml`) run through **build validation branch policies**, each with a path filter (`/dbt/*` or `/terraform/*`).

**Branch to environment mapping (demo)**

| Branch | Environment | Approval |
| --- | --- | --- |
| `dev` | DEV | None |
| `test` | TEST | None |
| `master` | STAGE, PROD (later) | Manual approval gate |

The final branching model is still open; Kumar asked for a proposal.

**Secrets per environment** (Azure DevOps variable groups, one per environment such as snowflake-dev and snowflake-test, ideally linked to Azure Key Vault; the private key goes in Secure Files)

- `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY`
- `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`

**Steps**

1. Create the repository in Azure Repos with the layout above, and add branch policies on `dev`, `test` and `master`: PR required, minimum one reviewer.
2. Create a CI service user in Snowflake with key-pair authentication and a deploy role per environment.
3. Create variable groups `snowflake-dev` and `snowflake-test`, and upload the private key to Pipelines > Library > Secure files.
4. Create the four pipelines from the YAML files, and add build validation policies with path filters for `dbt-ci` and `tf-plan`.
5. Test: change only a dbt file and confirm the Terraform pipelines do not run, then the reverse.

**Done when:** a change in one folder triggers only that folder's pipeline.

## Phase 2 – State-Based dbt Deployment

Each deployment saves its `manifest.json`, and the next run compares against it with `state:modified+`, so only changed models and their downstream models deploy.

**How it works**

1. `dbt compile` writes `target/manifest.json`, describing every model, test and config.
2. After a successful deploy, the pipeline uploads that manifest to state storage, one copy per environment.
3. On the next run, the pipeline downloads the last manifest into `./state/`.
4. `--select state:modified+ --state ./state` picks only new or changed models plus their children.
5. First run (no manifest yet): the pipeline does a full `dbt build`.

**State storage**

Use a Snowflake internal stage so no extra cloud account is needed:

```sql
CREATE DATABASE IF NOT EXISTS CICD;
CREATE SCHEMA IF NOT EXISTS CICD.STATE;
CREATE STAGE IF NOT EXISTS CICD.STATE.DBT_STATE;
-- paths: @CICD.STATE.DBT_STATE/dev/manifest.json, /test/manifest.json
```

An S3 or Azure Blob container works the same way if Kumar's team prefers it.

**profiles.yml** (no credentials in Git)

```yaml
snowflake:
  target: "{{ env_var('DBT_TARGET', 'dev') }}"
  outputs:
    dev: &defaults
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      private_key_path: "{{ env_var('SNOWFLAKE_KEY_PATH') }}"
      role: "{{ env_var('SNOWFLAKE_ROLE') }}"
      warehouse: "{{ env_var('SNOWFLAKE_WAREHOUSE') }}"
      database: "{{ env_var('SNOWFLAKE_DATABASE') }}"
      schema: ANALYTICS
      threads: 8
    test:
      <<: *defaults
    ci:
      <<: *defaults
      schema: "CI_PR_{{ env_var('PR_NUMBER', '0') }}"
```

**PR check (`dbt-ci.yml`)** – builds only changed models into a temporary PR schema and reads unchanged parents from the target environment:

```bash
cd dbt
dbt deps
./scripts/fetch_state.sh "$TARGET_ENV"        # downloads manifest to ./state
dbt build --target ci \
  --select state:modified+ \
  --defer --state ./state
# drop CI_PR_<n> schema when the PR closes
```

**Deploy on merge (`dbt-deploy.yml`)**

```bash
cd dbt
dbt deps
if ./scripts/fetch_state.sh "$DBT_TARGET"; then
  SELECT_ARGS="--select state:modified+ --state ./state"
else
  SELECT_ARGS=""                               # first run: full build
fi
dbt ls $SELECT_ARGS --resource-type model --output name > changed_models.txt
python scripts/backup.py changed_models.txt    # Phase 3
dbt build --target "$DBT_TARGET" $SELECT_ARGS
./scripts/upload_state.sh "$DBT_TARGET"       # only runs if build succeeded
```

**Azure DevOps pipeline (`pipelines/dbt-deploy.yml`, Deploy stage)**

```yaml
trigger:
  branches:
    include: [dev, test]
  paths:
    include: [dbt/*]
pr: none

pool:
  vmImage: ubuntu-latest

variables:
  - group: snowflake-${{ replace(variables['Build.SourceBranch'], 'refs/heads/', '') }}   # only dev / test are accepted (templates/variables.yml)

stages:
  - stage: Deploy
    jobs:
      - job: dbt_deploy
        steps:
          - checkout: self
          - task: UsePythonVersion@0
            inputs:
              versionSpec: '3.11'
          - task: DownloadSecureFile@1
            name: sfkey
            inputs:
              secureFile: snowflake_ci_key.p8
          - script: pip install dbt-snowflake snowflake-connector-python snowflake-cli
            displayName: Install tools
          - script: |
              set -e
              cd dbt
              dbt deps
              if ./scripts/fetch_state.sh "$DBT_TARGET"; then
                SELECT_ARGS="--select state:modified+ --state ./state"
              else
                SELECT_ARGS=""
              fi
              dbt ls $SELECT_ARGS --resource-type model --output name > changed_models.txt
              python scripts/backup.py changed_models.txt
              dbt build --target "$DBT_TARGET" $SELECT_ARGS
              ./scripts/upload_state.sh "$DBT_TARGET"
            displayName: Backup, deploy, save state
            env:
              DBT_TARGET: $(DBT_TARGET)
              SNOWFLAKE_ACCOUNT: $(SNOWFLAKE_ACCOUNT)
              SNOWFLAKE_USER: $(SNOWFLAKE_USER)
              SNOWFLAKE_KEY_PATH: $(sfkey.secureFilePath)
              SNOWFLAKE_ROLE: $(SNOWFLAKE_ROLE)
              SNOWFLAKE_WAREHOUSE: $(SNOWFLAKE_WAREHOUSE)
              SNOWFLAKE_DATABASE: $(SNOWFLAKE_DATABASE)
              RUN_ID: $(Build.BuildId)
              COMMIT_SHA: $(Build.SourceVersion)
```

`set -e` stops the script at the first failure, so the manifest is uploaded only after a successful build. In `dbt-ci.yml`, pass `PR_NUMBER: $(System.PullRequest.PullRequestId)` so each PR builds into its own `CI_PR_<n>` schema.

**fetch_state.sh** (Snowflake CLI)

```bash
#!/usr/bin/env bash
set -e
mkdir -p state
snow stage copy "@CICD.STATE.DBT_STATE/$1/manifest.json" ./state/ || exit 1
```

`upload_state.sh` runs the reverse copy from `target/manifest.json`.

**As built (differences from the snippets above)**

- `fetch_state.sh` lists the stage path first. Manifest found: exit 0. Confirmed absent: exit **10** (first run). Any other failure: exit 1, so an auth or network error never triggers an unintended full build (open-questions D). Pipelines handle the codes with a `case`, and `SELECT_ARGS` is a bash array.
- Scripts use `snow ... --temporary-connection` with key-pair flags built from `SNOWFLAKE_*` env vars (shared in `dbt/scripts/lib.sh`). Environment names (`^[a-z]+$`), PR numbers (digits) and the database name are validated before they reach a stage path or SQL.
- `dbt-ci.yml` drops `CI_PR_<id>` at the end of every run (`condition: always()`) because Azure Repos has no PR-closed trigger (open-questions M). Manual runs without a PR id fail the build step with a clear message and skip the cleanup.
- Install: `.venv` from `requirements.txt` and `.venv-cli` from `requirements-cli.txt`; `SNOW_BIN` points at the CLI (open-questions C).
- Sample project: two seeds (`raw_customers`, `raw_orders`), two staging views, one mart table (`fct_customer_orders`), tests on every key. Models set no schema in config, so `state:modified` ignores the per-target schema.
- The Phase 3 backup call is wired into `dbt-deploy.yml` (see "As built" under Phase 3).

**Done when:** changing one model deploys only that model and its children, and the run log shows the other models were skipped.

## Phase 3 – Automatic Backup Before Table Changes

Before `dbt build` runs, the pipeline zero-copy clones every changed table into a `BACKUP` schema and logs the clone against the PR, so any table can be restored in one statement.

**Rules**

- Back up only models materialized as `table` or `incremental` that already exist. Views hold no data and are recreated from code.
- Backup name: `<DB>.BACKUP.<TABLE>__<RUN_ID>`.
- Zero-copy clones cost no storage at creation; storage grows only as the source table changes.

**One-time setup**

```sql
CREATE SCHEMA IF NOT EXISTS ANALYTICS_DEV.BACKUP;
CREATE SCHEMA IF NOT EXISTS CICD.AUDIT;
CREATE TABLE IF NOT EXISTS CICD.AUDIT.BACKUP_LOG (
  run_id        STRING,
  pr_number     STRING,
  commit_sha    STRING,
  environment   STRING,
  source_table  STRING,
  backup_table  STRING,
  created_at    TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
```

**scripts/backup.py**

```python
import json, os, sys
import snowflake.connector

changed = {l.strip() for l in open(sys.argv[1]) if l.strip()}
nodes = json.load(open("target/manifest.json"))["nodes"].values()
run_id = os.environ["RUN_ID"]

conn = snowflake.connector.connect(
    account=os.environ["SNOWFLAKE_ACCOUNT"],
    user=os.environ["SNOWFLAKE_USER"],
    private_key_file=os.environ["SNOWFLAKE_KEY_PATH"],
    role=os.environ["SNOWFLAKE_ROLE"],
    warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
)
cur = conn.cursor()

for n in nodes:
    if n["resource_type"] != "model" or n["name"] not in changed:
        continue
    if n["config"]["materialized"] not in ("table", "incremental"):
        continue
    db, schema, name = n["database"], n["schema"], n["alias"] or n["name"]
    src = f"{db}.{schema}.{name}"
    exists = cur.execute(f"SHOW TABLES LIKE '{name.upper()}' IN SCHEMA {db}.{schema}").fetchall()
    if not exists:
        continue  # new table, nothing to back up
    bkp = f"{db}.BACKUP.{name}__{run_id}"
    cur.execute(f"CREATE TABLE {bkp} CLONE {src}")
    cur.execute(
        "INSERT INTO CICD.AUDIT.BACKUP_LOG "
        "(run_id, pr_number, commit_sha, environment, source_table, backup_table) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            run_id,
            os.getenv("PR_NUMBER"),
            os.getenv("COMMIT_SHA"),
            os.environ["DBT_TARGET"],
            src,
            bkp,
        ),
    )
    print(f"Backed up {src} -> {bkp}")
```

**Restore procedure**

```sql
-- 1. Find the backup for the PR or run
SELECT source_table, backup_table, created_at
FROM CICD.AUDIT.BACKUP_LOG
WHERE pr_number = '42' ORDER BY created_at DESC;

-- 2. Restore the table
CREATE OR REPLACE TABLE ANALYTICS_DEV.ANALYTICS.ORDERS
  CLONE ANALYTICS_DEV.BACKUP.ORDERS__1234;
```

**Retention:** a scheduled job drops backups older than a set number of days. The period (for example 14 days) is an open question for Kumar.

**As built (differences from the snippets above)**

- `backup.py` shares `dbt/scripts/backup_lib.py` with `restore.py` and `cleanup_backups.py`. Identifiers are validated (`^[A-Za-z0-9_]+$`), upper-cased (dbt-snowflake creates unquoted objects) and double-quoted; every value is a bind parameter. The existence check reads `<DB>.INFORMATION_SCHEMA.TABLES` instead of `SHOW TABLES LIKE` so it can use binds (open-questions A).
- Re-runnable (open-questions B): `CREATE TABLE IF NOT EXISTS ... CLONE`, and the log insert skips a `(run_id, source_table)` pair that is already logged.
- `RUN_ID` is validated (letters, digits, underscore, max 64) because it becomes part of a table name. Exit codes: 0 ok, 2 bad input/config, 3 connection failure, 4 SQL failure. Only connection errors are retried (3 attempts, 2s then 4s); authentication errors and SQL errors are not.
- `--dry-run` connects and checks existence (read-only) but creates and logs nothing.
- Pipeline: `dbt parse` runs first so `target/manifest.json` matches the code being deployed, then `dbt ls` writes `changed_models.txt`, then `backup.py` runs, then `dbt build`. `PR_NUMBER` is taken from an Azure Repos merge message (`Merged PR 123: ...`); if the message has no match it stays empty in the log.
- `BACKUP_LOG` has an extra `dropped_at` column. Cleanup sets it instead of deleting rows, so the audit trail stays. Restore only uses rows where `dropped_at IS NULL`.
- `restore.py` needs `--run-id` and/or `--pr-number`; a PR with several runs must be narrowed with `--run-id`. Without `--confirm` it is a dry run. It checks every backup exists before changing anything, takes a safety copy (`<TABLE>__RESTORE_<timestamp>`, logged, expired by cleanup) of the current table, then runs `CREATE OR REPLACE TABLE ... CLONE ... COPY GRANTS`.
- `cleanup_backups.py` has no default retention: pass `--days` or set `BACKUP_RETENTION_DAYS`. It works on one environment (`--environment` or `TARGET_ENV`) and only drops tables inside a `BACKUP` schema. A failed drop is reported at the end (exit 4) after the other backups were processed.
- Not built yet: the scheduled cleanup pipeline (waits for the retention answer, open-questions 3).

**Done when:** a deploy that changes a table creates a clone in `BACKUP`, writes a log row with the PR number, and the restore SQL brings the table back.

## Phase 4 – Failure Handling and Auto-Revert

If a deployment fails after merge, the pipeline creates a revert branch and opens a revert PR automatically, but never merges it; a person reviews and approves it.

**Pipeline order on merge**

1. Validate: `dbt deps`, `dbt parse` and `dbt compile` against the target environment.
2. Back up changed tables (Phase 3).
3. Deploy with `dbt build` (Phase 2).
4. Success: upload the new manifest.
5. Failure: do not upload the manifest, then run the revert job below.

Not uploading the manifest on failure matters: the next run still compares against the last good state, so nothing is skipped.

**Revert stage (appended to `pipelines/dbt-deploy.yml`)**

```yaml
  - stage: Revert
    dependsOn: Deploy
    condition: failed()
    jobs:
      - job: create_revert
        steps:
          - checkout: self
            fetchDepth: 0
            persistCredentials: true
          - script: |
              set -e
              az extension add --name azure-devops --only-show-errors
              SRC_BRANCH="$(Build.SourceBranchName)"
              SHA="$(Build.SourceVersion)"
              BRANCH="revert/${SRC_BRANCH}-${SHA:0:7}"
              git config user.name  "cicd-bot"
              git config user.email "cicd-bot@company.com"
              git checkout -b "$BRANCH" "$SHA"
              # merge commit has 2 parents, squash commit has 1
              if [ "$(git rev-list --parents -n 1 $SHA | wc -w)" -gt 2 ]; then
                git revert --no-edit -m 1 "$SHA"
              else
                git revert --no-edit "$SHA"
              fi
              git push origin "$BRANCH"
              az repos pr create \
                --organization "$(System.CollectionUri)" \
                --project "$(System.TeamProject)" \
                --repository "$(Build.Repository.Name)" \
                --source-branch "$BRANCH" \
                --target-branch "$SRC_BRANCH" \
                --title "Revert ${SHA:0:7}: deployment to ${SRC_BRANCH} failed" \
                --description "Auto-created after failed run $(Build.BuildId). Review before merging. Backups for this run are logged in CICD.AUDIT.BACKUP_LOG."
            displayName: Create revert branch and PR
            env:
              AZURE_DEVOPS_EXT_PAT: $(System.AccessToken)
```

The project's Build Service account needs **Contribute**, **Create branch** and **Contribute to pull requests** permissions on the repository. The branch policy on the target branch keeps the revert PR from merging without a reviewer's approval.

**Code revert vs data restore**

- The revert PR restores the code. Merging it triggers a normal deploy of the previous version.
- If a table's data was damaged, restore it from the backup clone (Phase 3 restore SQL).
- Notify the team on failure (Teams or email step) with the run link and revert PR link.

**As built (differences from the snippets above)**

- `dbt-deploy.yml` runs separate steps: validate (`dbt deps`, `parse`, `compile`) -> fetch state and back up -> `dbt build` -> upload manifest. The shared Snowflake `env:` block lives in `pipelines/templates/dbt-script-step.yml`. The upload step has the default `succeeded()` condition, and `tests/test_notify_and_pipeline.py` checks that it is the last step and that nothing before it uses `always()` or `failed()`.
- The Revert stage keeps `condition: failed()`, but the revert step runs only when the build started and did not finish. Two step markers (`mark_build`, `mark_done`) carry this across stages. Failures before the build (open-questions E) deployed nothing and do not open a PR; the Teams message says so.
- The logic is in `pipelines/scripts/create_revert_pr.sh`, not inline. It reverts onto the current tip of the deployed branch (not onto the failed commit), so later commits are respected and a conflict is found in the script (exit 3, no branch pushed) instead of inside the PR.
- Merge commits use `-m 1`; single-parent commits are reverted plainly. A subject that is not `Merged PR <n>: ...` is refused (exit 4) unless `--allow-unrecognised-commit` is passed, because a rebase merge adds several commits and one revert would not undo them (open-questions R2).
- Re-runnable: an existing remote `revert/...` branch is reused, and an active PR for it is reused instead of creating a second one. The script never merges, never sets auto-complete and never force-pushes.
- The bot identity comes from `GIT_BOT_NAME` / `GIT_BOT_EMAIL` (open-questions G). The token reaches git through `GIT_CONFIG_*` environment variables and `az` through `AZURE_DEVOPS_EXT_PAT`, never through arguments or `.git/config` (`persistCredentials: false`).
- `--dry-run` does every local step including the revert, then restores the checkout; it pushes nothing and calls no `az`.
- `pipelines/scripts/notify_teams.sh` sends an Adaptive Card to a Teams Workflows webhook from the secret `TEAMS_WEBHOOK_URL`. The URL is given to `curl` through stdin config, so it is not in the process list or logs. A missing webhook gives a warning, a failed POST fails the step. The notify step runs with `condition: always()` so it still reports when the revert step itself fails.

**Done when:** a deliberately broken model fails the deploy, a `revert/...` branch and PR appear, and the PR is left unmerged.

## Phase 5 – Terraform Pipeline (Built From Scratch)

A new Terraform project manages Snowflake administration objects per environment, with `terraform plan` shown on every PR and `terraform apply` on merge.

**What Terraform owns vs dbt**

| Terraform (admin) | dbt (development) |
| --- | --- |
| Databases per environment | Tables, views, incremental models |
| Warehouses | Model-level schemas created at run time |
| Roles and grants | Tests and documentation |
| `BACKUP` schema, `CICD` database and stage | Tags in `meta` (later phase) |

**Provider and backend** (`terraform/envs/dev/providers.tf`)

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 2.0"   # pin to the current stable release from the registry
    }
  }
  backend "azurerm" {      # Azure Blob; blob leases provide state locking
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "stsnowflaketfstate"
    container_name       = "tfstate"
    key                  = "snowflake/dev/terraform.tfstate"
  }
}

provider "snowflake" {
  organization_name = var.organization_name
  account_name      = var.account_name
  user              = var.tf_user
  role              = "SYSADMIN"
  authenticator     = "SNOWFLAKE_JWT"
  # private_key comes from the SNOWFLAKE_PRIVATE_KEY environment variable (Secure File), not a variable
}
```

**Reusable module** (`terraform/modules/environment/main.tf`)

```hcl
resource "snowflake_database" "analytics" {
  name = "ANALYTICS_${upper(var.env)}"
}

resource "snowflake_schema" "backup" {
  database = snowflake_database.analytics.name
  name     = "BACKUP"
}

resource "snowflake_warehouse" "transform" {
  name           = "TRANSFORM_WH_${upper(var.env)}"
  warehouse_size = var.warehouse_size
  auto_suspend   = 60
  auto_resume    = true
}

resource "snowflake_account_role" "deployer" {
  name = "DBT_DEPLOYER_${upper(var.env)}"
}

resource "snowflake_grant_privileges_to_account_role" "db_usage" {
  account_role_name = snowflake_account_role.deployer.name
  privileges        = ["USAGE", "CREATE SCHEMA"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.analytics.name
  }
}
```

Each environment folder calls the module: `module "env" { source = "../../modules/environment"  env = "dev"  warehouse_size = "XSMALL" }`.

**Pipelines**

- `tf-plan.yml` runs as a build validation policy on PRs touching `/terraform/*`: `terraform fmt -check`, `terraform init`, `terraform validate`, `terraform plan -out=tfplan`. The plan output is published as a pipeline artifact and linked on the PR, so reviewers see it before approving.
- `tf-apply.yml` (trigger: merge to `dev` or `test`, path `terraform/*`): `terraform init`, then `terraform apply` for Dev and Test. Stage and Prod deploy through an Azure DevOps **Environment** with an approval check.
- Install Terraform with the `TerraformInstaller@1` task or a pinned download, and authenticate the azurerm backend with an Azure Resource Manager service connection.

**Access needed from Kumar's team**

- A Terraform service user with key-pair auth. Object creation runs as `SYSADMIN`; role and grant management needs `SECURITYADMIN` or a custom admin role. Least-privilege setup to be agreed with Kumar.
- An Azure Storage account and container for Terraform state, a service connection to it, and the organization and account names.

**As built (differences from the snippets above)**

- **Layout:** `modules/environment` (database, `BACKUP` schema, warehouse, deployer role, grants) and `modules/cicd` (`CICD` database, `STATE`/`AUDIT` schemas, `DBT_STATE` stage, `BACKUP_LOG` table, grants), called by `envs/dev` and `envs/test`. Versions are in `versions.tf`: Terraform `1.12.2` and snowflake provider `2.22.0`, both exact; `.terraform.lock.hcl` is committed with hashes for Linux, Windows and macOS (arm64). Modules allow `>= 2.22.0, < 3.0.0`.
- **Two provider aliases** instead of one `SYSADMIN` provider (open-questions F): `objects` (default `SYSADMIN`) and `security` (default `SECURITYADMIN`). Both roles are variables and refuse `ACCOUNTADMIN`. The final role choice is still open (question 5).
- **Shared `CICD`:** all environments use one database, so only one state may own it. `envs/dev` sets `manage_objects = true`; `envs/test` sets `false` and only adds grants for the test deployer role. Dev must be applied first. A separate `envs/shared` root is the cleaner end state once PROD exists (open-questions W).
- **Backend:** only `key` (`snowflake/<env>/terraform.tfstate`) is in code. Storage account, resource group and container come from `-backend-config` (variable group), and sign-in uses Azure AD with a workload-identity-federation service connection (OIDC, no stored secret).
- **The saved plan contains the private key.** Terraform stores variable values in `tfplan`, sensitive ones too. So `tf-plan.yml` publishes only `terraform show -no-color` text and keeps the binary on the agent. `tf-apply.yml` passes the binary between its own two stages as a pipeline artifact (needed to apply exactly what was planned); keep that artifact's retention short and the pipeline's readers few. Risk accepted for now (open-questions X).
- **Safety:** `prevent_destroy` on the analytics database, `CICD`, the stage (manifests) and `BACKUP_LOG` (audit trail). `terraform destroy` fails on them by design.
- **`snowflake_table` is a provider preview feature** (`snowflake_table_resource`, enabled in `providers.tf`). It may change without a major version. `BACKUP_LOG` uses explicit types (`VARCHAR(16777216)`, `TIMESTAMP_LTZ(9)`) to avoid a permanent diff against what Snowflake reports.
- **Pipelines:** logic is in `pipelines/scripts/tf.sh` (`checks`, `init`, `plan`, `apply`) and `install_tf_tools.sh` (Terraform and tflint downloaded with pinned SHA256, checkov from `requirements-tf.txt` in its own venv). `tf-plan.yml`: Validate stage (no credentials) then Plan stage. `tf-apply.yml`: Plan stage, then Apply as a `deployment` job on Environment `snowflake-<env>`; approvals are configured on the Environment. `apply` only ever runs a saved plan, with no `-auto-approve`. Variables reach Terraform as `TF_VAR_*`, never `-var` flags. No `master` trigger yet (open-questions J, question 4).
- **SQL that moved to Terraform:** everything in `dbt/setup/cicd_objects.sql` and `backup_objects.sql`, plus the grants listed in their comments (open-questions Q). **SQL that remains:** `terraform/bootstrap/create_terraform_user.sql` (the Terraform user and its public key) and a one-time `terraform import` of objects that already exist (docs/azure-devops-setup.md, section 7). Objects created at run time stay outside Terraform: `BACKUP` clones, `CI_PR_<n>` schemas and the dbt schemas.
- **Not built:** a narrower CI role for PR checks (open-questions N) and a separate restore/cleanup role (Q). Both are listed there.
- **Local checks run:** `fmt`, `validate` (both envs, `-backend=false`), `tflint` (all folders), `checkov` (see below), `pytest`. Checkov 3.3.29 has almost no Snowflake policies, so it reports nothing to check; it stays in the pipeline for when policies or other providers appear. Not tested (needs real access): `plan`/`apply`, the azurerm backend and OIDC sign-in, Environment approvals, and the import commands.

**Done when:** a PR adding a warehouse shows the plan in its build validation run, and merging it creates the warehouse in Dev.

## Demo Script for Kumar

The demo runs in about 20 minutes and proves each phase with a live change in the Dev environment.

1. **Repository (2 min):** show the `snowflake` repo with `terraform/` and `dbt/`, and the four Azure DevOps pipelines.
2. **Path trigger (2 min):** push a dbt-only change; show that only the dbt pipeline ran.
3. **State-based deploy (5 min):** change one model's SQL and merge. Show the run log selecting only that model and its children, and the new manifest in `@CICD.STATE.DBT_STATE/dev/`.
4. **Backup (3 min):** show the clone in `ANALYTICS_DEV.BACKUP` and the row in `CICD.AUDIT.BACKUP_LOG` with the PR number. Run the restore SQL.
5. **Failure and revert (4 min):** merge a model with a deliberate SQL error. Show the failed run, the auto-created `revert/...` branch and the unmerged revert PR. Show that the manifest was not updated.
6. **Terraform (4 min):** open a PR adding a warehouse; show the plan output from its build validation run. Merge it and show the warehouse in Snowflake.

**Pre-demo checklist**

- [ ] Dev and Test databases, stage and audit table exist
- [ ] CI secrets set for each environment
- [ ] One clean full deploy done so a baseline manifest exists
- [ ] Demo branches prepared: model change, broken model, new warehouse
- [ ] Dry run of the full script completed the day before

**Open questions for Kumar and Jitender**

- Is the repository hosted in Azure Repos (assumed here) or another Git host?
- Where to store dbt manifest and Terraform state: Snowflake stage, S3 or Azure Blob?
- Backup retention period, for example 14 days?
- Final branching model for the 5 environments (our proposal to follow).
- Which roles the Terraform service user may use (SYSADMIN, SECURITYADMIN or a custom role).
