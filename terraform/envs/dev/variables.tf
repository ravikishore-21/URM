variable "organization_name" {
  type        = string
  description = "Snowflake organization name (first part of the account identifier)."
}

variable "account_name" {
  type        = string
  description = "Snowflake account name (second part of the account identifier)."
}

variable "tf_user" {
  type        = string
  description = "Snowflake service user that Terraform signs in as (key-pair authentication)."
}

variable "objects_role" {
  type        = string
  description = "Role used to create databases, schemas, warehouses, stages and tables. Never ACCOUNTADMIN."
  default     = "SYSADMIN"

  validation {
    condition     = upper(var.objects_role) != "ACCOUNTADMIN"
    error_message = "ACCOUNTADMIN must not be used by Terraform."
  }
}

variable "security_role" {
  type        = string
  description = "Role used to create roles and grants. Never ACCOUNTADMIN."
  default     = "SECURITYADMIN"

  validation {
    condition     = upper(var.security_role) != "ACCOUNTADMIN"
    error_message = "ACCOUNTADMIN must not be used by Terraform."
  }
}

variable "warehouse_size" {
  type        = string
  description = "Size of the transform warehouse for this environment."
  default     = "XSMALL"
}

variable "deployer_user_names" {
  type        = set(string)
  description = "Users (for example the dbt CI service user) that receive the deployer role."
  default     = []
}
