# Open Questions

Running list of client decisions and design issues. Do not guess answers; record them here and ask.
Status: `open` until answered, then move to the Decided section with the date and who decided.

## Client decisions (Kumar Saurabh / Jitender)

| # | Question | Needed for | Status |
| --- | --- | --- | --- |
| 1 | Is the repository hosted in Azure Repos (assumed) or another Git host? | Phase 1 pipelines, branch policies, revert PR step | open |
| 2 | Where to store the dbt manifest and Terraform state: Snowflake stage, S3 or Azure Blob? | Phase 2, Phase 5 | open |
| 3 | Backup retention period (for example 14 days)? | Phase 3 cleanup job | open |
| 4 | Final branching model for the 5 environments (our proposal to follow; Kumar asked for one) | Phase 1, all pipelines | open |
| 5 | Which roles may the Terraform service user use (SYSADMIN, SECURITYADMIN or a custom role)? | Phase 5 | open |
| 6 | Final role names per environment (`DBT_DEPLOYER_<ENV>` is the working name) | Phase 1, Phase 5 | open |
| 7 | Confirm Azure DevOps Pipelines (YAML) as the CI/CD tool and the Dev/Test-only scope for the demo | All phases | open |

## Design issues found in the guide (to fix when the phase is built)

| # | Issue | Phase | Proposed fix |
| --- | --- | --- | --- |
| A | `backup.py` builds SQL with f-strings (`SHOW TABLES LIKE`, `CREATE TABLE ... CLONE`); breaks the safe-SQL rule | 3 | **Done in Phase 3.** Names validated and quoted, `INFORMATION_SCHEMA` lookup and log insert use bind parameters |
| B | `CREATE TABLE <backup> CLONE` fails on a re-run with the same run ID; not re-runnable | 3 | **Done in Phase 3.** `CREATE TABLE IF NOT EXISTS` plus a log insert that skips already-logged pairs |
| O | Seeds are tables that `dbt build` reloads, but Phase 3 backs up only table/incremental models (as specified) | 3 | Decide whether changed seeds should be backed up too (needs `dbt ls --resource-type seed` and manifest nodes of type `seed`) |
| P | `PR_NUMBER` is not known on merge builds (`pr: none`); it is parsed from the `Merged PR 123:` commit message | 3 | Confirm this matches the client's merge strategy (squash/rebase messages differ); otherwise look up the PR with `az repos pr list` |
| Q | The deployer role needs `USAGE, CREATE TABLE` on `<DB>.BACKUP` and `SELECT, INSERT, UPDATE` on `CICD.AUDIT.BACKUP_LOG` | 3, 5 | **Done in Phase 5** (grants in the Terraform modules). Still open: a separate, narrower role for `restore.py` and cleanup |
| R | `restore.py` uses `CREATE OR REPLACE TABLE ... CLONE ... COPY GRANTS`; not yet run against real Snowflake | 3 | Verify the syntax and grant behaviour on DEV before relying on it |
| C | Pipeline installs tools with unpinned `pip install ... snowflake-cli` in one environment; `snowflake-cli` conflicts with dbt-core (click, protobuf pins) | 2 | Install `requirements.txt` for dbt and `requirements-cli.txt` in a separate venv or via pipx; both pinned |
| D | `fetch_state.sh` treats any `snow stage copy` failure as "first run" and falls back to a full build, so an auth or network error would trigger an unintended full deploy | 2 | **Done in Phase 2.** The script lists the stage path first: found = 0, confirmed empty = exit 10 (first run), anything else = exit 1 and the job fails |
| E | Revert stage uses `condition: failed()`, which also fires on infrastructure failures before the build | 4 | **Done in Phase 4.** The revert step runs only if the build started and did not finish (step markers); notification still fires on every failure |
| F | Provider role `SYSADMIN` cannot manage roles and grants | 5 | **Done in Phase 5.** Two provider aliases: `SYSADMIN` for objects, `SECURITYADMIN` for roles and grants; both are variables and refuse `ACCOUNTADMIN`. Custom roles still depend on question 5 |
| G | `cicd-bot@company.com` is hard-coded in the revert script | 4 | **Done in Phase 4.** `GIT_BOT_NAME` / `GIT_BOT_EMAIL` variables in the group |
| H | Azure Repos ignores YAML `pr:` triggers | 1 | PR checks run through build validation branch policies with path filters (already in the guide) |
| I | Group/target for PR pipelines is derived from `System.PullRequest.TargetBranch` at compile time; not confirmed to resolve in Azure Repos build validation runs | 1 | Verify with the test checklist in `docs/azure-devops-setup.md`; fallback is one CI pipeline per target branch |
| J | `master` would map to a non-existent `snowflake-master` group | 1 | Keep `master` out of triggers until the branching model (question 4) is decided |
| L | `sqlfluff` with the dbt templater opens a Snowflake connection, so `pre-commit` lint fails offline (checked locally on 2026-10-17: "dbt tried to connect to the database") | 2 | Locally lint with `sqlfluff lint dbt/models --templater jinja` (dbt builtins are stubbed). Decide whether CI should run the dbt-templater lint with real read-only credentials, or switch the hook to the jinja templater |
| M | PR schema cleanup: the guide says drop `CI_PR_<n>` when the PR closes, but Azure Repos has no PR-closed trigger for pipelines | 2 | `dbt-ci.yml` drops the schema at the end of every run (`condition: always()`). Reviewers cannot inspect it afterwards. A scheduled sweep for stale `CI_PR_*` schemas is the alternative |
| N | CI role needs `CREATE SCHEMA` on the environment database, broader than the deploy role needs | 2, 5 | **Open after Phase 5.** The deployer role still has `CREATE SCHEMA` on the database. Add a separate CI role limited to `CI_PR_*` schemas |
| K | Guide lists `SNOWFLAKE_PRIVATE_KEY` in the variable group and also uses a Secure File | 1 | Use the Secure File only; no key in the group |
| R2 | Rebase merges add several commits to the branch; reverting only the last would leave the rest deployed | 4 | The script refuses commits not named `Merged PR <n>: ...` (exit 4). Confirm the client merges with squash or no-fast-forward; if they use rebase, the PR's commit range has to be looked up with `az repos pr` |
| S | Teams classic Office 365 connector webhooks are retired; the notification uses the Workflows (Power Automate) Adaptive Card format | 4 | Confirm the client's Teams channel has a Workflows webhook; the payload may need adjusting |
| T | The revert is made onto the branch tip; if a newer commit touches the same lines the script stops with exit 3 | 4 | Revert by hand in that case; the notification says so |
| U | `az extension add --name azure-devops --version 1.0.2` is pinned from memory and was not run | 4 | Check the current stable version on the first real run and update `dbt-deploy.yml` |
| V | The revert PR needs the Build Service identity to have Contribute-to-PR and Create-branch rights but no bypass rights | 4 | Listed in `docs/azure-devops-setup.md` section 5; verify on the client's Azure DevOps |
| W | `CICD` is shared by all environments but each Terraform state must own an object once | 5 | `envs/dev` owns it, `envs/test` only grants; dev must apply first. Move `CICD` into an `envs/shared` root before PROD is added |
| X | The saved `tfplan` contained the private key (variable values are stored in plan files) and was published as an artifact | 5 | **Done (review fix C1).** The key is passed as `SNOWFLAKE_PRIVATE_KEY`, not a Terraform variable. Verify on DEV that `terraform show -json tfplan` has no key and that the provider still signs in |
| Y | `snowflake_table` is a provider preview feature | 5 | Accepted for `BACKUP_LOG`. Re-check on each provider upgrade; the fallback is to keep the table in SQL |
| Z | Import ids in `docs/azure-devops-setup.md` section 7 and the `AzureCLI@2` OIDC sign-in were written from the provider docs and not run | 5 | Verify on the first real run in DEV; read the plan for any `replace` before applying |
| AA | Terraform pinned to `1.12.2` (the version used for local checks) instead of the guide's `>= 1.6` | 5 | Confirm the client's agents may use it. Change `versions.tf`, `install_tf_tools.sh` and the checksum together; a test keeps them in step |
| AB | The Terraform service user is granted `SYSADMIN` and `SECURITYADMIN` in `terraform/bootstrap/create_terraform_user.sql` | 5 | Broader than ideal; replace with custom roles once question 5 is answered |

## Review fixes (2026-10-18) that need client action or real-environment checks

| # | Item | Needed |
| --- | --- | --- |
| AC | PR builds now use their own identities (`snowflake_pr_key.p8`, `snowflake_tf_plan_key.p8`, `SNOWFLAKE_PR_*`, `SNOWFLAKE_TF_PLAN_USER`, `TF_PLAN_*_ROLE`) | Client creates the users, the read-only roles, the secure files and the variable-group entries. The Terraform plan roles depend on question 5 |
| AD | Branch control checks on the deploy secure files and variable groups | Configured in Azure DevOps by an admin (docs/azure-devops-setup.md section 2) |
| AE | `backup.py --state-manifest`: a previously deployed table that cannot be seen stops the deploy | Verify on DEV; `--allow-missing` is the manual override |
| AF | `batch: true` and the `values: [dev, test]` branch limit are untested in a real Azure DevOps run | Verify on the first run in DEV |

## Decided

_None yet._
