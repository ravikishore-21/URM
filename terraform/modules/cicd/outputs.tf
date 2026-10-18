output "database_name" {
  description = "Name of the shared CICD database."
  value       = local.database_name
}

output "state_stage_name" {
  description = "Fully qualified name of the stage that holds the dbt manifests."
  value       = "${local.database_name}.${local.state_schema}.${local.stage_name}"
}

output "backup_log_table_name" {
  description = "Fully qualified name of the backup audit table."
  value       = "${local.database_name}.${local.audit_schema}.${local.backup_log_name}"
}
