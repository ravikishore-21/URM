terraform {
  required_version = "1.12.2"

  required_providers {
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "2.22.0"
    }
  }

  # Partial configuration: the storage account, resource group and container come from
  # -backend-config flags in the pipeline. Authentication uses Azure AD (ARM_* variables).
  backend "azurerm" {
    key              = "snowflake/test/terraform.tfstate"
    use_azuread_auth = true
  }
}
