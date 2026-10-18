variable "env" {
  type        = string
  description = "Short environment name in lower case, for example dev or test. Used to build object names (ANALYTICS_<ENV>)."

  validation {
    condition     = can(regex("^[a-z]{2,10}$", var.env))
    error_message = "env must be 2-10 lower-case letters, for example dev, test, uat, stage, prod."
  }
}

variable "warehouse_size" {
  type        = string
  description = "Size of the transform warehouse, for example XSMALL."
  default     = "XSMALL"

  validation {
    condition     = contains(["XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE"], var.warehouse_size)
    error_message = "warehouse_size must be one of XSMALL, SMALL, MEDIUM, LARGE, XLARGE. Larger sizes need a review."
  }
}

variable "warehouse_auto_suspend_seconds" {
  type        = number
  description = "Seconds of inactivity before the transform warehouse suspends."
  default     = 60

  validation {
    condition     = var.warehouse_auto_suspend_seconds >= 60 && var.warehouse_auto_suspend_seconds <= 3600
    error_message = "warehouse_auto_suspend_seconds must be between 60 and 3600."
  }
}

variable "deployer_user_names" {
  type        = set(string)
  description = "Snowflake users (for example the dbt CI service user) that receive the deployer role. Users must already exist."
  default     = []
}
