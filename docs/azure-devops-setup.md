# Azure DevOps Setup (Phase 1)

Step-by-step setup for the four pipelines. The dbt and Terraform pipelines are real (Phases 2 to 5). Do this in the Azure DevOps UI; sections 1 to 6 need no Snowflake access, section 7 covers what Terraform needs.

Assumes the repository is in Azure Repos (open question 1) and the branches `dev`, `test` and `master` exist.

## 1. Create the variable groups

Pipelines > Library > **+ Variable group**. Create one per environment: `snowflake-dev` and `snowflake-test`.

The group name must be exactly `snowflake-<branch>`. The pipelines build it from the branch name, and a missing group fails the run before any step starts.

| Variable | Secret | Notes |
| --- | --- | --- |
| `SNOWFLAKE_ACCOUNT` | no | Account identifier |
| `SNOWFLAKE_USER` | no | CI service user |
| `SNOWFLAKE_ROLE` | no | `DBT_DEPLOYER_<ENV>` (working name, open question 6) |
| `SNOWFLAKE_WAREHOUSE` | no | `TRANSFORM_WH_<ENV>` |
| `SNOWFLAKE_DATABASE` | no | `ANALYTICS_<ENV>` |
| `GIT_BOT_NAME` | no | Author name on revert commits (for example `cicd-bot`) |
| `GIT_BOT_EMAIL` | no | Author email on revert commits (a mailbox the client owns) |
| `TF_DEPLOYER_USERS` | no | JSON list of users that get the deployer role, for example `["SVC_DBT_DEV"]`; `[]` for none (Terraform, Phase 5) |
| `TEAMS_WEBHOOK_URL` | **yes** | Teams Workflows webhook for failure notifications; link it from Key Vault |

- The Terraform pipelines also read these (same group per environment, or a shared group added to both): `SNOWFLAKE_ORGANIZATION_NAME`, `SNOWFLAKE_ACCOUNT_NAME`, `SNOWFLAKE_TF_USER` (for example `TERRAFORM_SVC`), `TFSTATE_RESOURCE_GROUP`, `TFSTATE_STORAGE_ACCOUNT`, `TFSTATE_CONTAINER` and `AZURE_SERVICE_CONNECTION` (name of the service connection from section 7). None is secret.
- To test triggers before real values exist, create the groups with one dummy variable (for example `PLACEHOLDER = x`).
- Prefer **Link secrets from an Azure Key Vault** for real values (needs an Azure service connection).
- Do not add the private key to the group. It lives in a Secure File (next section).
- `DBT_TARGET` is not stored in the group. The pipelines derive it from the branch (`TARGET_ENV`).
- For each group, open **Pipeline permissions** and authorize the four pipelines after they are created (section 3), or use **+** to grant access to all pipelines.

## 2. Upload the private key (Secure File)

Pipelines > Library > **Secure files** > **+ Secure file**. Upload the CI user's private key as `snowflake_ci_key.p8`.

- Open the file, then **Pipeline permissions** and authorize the pipelines.
- Never commit the key or paste it in a variable.
- The dbt pipelines download it and pass its path to scripts as `SNOWFLAKE_KEY_PATH`.
- Terraform uses its own user and key. Upload that key as `snowflake_tf_key.p8` the same way (key pair and user: `terraform/bootstrap/create_terraform_user.sql`). The pipelines hand it to the Snowflake provider through the `SNOWFLAKE_PRIVATE_KEY` environment variable. It is deliberately not a Terraform variable: variable values are written into the saved plan, which is published as an artifact.
- **PR builds use their own identity** (they run code from the PR branch before review). Upload `snowflake_pr_key.p8` (dbt CI) and `snowflake_tf_plan_key.p8` (Terraform plan) and authorize only the PR pipelines for them. Add to the variable group: `SNOWFLAKE_PR_USER`, `SNOWFLAKE_PR_ROLE` (may read the target database and create/drop `CI_PR_*` schemas, nothing else), `SNOWFLAKE_TF_PLAN_USER`, `TF_PLAN_OBJECTS_ROLE`, `TF_PLAN_SECURITY_ROLE` (read-only roles). If one is missing the run fails; it never falls back to the deploy identity.
- On `snowflake_ci_key.p8` and `snowflake_tf_key.p8` (deploy/apply), set **Security > Approvals and checks > Branch control** to `refs/heads/dev,refs/heads/test` (and `master` later), so a PR or feature-branch build cannot download them.
- Only branches named exactly `dev` or `test` can deploy. A run from any other branch (for example `feature/dev`) fails at compile time (`templates/variables.yml`).
- `snowflake-dbt-deploy` refuses a full build when no dbt state exists. For the very first deploy of an environment, run it manually with **allowFullBuild** ticked.

## 3. Create the four pipelines

Pipelines > **New pipeline** > **Azure Repos Git** > select the repository > **Existing Azure Pipelines YAML file**. Pick the branch `dev`, choose the path, then **Save** (not Run). Rename each pipeline after saving (the three dots menu > **Rename/move**).

| Pipeline name | YAML path | Runs when |
| --- | --- | --- |
| `snowflake-dbt-ci` | `/pipelines/dbt-ci.yml` | PR into `dev`/`test` touching `dbt/` (policy) |
| `snowflake-dbt-deploy` | `/pipelines/dbt-deploy.yml` | Push to `dev`/`test` touching `dbt/*` |
| `snowflake-tf-plan` | `/pipelines/tf-plan.yml` | PR into `dev`/`test` touching `terraform/` (policy) |
| `snowflake-tf-apply` | `/pipelines/tf-apply.yml` | Push to `dev`/`test` touching `terraform/*` |

On the first manual run of a pipeline, Azure DevOps asks to **Permit** access to the variable groups. Approve it.

The YAML `trigger:` is read from the branch being pushed, so the pipeline files must exist on `dev` and `test` before pushes there trigger anything.

## 4. Branch policies and build validation

Azure Repos ignores the YAML `pr:` trigger, so PR checks are branch policies.

Repos > Branches > `...` next to the branch > **Branch policies**. Do this for `dev`, `test` and `master`.

1. **Require a minimum number of reviewers**: 1. Leave "Allow requestors to approve their own changes" off.
2. For `dev` and `test`, **Build validation** > **+**, once per CI pipeline:

| Setting | dbt CI | Terraform plan |
| --- | --- | --- |
| Build pipeline | `snowflake-dbt-ci` | `snowflake-tf-plan` |
| Path filter | `/dbt/*` | `/terraform/*` |
| Trigger | Automatic | Automatic |
| Policy requirement | Required | Required |
| Build expiration | Immediately when the branch is updated | Immediately when the branch is updated |
| Display name | `dbt CI` | `Terraform plan` |

Path filters start with `/` here. The YAML `paths:` filters do not.

`master` gets the reviewer policy only for now. STAGE and PROD gates come later (branching model is open question 4).

## 5. Build Service permissions (Phase 4 revert stage)

The `Revert` stage of `snowflake-dbt-deploy` pushes a `revert/<branch>-<sha7>` branch and opens a pull request with `System.AccessToken`. The identity behind that token needs these rights, and nothing more.

1. Find out which identity runs the pipeline: Project settings > Pipelines > Settings > **Limit job authorization scope to current project for non-release pipelines**.
   - On (recommended): `<Project name> Build Service (<Organization>)`.
   - Off: `Project Collection Build Service (<Organization>)`. Avoid this; it can reach every project.
2. Project settings > Repositories > the repository > **Security** > select that identity and set to **Allow**:

| Permission | Why |
| --- | --- |
| Read | Checkout and `git fetch` of the deployed branch |
| Contribute | Push the revert branch |
| Create branch | The `revert/...` branch is new |
| Contribute to pull requests | `az repos pr create` and `az repos pr list` |

3. Leave these on **Not set** or **Deny** for the Build Service. Granting them would let a pipeline get around review:
   - Bypass policies when pushing
   - Bypass policies when completing pull requests
   - Force push (rewrite history and delete branches)
   - Manage permissions
4. Contribute at repository level also lets the identity push to `dev`, `test` and `master`. The branch policies block direct pushes there only for identities without the bypass rights above, so keep those off. If you want a second line of defence, add a **Deny** for Contribute on the `dev`, `test` and `master` branch security pages and an **Allow** on the `revert/` folder.
5. Branch policies from section 4 stay on for the target branch. They keep the revert PR from merging without a reviewer. The pipeline never completes or auto-completes the PR.
6. The pipeline needs `az` and the `azure-devops` extension (version pinned in `dbt-deploy.yml`, installed on the hosted agent at run time). `AZURE_DEVOPS_EXT_PAT` is set from `System.AccessToken` through `env:`.

If the push or PR step fails with `TF401027` or `TF402455`, a permission above is missing. The notification then says "Creating the revert PR failed".

## 6. Test checklist

Run these on a throwaway branch and PR, then clean up. Tick each as you confirm it.

- [ ] A PR into `dev` changing only `dbt/…` runs `dbt CI` and **not** `Terraform plan`.
- [ ] A PR into `dev` changing only `terraform/…` runs `Terraform plan` and **not** `dbt CI`.
- [ ] The `dbt CI` log shows `target dev` and the variable group `snowflake-dev` is linked (check **Variable groups** in the run summary).
- [ ] The same PR into `test` shows `target test`. This confirms the group follows the PR target branch.
- [ ] Merging the dbt-only PR into `dev` runs `snowflake-dbt-deploy` only.
- [ ] Merging the terraform-only PR into `dev` runs `snowflake-tf-apply` only.
- [ ] A docs-only change (`docs/…`) runs none of the four.
- [ ] A manual run of `dbt CI` and `Terraform plan` falls back to `dev`.

### Phase 4 checks (revert and notification)

Do these on `dev` only, with a throwaway model that you break on purpose.

- [ ] A merged PR whose model has invalid SQL fails at **Validate project** (compile). No backup is created, no `revert/...` branch appears, and Teams still gets a message saying no revert PR was created.
- [ ] A merged PR whose model compiles but fails at run time (for example a failing `unique` test) fails at **Deploy changed models**. `revert/dev-<sha7>` and an active PR into `dev` appear, the PR is **not** merged and has no auto-complete, and the Teams message links the run and the PR.
- [ ] The **Upload manifest** step shows as skipped in both failures, and `CICD.STATE.DBT_STATE` still holds the previous manifest.
- [ ] Re-running the failed run does not create a second branch or PR (the existing PR is reused).
- [ ] After merging the revert PR, the next deploy is green.
- [ ] Removing `TEAMS_WEBHOOK_URL` gives a warning, not a second failure.

If the PR runs show the wrong group or fail with "variable group not found", the PR target branch was not available when the YAML was compiled. Fall back to creating one `dbt-ci` and `tf-plan` pipeline per target branch and set the branch in each.

## 7. Terraform (Phase 5)

Do these once, in this order, before the first Terraform run.

1. **Bootstrap user (Snowflake admin).** Run `terraform/bootstrap/create_terraform_user.sql` with the public key. This is the only SQL Terraform cannot replace.
2. **State storage (Azure).** Create a storage account and a private container (for example `tfstate`) with blob versioning on. Blob leases lock the state. Record the resource group, account and container in the variable group (`TFSTATE_*`).
3. **Service connection.** Project settings > Service connections > Azure Resource Manager > **Workload identity federation** (no stored secret). Give its identity **Storage Blob Data Contributor** on the container only. Put its name in `AZURE_SERVICE_CONNECTION`.
4. **Environments.** Pipelines > Environments: create `snowflake-dev` and `snowflake-test` with no checks. When STAGE and PROD are added later, create `snowflake-stage` and `snowflake-prod` with **Approvals** (a required approver other than the person who merged). Approvals live on the Environment, not in the YAML.
5. **Permissions.** Authorize `snowflake-tf-plan` and `snowflake-tf-apply` for the variable groups, the Secure File and the service connection.
6. **Existing objects.** If `CICD`, its schemas, stage or table (or the `ANALYTICS_<ENV>` databases) were already created with the old SQL, import them once, or the first apply fails with "already exists". Run from `terraform/envs/dev` with the variables set as in the pipeline, shown here for dev (a pipeline-approved admin run, not a developer machine):

   ```
   terraform import 'module.cicd.snowflake_database.cicd[0]' 'CICD'
   terraform import 'module.cicd.snowflake_schema.state[0]' '"CICD"."STATE"'
   terraform import 'module.cicd.snowflake_schema.audit[0]' '"CICD"."AUDIT"'
   terraform import 'module.cicd.snowflake_stage_internal.dbt_state[0]' '"CICD"."STATE"."DBT_STATE"'
   terraform import 'module.cicd.snowflake_table.backup_log[0]' '"CICD"."AUDIT"."BACKUP_LOG"'
   terraform import 'module.env.snowflake_database.analytics' 'ANALYTICS_DEV'
   terraform import 'module.env.snowflake_schema.backup' '"ANALYTICS_DEV"."BACKUP"'
   ```

   Then run the plan pipeline and read it: it should show no replacement of any imported object. Test needs only its own `module.env.*` objects imported; `CICD` belongs to the dev state.
7. **Order.** Apply `dev` before `test` the first time: the test state only adds grants on the `CICD` objects that dev creates.

Checks to run on the first real runs (these could not be tested without access): the `AzureCLI@2` step signs in with OIDC and can read the state; `azureSubscription: $(AZURE_SERVICE_CONNECTION)` resolves from the variable group (if not, hard-code the connection name in the YAML); the PR plan appears in the run summary; the Apply stage waits on the Environment check.

## Known limits

- Branch names with a `/` (for example `feature/x`) do not map to a variable group. Only `dev` and `test` are triggered.
- `master` is not triggered yet. It would need a `snowflake-master` group, or per-stage groups, once the branching model is decided.
