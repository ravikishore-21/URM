output "database_name" {
  description = "Name of the analytics database for this environment."
  value       = snowflake_database.analytics.name
}

output "backup_schema_name" {
  description = "Name of the BACKUP schema inside the analytics database."
  value       = snowflake_schema.backup.name
}

output "warehouse_name" {
  description = "Name of the transform warehouse."
  value       = snowflake_warehouse.transform.name
}

output "deployer_role_name" {
  description = "Name of the deployer role used by the dbt pipeline."
  value       = snowflake_account_role.deployer.name
}
