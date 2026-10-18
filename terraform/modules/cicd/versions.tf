terraform {
  required_version = ">= 1.6.0"

  required_providers {
    snowflake = {
      source                = "snowflakedb/snowflake"
      version               = ">= 2.22.0, < 3.0.0"
      configuration_aliases = [snowflake.objects, snowflake.security]
    }
  }
}
