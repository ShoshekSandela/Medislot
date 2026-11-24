-- MediSlot Milestone 5 migration.
-- Run once against the existing medislot database before deploying Appointment Lambda.
-- The checks make this script safe to re-run on MySQL 8.x.

USE medislot;

SET @has_consultation_fee = (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'appointments'
    AND column_name = 'consultation_fee'
);
SET @sql = IF(
  @has_consultation_fee = 0,
  'ALTER TABLE appointments ADD COLUMN consultation_fee DECIMAL(10,2) NOT NULL DEFAULT 0.00',
  'SELECT 1'
);
PREPARE migration_stmt FROM @sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;

SET @has_idempotency_key = (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'appointments'
    AND column_name = 'idempotency_key'
);
SET @sql = IF(
  @has_idempotency_key = 0,
  'ALTER TABLE appointments ADD COLUMN idempotency_key VARCHAR(128) NULL',
  'SELECT 1'
);
PREPARE migration_stmt FROM @sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;

SET @has_idempotency_index = (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE()
    AND table_name = 'appointments'
    AND index_name = 'uq_appointment_idempotency_key'
);
SET @sql = IF(
  @has_idempotency_index = 0,
  'CREATE UNIQUE INDEX uq_appointment_idempotency_key ON appointments (idempotency_key)',
  'SELECT 1'
);
PREPARE migration_stmt FROM @sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;

SET @has_refund_tier = (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'refund_ledger'
    AND column_name = 'refund_tier'
);
SET @sql = IF(
  @has_refund_tier = 0,
  'ALTER TABLE refund_ledger ADD COLUMN refund_tier VARCHAR(50) NOT NULL DEFAULT ''UNSPECIFIED''',
  'SELECT 1'
);
PREPARE migration_stmt FROM @sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;

