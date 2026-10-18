locals {
  env_upper      = upper(var.env)
  database_name  = "ANALYTICS_${local.env_upper}"
  warehouse_name = "TRANSFORM_WH_${local.env_upper}"
  role_name      = "DBT_DEPLOYER_${local.env_upper}"
}

# ---- Objects (created with the objects provider, default role SYSADMIN) ----

resource "snowflake_database" "analytics" {
  provider = snowflake.objects

  name    = local.database_name
  comment = "dbt target database for ${local.env_upper}. Managed by Terraform."

  # Holds business data. Removing it needs a deliberate code change.
  lifecycle {
    prevent_destroy = true
  }
}

resource "snowflake_schema" "backup" {
  provider = snowflake.objects

  database = snowflake_database.analytics.name
  name     = "BACKUP"
  comment  = "Zero-copy clones taken before dbt changes tables. Managed by Terraform."

  # Dropping this schema deletes every backup clone in it.
  lifecycle {
    prevent_destroy = true
  }
}

resource "snowflake_warehouse" "transform" {
  provider = snowflake.objects

  name                = local.warehouse_name
  warehouse_size      = var.warehouse_size
  auto_suspend        = var.warehouse_auto_suspend_seconds
  auto_resume         = "true"
  initially_suspended = true
  comment             = "dbt transforms for ${local.env_upper}. Managed by Terraform."
}

# ---- Role and grants (created with the security provider, default role SECURITYADMIN) ----

resource "snowflake_account_role" "deployer" {
  provider = snowflake.security

  name    = local.role_name
  comment = "Used by the dbt deploy pipeline for ${local.env_upper}. Managed by Terraform."
}

resource "snowflake_grant_privileges_to_account_role" "database" {
  provider = snowflake.security

  account_role_name = snowflake_account_role.deployer.name
  privileges        = ["USAGE", "CREATE SCHEMA"]

  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.analytics.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "backup_schema" {
  provider = snowflake.security

  account_role_name = snowflake_account_role.deployer.name
  privileges        = ["USAGE", "CREATE TABLE"]

  on_schema {
    schema_name = snowflake_schema.backup.fully_qualified_name
  }
}

resource "snowflake_grant_privileges_to_account_role" "warehouse" {
  provider = snowflake.security

  account_role_name = snowflake_account_role.deployer.name
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.transform.name
  }
}

resource "snowflake_grant_account_role" "deployer_to_user" {
  provider = snowflake.security
  for_each = var.deployer_user_names

  role_name = snowflake_account_role.deployer.name
  user_name = each.value
}
