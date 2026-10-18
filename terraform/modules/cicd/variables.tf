variable "manage_objects" {
  type        = bool
  description = "True in the one environment that owns the shared CICD objects. False elsewhere: the module then only adds grants for the given roles on objects that already exist."
}

variable "deployer_role_names" {
  type        = set(string)
  description = "Deployer roles that receive access to the CICD stage and audit table."

  validation {
    condition     = alltrue([for r in var.deployer_role_names : can(regex("^[A-Z][A-Z0-9_]*$", r))])
    error_message = "Role names must be upper-case Snowflake identifiers (letters, digits, underscore)."
  }
}

variable "database_name" {
  type        = string
  description = "Name of the shared CICD database."
  default     = "CICD"

  validation {
    condition     = can(regex("^[A-Z][A-Z0-9_]*$", var.database_name))
    error_message = "database_name must be an upper-case Snowflake identifier."
  }
}
