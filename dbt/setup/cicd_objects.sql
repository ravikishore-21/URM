-- One-time setup for dbt state storage. Safe to run more than once.
-- Run as the role that owns CICD objects (agreed with the client; never ACCOUNTADMIN in code).
-- Terraform now owns these objects (terraform/modules/cicd, Phase 5). Use this file only for a
-- sandbox without Terraform. If the objects already exist, import them instead of re-creating
-- (docs/azure-devops-setup.md, section 7). Keep this file in step with the module.

CREATE DATABASE IF NOT EXISTS CICD;
CREATE SCHEMA IF NOT EXISTS CICD.STATE;
CREATE STAGE IF NOT EXISTS CICD.STATE.DBT_STATE
  COMMENT = 'dbt manifest per environment: <env>/manifest.json';

-- Paths used by the pipelines:
--   @CICD.STATE.DBT_STATE/dev/manifest.json
--   @CICD.STATE.DBT_STATE/test/manifest.json

-- Minimum grants the deployer role needs (shown for review; apply through Terraform).
-- Replace DBT_DEPLOYER_DEV with the role for each environment.
--   GRANT USAGE ON DATABASE CICD TO ROLE DBT_DEPLOYER_DEV;
--   GRANT USAGE ON SCHEMA CICD.STATE TO ROLE DBT_DEPLOYER_DEV;
--   GRANT READ, WRITE ON STAGE CICD.STATE.DBT_STATE TO ROLE DBT_DEPLOYER_DEV;
-- PR checks also need to create and drop the CI_PR_<n> schemas in the target database:
--   GRANT CREATE SCHEMA ON DATABASE ANALYTICS_DEV TO ROLE DBT_DEPLOYER_DEV;
-- That grant is broader than deploys need. Prefer a separate CI role limited to CI_PR_* schemas.
