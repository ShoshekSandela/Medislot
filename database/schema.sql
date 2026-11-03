-- MediSlot Hospital Appointment & Doctor Availability System
-- RDS MySQL schema based on the supplied ER diagram.
-- All primary IDs use BIGINT AUTO_INCREMENT (no UUID IDs).

CREATE DATABASE IF NOT EXISTS medislot
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE medislot;

-- 1. SPECIALISATIONS
CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    specialisation_name VARCHAR(100) NOT NULL,
    description VARCHAR(255) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (specialisation_id),
    UNIQUE KEY uq_specialisation_name (specialisation_name)
) ENGINE=InnoDB;

-- 2. DOCTORS
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    registration_number VARCHAR(100) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    qualification VARCHAR(255) NULL,
    experience_years SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    profile_photo_url VARCHAR(500) NULL,
    profile_status ENUM('PENDING', 'ACTIVE', 'INACTIVE')
        NOT NULL DEFAULT 'PENDING',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id),
    UNIQUE KEY uq_doctor_registration_number (registration_number),
    UNIQUE KEY uq_doctor_email (email)
) ENGINE=InnoDB;

-- 3. HOSPITALS
CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    hospital_name VARCHAR(255) NOT NULL,
    branch_name VARCHAR(255) NULL,
    address_line1 VARCHAR(255) NOT NULL,
    address_line2 VARCHAR(255) NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NOT NULL,
    postal_code VARCHAR(20) NULL,
    country VARCHAR(100) NOT NULL DEFAULT 'India',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    phone VARCHAR(20) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (hospital_id),
    KEY idx_hospitals_city (city),
    KEY idx_hospitals_active (is_active)
) ENGINE=InnoDB;

-- 4. CONSULTATION TYPES
CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    type_name VARCHAR(100) NOT NULL,
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    description VARCHAR(500) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    consultation_fee DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_type_name (type_name),
    CONSTRAINT chk_consultation_duration CHECK (duration_minutes > 0),
    CONSTRAINT chk_consultation_capacity CHECK (capacity > 0),
    CONSTRAINT chk_consultation_fee CHECK (consultation_fee >= 0)
) ENGINE=InnoDB;

-- 5. PATIENTS
CREATE TABLE IF NOT EXISTS patients (
    patient_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NULL,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    date_of_birth DATE NULL,
    gender VARCHAR(20) NULL,
    phone VARCHAR(20) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patient_email (email),
    KEY idx_patient_phone (phone)
) ENGINE=InnoDB;

-- 6. DOCTOR_SPECIALISATIONS (many-to-many)
CREATE TABLE IF NOT EXISTS doctor_specialisations (
    doctor_id BIGINT UNSIGNED NOT NULL,
    specialisation_id BIGINT UNSIGNED NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id, specialisation_id),
    CONSTRAINT fk_doctor_specialisations_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_specialisations_specialisation
        FOREIGN KEY (specialisation_id)
        REFERENCES specialisations(specialisation_id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB;

-- 7. DOCTOR_HOSPITALS (doctor can work at multiple hospital branches)
CREATE TABLE IF NOT EXISTS doctor_hospitals (
    doctor_hospital_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    hospital_id BIGINT UNSIGNED NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_hospital_id),
    UNIQUE KEY uq_doctor_hospital (doctor_id, hospital_id),
    KEY idx_doctor_hospitals_hospital (hospital_id),
    CONSTRAINT fk_doctor_hospitals_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_hospitals_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB;

-- 8. DOCTOR_BLACKOUTS
CREATE TABLE IF NOT EXISTS doctor_blackouts (
    blackout_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    hospital_id BIGINT UNSIGNED NOT NULL,
    start_at_utc DATETIME NOT NULL,
    end_at_utc DATETIME NOT NULL,
    reason VARCHAR(255) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (blackout_id),
    KEY idx_blackouts_doctor_time (doctor_id, start_at_utc, end_at_utc),
    KEY idx_blackouts_hospital (hospital_id),
    CONSTRAINT fk_doctor_blackouts_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_blackouts_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_blackout_time CHECK (end_at_utc > start_at_utc)
) ENGINE=InnoDB;

-- 9. DOCTOR_AVAILABILITY
CREATE TABLE IF NOT EXISTS doctor_availability (
    availability_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    hospital_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL COMMENT '0=Sunday, 6=Saturday',
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    effective_from DATE NOT NULL,
    effective_until DATE NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (availability_id),
    KEY idx_availability_doctor_day (doctor_id, day_of_week, is_active),
    KEY idx_availability_hospital (hospital_id),
    KEY idx_availability_consultation (consultation_type_id),
    CONSTRAINT fk_doctor_availability_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_availability_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_availability_consultation
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types(consultation_type_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_availability_day CHECK (day_of_week BETWEEN 0 AND 6),
    CONSTRAINT chk_availability_time CHECK (end_time > start_time),
    CONSTRAINT chk_availability_dates
        CHECK (effective_until IS NULL OR effective_until >= effective_from)
) ENGINE=InnoDB;

-- 10. APPOINTMENTS
CREATE TABLE IF NOT EXISTS appointments (
    appointment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    patient_id BIGINT UNSIGNED NOT NULL,
    doctor_id BIGINT UNSIGNED NOT NULL,
    hospital_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    slot_id VARCHAR(128) NOT NULL,
    start_at_utc DATETIME NOT NULL,
    end_at_utc DATETIME NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    status ENUM('BOOKED', 'CANCELLED', 'RESCHEDULED', 'COMPLETED')
        NOT NULL DEFAULT 'BOOKED',
    reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    cancelled_at_utc DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (appointment_id),
    KEY idx_appointments_patient_start (patient_id, start_at_utc),
    KEY idx_appointments_doctor_start (doctor_id, start_at_utc),
    KEY idx_appointments_hospital_start (hospital_id, start_at_utc),
    KEY idx_appointments_slot (slot_id),
    KEY idx_appointments_status (status),
    CONSTRAINT fk_appointments_patient
        FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_appointments_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_appointments_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_appointments_consultation
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types(consultation_type_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_appointment_time CHECK (end_at_utc > start_at_utc)
) ENGINE=InnoDB;

-- 11. REFUND_LEDGER
CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    cancellation_type ENUM('PATIENT', 'DOCTOR') NOT NULL,
    original_fee DECIMAL(10,2) NOT NULL,
    refund_percentage DECIMAL(5,2) NOT NULL,
    refund_amount DECIMAL(10,2) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (refund_id),
    KEY idx_refund_appointment (appointment_id),
    CONSTRAINT fk_refund_ledger_appointment
        FOREIGN KEY (appointment_id)
        REFERENCES appointments(appointment_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_refund_original_fee CHECK (original_fee >= 0),
    CONSTRAINT chk_refund_percentage
        CHECK (refund_percentage BETWEEN 0 AND 100),
    CONSTRAINT chk_refund_amount CHECK (refund_amount >= 0)
) ENGINE=InnoDB;
