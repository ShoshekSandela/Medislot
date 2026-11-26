-- Health Camp per-session capacity override and explicit appointment outcomes.
-- Run once against the existing MediSlot database before deploying the updated Lambdas.

CREATE TABLE IF NOT EXISTS slot_capacity_overrides (
    override_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    slot_start_ist DATETIME NOT NULL,
    capacity TINYINT UNSIGNED NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (override_id),
    UNIQUE KEY uq_session_capacity_override (doctor_id, consultation_type_id, slot_start_ist),
    KEY idx_session_capacity_override_slot (doctor_id, slot_start_ist),
    CONSTRAINT fk_session_capacity_override_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_session_capacity_override_consultation
        FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
    CONSTRAINT chk_session_capacity_override_range CHECK (capacity BETWEEN 2 AND 10)
) ENGINE=InnoDB;

ALTER TABLE appointments
    ADD COLUMN outcome_at_ist DATETIME NULL AFTER status;
