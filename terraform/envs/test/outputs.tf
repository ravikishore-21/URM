output "database_name" {
  description = "Analytics database for this environment."
  value       = module.env.database_name
}

output "warehouse_name" {
  description = "Transform warehouse for this environment."
  value       = module.env.warehouse_name
}

output "deployer_role_name" {
  description = "Role the dbt pipeline uses in this environment."
  value       = module.env.deployer_role_name
}

output "state_stage_name" {
  description = "Stage that holds the dbt manifests."
  value       = module.cicd.state_stage_name
}
