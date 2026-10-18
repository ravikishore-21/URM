-- One-time setup for table backups (Phase 3). Safe to run more than once.
-- Run as the role that owns CICD objects (agreed with the client; never ACCOUNTADMIN in code).
-- Terraform now owns these objects (terraform/modules/cicd, Phase 5). Use this file only for a
-- sandbox without Terraform. If the objects already exist, import them instead of re-creating
-- (docs/azure-devops-setup.md, section 7). Keep this file in step with the module.

CREATE DATABASE IF NOT EXISTS CICD;
CREATE SCHEMA IF NOT EXISTS CICD.AUDIT;

CREATE TABLE IF NOT EXISTS CICD.AUDIT.BACKUP_LOG (
  run_id        STRING NOT NULL COMMENT 'Pipeline run that made the backup (Build.BuildId)',
  pr_number     STRING COMMENT 'Pull request, when known',
  commit_sha    STRING,
  environment   STRING NOT NULL COMMENT 'dev, test, ...',
  source_table  STRING NOT NULL COMMENT 'DB.SCHEMA.TABLE that was cloned',
  backup_table  STRING NOT NULL COMMENT 'DB.BACKUP.TABLE__RUN_ID',
  created_at    TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'One row per table backup taken by dbt/scripts/backup.py';

-- Added after the first release of the table; set by cleanup_backups.py when the clone is dropped.
ALTER TABLE CICD.AUDIT.BACKUP_LOG ADD COLUMN IF NOT EXISTS dropped_at TIMESTAMP_LTZ;

-- The BACKUP schema in each environment database is owned by Terraform (Phase 5).
-- Until then, run once per environment (replace DEV):
--   CREATE SCHEMA IF NOT EXISTS ANALYTICS_DEV.BACKUP;

-- Minimum grants the deployer role needs (shown for review; apply through Terraform).
-- Replace DBT_DEPLOYER_DEV and ANALYTICS_DEV for each environment.
--   GRANT USAGE ON SCHEMA CICD.AUDIT TO ROLE DBT_DEPLOYER_DEV;
--   GRANT SELECT, INSERT, UPDATE ON TABLE CICD.AUDIT.BACKUP_LOG TO ROLE DBT_DEPLOYER_DEV;
--   GRANT USAGE, CREATE TABLE ON SCHEMA ANALYTICS_DEV.BACKUP TO ROLE DBT_DEPLOYER_DEV;
-- Cloning needs SELECT on the source tables; the deployer already owns the tables it builds.
-- Dropping expired backups needs OWNERSHIP of the clones, which the creating role holds.
