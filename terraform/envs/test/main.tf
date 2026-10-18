module "env" {
  source = "../../modules/environment"

  providers = {
    snowflake.objects  = snowflake.objects
    snowflake.security = snowflake.security
  }

  env                 = "test"
  warehouse_size      = var.warehouse_size
  deployer_user_names = var.deployer_user_names
}

module "cicd" {
  source = "../../modules/cicd"

  providers = {
    snowflake.objects  = snowflake.objects
    snowflake.security = snowflake.security
  }

  # CICD is shared by all environments. Only dev owns the objects; apply dev first.
  manage_objects      = false
  deployer_role_names = [module.env.deployer_role_name]
}
