locals {
  database_name   = var.database_name
  state_schema    = "STATE"
  audit_schema    = "AUDIT"
  stage_name      = "DBT_STATE"
  backup_log_name = "BACKUP_LOG"

  # Quoted names, so grants work with and without the objects in this state.
  fq_database = "\"${local.database_name}\""
  fq_state    = "\"${local.database_name}\".\"${local.state_schema}\""
  fq_audit    = "\"${local.database_name}\".\"${local.audit_schema}\""
  fq_stage    = "\"${local.database_name}\".\"${local.state_schema}\".\"${local.stage_name}\""
  fq_table    = "\"${local.database_name}\".\"${local.audit_schema}\".\"${local.backup_log_name}\""
}

# ---- Objects (only where manage_objects is true) ----

resource "snowflake_database" "cicd" {
  provider = snowflake.objects
  count    = var.manage_objects ? 1 : 0

  name    = local.database_name
  comment = "CI/CD state and audit data shared by all environments. Managed by Terraform."

  lifecycle {
    prevent_destroy = true
  }
}

resource "snowflake_schema" "state" {
  provider = snowflake.objects
  count    = var.manage_objects ? 1 : 0

  database = snowflake_database.cicd[0].name
  name     = local.state_schema
  comment  = "dbt manifests per environment. Managed by Terraform."
}

resource "snowflake_schema" "audit" {
  provider = snowflake.objects
  count    = var.manage_objects ? 1 : 0

  database = snowflake_database.cicd[0].name
  name     = local.audit_schema
  comment  = "Audit tables written by the pipelines. Managed by Terraform."
}

resource "snowflake_stage_internal" "dbt_state" {
  provider = snowflake.objects
  count    = var.manage_objects ? 1 : 0

  database = snowflake_database.cicd[0].name
  schema   = snowflake_schema.state[0].name
  name     = local.stage_name
  comment  = "dbt manifest per environment: <env>/manifest.json"

  # Holds the manifests that drive state:modified selection.
  lifecycle {
    prevent_destroy = true
  }
}

resource "snowflake_table" "backup_log" {
  provider = snowflake.objects
  count    = var.manage_objects ? 1 : 0

  database = snowflake_database.cicd[0].name
  schema   = snowflake_schema.audit[0].name
  name     = local.backup_log_name
  comment  = "One row per table backup taken by dbt/scripts/backup.py"

  column {
    name     = "RUN_ID"
    type     = "VARCHAR(16777216)"
    nullable = false
    comment  = "Pipeline run that made the backup (Build.BuildId)"
  }

  column {
    name     = "PR_NUMBER"
    type     = "VARCHAR(16777216)"
    nullable = true
    comment  = "Pull request, when known"
  }

  column {
    name     = "COMMIT_SHA"
    type     = "VARCHAR(16777216)"
    nullable = true
  }

  column {
    name     = "ENVIRONMENT"
    type     = "VARCHAR(16777216)"
    nullable = false
    comment  = "dev, test, ..."
  }

  column {
    name     = "SOURCE_TABLE"
    type     = "VARCHAR(16777216)"
    nullable = false
    comment  = "DB.SCHEMA.TABLE that was cloned"
  }

  column {
    name     = "BACKUP_TABLE"
    type     = "VARCHAR(16777216)"
    nullable = false
    comment  = "DB.BACKUP.TABLE__RUN_ID"
  }

  column {
    name     = "CREATED_AT"
    type     = "TIMESTAMP_LTZ(9)"
    nullable = true

    default {
      expression = "CURRENT_TIMESTAMP()"
    }
  }

  column {
    name     = "DROPPED_AT"
    type     = "TIMESTAMP_LTZ(9)"
    nullable = true
    comment  = "Set by cleanup_backups.py when the clone is dropped"
  }

  # Audit trail. A column change must never silently replace (empty) the table.
  lifecycle {
    prevent_destroy = true
  }
}

# ---- Grants to the deployer roles ----

resource "snowflake_grant_privileges_to_account_role" "database_usage" {
  provider   = snowflake.security
  for_each   = var.deployer_role_names
  depends_on = [snowflake_database.cicd]

  account_role_name = each.value
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "DATABASE"
    object_name = local.fq_database
  }
}

resource "snowflake_grant_privileges_to_account_role" "state_schema_usage" {
  provider   = snowflake.security
  for_each   = var.deployer_role_names
  depends_on = [snowflake_schema.state]

  account_role_name = each.value
  privileges        = ["USAGE"]

  on_schema {
    schema_name = local.fq_state
  }
}

resource "snowflake_grant_privileges_to_account_role" "audit_schema_usage" {
  provider   = snowflake.security
  for_each   = var.deployer_role_names
  depends_on = [snowflake_schema.audit]

  account_role_name = each.value
  privileges        = ["USAGE"]

  on_schema {
    schema_name = local.fq_audit
  }
}

resource "snowflake_grant_privileges_to_account_role" "stage_read_write" {
  provider   = snowflake.security
  for_each   = var.deployer_role_names
  depends_on = [snowflake_stage_internal.dbt_state]

  account_role_name = each.value
  privileges        = ["READ", "WRITE"]

  on_schema_object {
    object_type = "STAGE"
    object_name = local.fq_stage
  }
}

resource "snowflake_grant_privileges_to_account_role" "backup_log_dml" {
  provider   = snowflake.security
  for_each   = var.deployer_role_names
  depends_on = [snowflake_table.backup_log]

  account_role_name = each.value
  privileges        = ["SELECT", "INSERT", "UPDATE"]

  on_schema_object {
    object_type = "TABLE"
    object_name = local.fq_table
  }
}
