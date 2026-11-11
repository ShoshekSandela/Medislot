-- MediSlot schema matching the supplied ER diagram.
-- No users table. Doctor and patient identities are their own numeric profile IDs.
-- Use on a NEW/EMPTY database only; existing UUID-based databases need a migration.
-- All hospital, availability, blackout, appointment, and generated slot times use IST (Asia/Kolkata).

CREATE DATABASE IF NOT EXISTS medislot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE medislot;

CREATE TABLE IF NOT EXISTS specialisations (
  specialisation_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  specialisation_name VARCHAR(100) NOT NULL,
  description VARCHAR(255),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (specialisation_id),
  UNIQUE KEY uq_specialisation_name (specialisation_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctors (
  doctor_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  registration_number VARCHAR(100) NOT NULL,
  first_name VARCHAR(100) NOT NULL,
  last_name VARCHAR(100) NOT NULL,
  email VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  qualification VARCHAR(255),
  experience_years SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  profile_photo_url VARCHAR(500),
  profile_status ENUM('PENDING','ACTIVE','INACTIVE') NOT NULL DEFAULT 'PENDING',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (doctor_id),
  UNIQUE KEY uq_doctor_registration (registration_number),
  UNIQUE KEY uq_doctor_email (email),
  KEY idx_doctor_status_first_name (profile_status, first_name, doctor_id),
  KEY idx_doctor_status_last_name (profile_status, last_name, doctor_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS hospitals (
  hospital_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  hospital_name VARCHAR(255) NOT NULL,
  branch_name VARCHAR(255),
  address_line1 VARCHAR(255) NOT NULL,
  address_line2 VARCHAR(255),
  city VARCHAR(100) NOT NULL,
  state VARCHAR(100) NOT NULL,
  postal_code VARCHAR(20),
  country VARCHAR(100) NOT NULL DEFAULT 'India',
  timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
  phone VARCHAR(20),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (hospital_id),
  KEY idx_hospital_city (city),
  KEY idx_hospital_name (hospital_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS consultation_types (
  consultation_type_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  doctor_id BIGINT UNSIGNED NULL,
  type_name VARCHAR(100) NOT NULL,
  duration_minutes SMALLINT UNSIGNED NOT NULL,
  capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  description VARCHAR(500),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  consultation_fee DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (consultation_type_id),
  UNIQUE KEY uq_consultation_type_doctor_name (doctor_id, type_name),
  CONSTRAINT fk_consultation_type_doctor
    FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS patients (
  patient_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  first_name VARCHAR(100) NOT NULL,
  last_name VARCHAR(100),
  email VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  date_of_birth DATE,
  gender VARCHAR(20),
  phone VARCHAR(20),
  account_status ENUM('ACTIVE','DEACTIVATED') NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (patient_id),
  UNIQUE KEY uq_patient_email (email),
  KEY idx_patient_phone (phone)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_specialisations (
  doctor_id BIGINT UNSIGNED NOT NULL,
  specialisation_id BIGINT UNSIGNED NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (doctor_id, specialisation_id),
  CONSTRAINT fk_ds_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id) ON DELETE CASCADE,
  CONSTRAINT fk_ds_specialisation FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id),
  KEY idx_doctor_specialisation_search (specialisation_id, doctor_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_hospitals (
  doctor_hospital_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  doctor_id BIGINT UNSIGNED NOT NULL,
  hospital_id BIGINT UNSIGNED NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (doctor_hospital_id),
  UNIQUE KEY uq_doctor_hospital (doctor_id, hospital_id),
  CONSTRAINT fk_dh_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id) ON DELETE CASCADE,
  CONSTRAINT fk_dh_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id),
  KEY idx_doctor_hospital_search (hospital_id, doctor_id, is_active)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_blackouts (
  blackout_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  doctor_id BIGINT UNSIGNED NOT NULL,
  hospital_id BIGINT UNSIGNED NOT NULL,
  start_at_ist DATETIME NOT NULL,
  end_at_ist DATETIME NOT NULL,
  reason VARCHAR(255),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (blackout_id),
  KEY idx_blackout_doctor_time (doctor_id, start_at_ist, end_at_ist),
  CONSTRAINT fk_blackout_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id),
  CONSTRAINT fk_blackout_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id),
  CONSTRAINT chk_blackout_range CHECK (end_at_ist > start_at_ist)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_availability (
  availability_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  doctor_id BIGINT UNSIGNED NOT NULL,
  hospital_id BIGINT UNSIGNED NOT NULL,
  consultation_type_id BIGINT UNSIGNED NOT NULL,
  day_of_week TINYINT UNSIGNED NOT NULL COMMENT '0=Sunday, 1=Monday, ..., 6=Saturday; availability times are IST (Asia/Kolkata)',
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
  effective_from DATE NOT NULL,
  effective_until DATE,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (availability_id),
  KEY idx_availability_doctor_day (doctor_id, day_of_week, is_active),
  CONSTRAINT fk_availability_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id),
  CONSTRAINT fk_availability_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id),
  CONSTRAINT fk_availability_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
  CONSTRAINT chk_availability_day CHECK (day_of_week BETWEEN 0 AND 6),
  CONSTRAINT chk_availability_time CHECK (end_time > start_time),
  CONSTRAINT chk_availability_dates CHECK (effective_until IS NULL OR effective_until >= effective_from)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS appointments (
  appointment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  patient_id BIGINT UNSIGNED NOT NULL,
  doctor_id BIGINT UNSIGNED NOT NULL,
  hospital_id BIGINT UNSIGNED NOT NULL,
  consultation_type_id BIGINT UNSIGNED NOT NULL,
  slot_id VARCHAR(128) NOT NULL,
  start_at_ist DATETIME NOT NULL,
  end_at_ist DATETIME NOT NULL,
  timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
  status ENUM('BOOKED','CANCELLED','RESCHEDULED','COMPLETED') NOT NULL DEFAULT 'BOOKED',
  reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
  cancelled_at_ist DATETIME,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (appointment_id),
  KEY idx_appointment_patient_start (patient_id, start_at_ist),
  KEY idx_appointment_doctor_start (doctor_id, start_at_ist),
  KEY idx_appointment_slot (slot_id),
  CONSTRAINT fk_appointment_patient FOREIGN KEY (patient_id) REFERENCES patients(patient_id),
  CONSTRAINT fk_appointment_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id),
  CONSTRAINT fk_appointment_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id),
  CONSTRAINT fk_appointment_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
  CONSTRAINT chk_appointment_time CHECK (end_at_ist > start_at_ist)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS refund_ledger (
  refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  appointment_id BIGINT UNSIGNED NOT NULL,
  cancellation_type ENUM('PATIENT','DOCTOR') NOT NULL,
  original_fee DECIMAL(10,2) NOT NULL,
  refund_percentage DECIMAL(5,2) NOT NULL,
  refund_amount DECIMAL(10,2) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (refund_id),
  KEY idx_refund_appointment (appointment_id),
  CONSTRAINT fk_refund_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
  CONSTRAINT chk_refund_percentage CHECK (refund_percentage BETWEEN 0 AND 100)
) ENGINE=InnoDB;


-- Standard consultation durations supported by MediSlot.
-- Re-running this block is safe because type_name is unique.
INSERT INTO consultation_types
    (type_name, duration_minutes, capacity, description, is_active, consultation_fee)
VALUES
    ('15 Minutes', 15, 1, '15-minute consultation', TRUE, 0.00),
    ('30 Minutes', 30, 1, '30-minute consultation', TRUE, 0.00),
    ('60 Minutes', 60, 1, '60-minute consultation', TRUE, 0.00) AS new_values
ON DUPLICATE KEY UPDATE
    duration_minutes = new_values.duration_minutes,
    is_active = TRUE;
