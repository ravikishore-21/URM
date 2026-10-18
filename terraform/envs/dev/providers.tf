# Two aliases keep privileges apart: object creation never needs role/grant management.
# Authentication is key-pair (SNOWFLAKE_JWT). The private key is NOT a Terraform variable: the
# provider reads it from the SNOWFLAKE_PRIVATE_KEY environment variable, so it is never stored in
# a saved plan file (variable values are).

provider "snowflake" {
  alias = "objects"

  organization_name        = var.organization_name
  account_name             = var.account_name
  user                     = var.tf_user
  role                     = var.objects_role
  authenticator            = "SNOWFLAKE_JWT"
  preview_features_enabled = ["snowflake_table_resource"]
}

provider "snowflake" {
  alias = "security"

  organization_name        = var.organization_name
  account_name             = var.account_name
  user                     = var.tf_user
  role                     = var.security_role
  authenticator            = "SNOWFLAKE_JWT"
  preview_features_enabled = ["snowflake_table_resource"]
}
