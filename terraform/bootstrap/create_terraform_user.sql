-- One-time bootstrap for Terraform itself. Terraform cannot create the user it signs in as.
-- Run by a Snowflake administrator in the client account, once. Safe to run again.
-- Never run as ACCOUNTADMIN: each statement names the lowest role that can run it.
--
-- Contains no secrets. The public key is not secret; the private key stays in the Azure DevOps
-- Secure File snowflake_tf_key.p8 and is never written here.
--
-- Create the key pair on a trusted machine (unencrypted PKCS#8 is what the pipeline expects):
--   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out snowflake_tf_key.p8 -nocrypt
--   openssl rsa -in snowflake_tf_key.p8 -pubout -out snowflake_tf_key.pub
-- Paste the body of the .pub file (without the BEGIN/END lines) below.

USE ROLE USERADMIN;
CREATE USER IF NOT EXISTS TERRAFORM_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = SYSADMIN
  COMMENT = 'Service user for Terraform (key-pair authentication only)';

USE ROLE SECURITYADMIN;
ALTER USER TERRAFORM_SVC SET RSA_PUBLIC_KEY = '<PASTE_PUBLIC_KEY_BODY_HERE>';

-- Terraform uses two provider roles (terraform/envs/*/providers.tf):
--   objects  -> SYSADMIN      databases, schemas, warehouses, stages, tables
--   security -> SECURITYADMIN roles and grants
-- Granting SECURITYADMIN to a service user is broad. Open question 5 asks the client whether to
-- replace both with custom roles; if so, change objects_role and security_role instead of this file.
GRANT ROLE SYSADMIN TO USER TERRAFORM_SVC;
GRANT ROLE SECURITYADMIN TO USER TERRAFORM_SVC;
