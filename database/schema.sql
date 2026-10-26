CREATE DATABASE IF NOT EXISTS medislot;
USE medislot;

-- 1. Users
CREATE TABLE IF NOT EXISTS users (
    user_id CHAR(36) PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin', 'doctor', 'patient') NOT NULL,
    status ENUM('active', 'inactive', 'blocked') DEFAULT 'active',
    created_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3)
);

-- 2. Hospitals
CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id CHAR(36) PRIMARY KEY,
    hospital_name VARCHAR(255) NOT NULL,
    address VARCHAR(500),
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NOT NULL,
    country VARCHAR(100) DEFAULT 'India',
    postal_code VARCHAR(20),
    phone VARCHAR(20),
    email VARCHAR(255),
    status ENUM('active', 'inactive') DEFAULT 'active',
    created_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3)
);

-- 3. Doctors
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id CHAR(36) PRIMARY KEY,
    user_id CHAR(36) NOT NULL UNIQUE,
    hospital_id CHAR(36) NOT NULL,
    registration_number VARCHAR(100) NOT NULL UNIQUE,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    qualification VARCHAR(255),
    experience_years INT DEFAULT 0,
    consultation_fee DECIMAL(10,2) DEFAULT 0.00,
    timezone VARCHAR(100) DEFAULT 'Asia/Kolkata',
    profile_status ENUM('pending', 'active', 'inactive')
        DEFAULT 'pending',
    created_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    CONSTRAINT fk_doctors_user
        FOREIGN KEY (user_id) REFERENCES users(user_id),

    CONSTRAINT fk_doctors_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
);

-- 4. Patients
CREATE TABLE IF NOT EXISTS patients (
    patient_id CHAR(36) PRIMARY KEY,
    user_id CHAR(36) NOT NULL UNIQUE,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100),
    phone VARCHAR(20),
    date_of_birth DATE,
    gender ENUM('male', 'female', 'other', 'prefer_not_to_say'),
    created_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    CONSTRAINT fk_patients_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 5. Specialisations
CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id CHAR(36) PRIMARY KEY,
    name VARCHAR(150) NOT NULL UNIQUE,
    description VARCHAR(500)
);

-- 6. Doctor Specialisations
CREATE TABLE IF NOT EXISTS doctor_specialisations (
    doctor_id CHAR(36) NOT NULL,
    specialisation_id CHAR(36) NOT NULL,

    PRIMARY KEY (doctor_id, specialisation_id),

    CONSTRAINT fk_ds_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id),

    CONSTRAINT fk_ds_specialisation
        FOREIGN KEY (specialisation_id)
        REFERENCES specialisations(specialisation_id)
);

-- 7. Consultation Types
CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id CHAR(36) PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500),
    default_duration_minutes INT NOT NULL DEFAULT 30,
    status ENUM('active', 'inactive') DEFAULT 'active'
);

-- 8. Appointments
CREATE TABLE IF NOT EXISTS appointments (
    appointment_id CHAR(36) PRIMARY KEY,
    patient_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    hospital_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    slot_id VARCHAR(255) NOT NULL,
    appointment_start DATETIME(3) NOT NULL,
    appointment_end DATETIME(3) NOT NULL,
    status ENUM(
        'booked',
        'cancelled',
        'completed',
        'rescheduled'
    ) DEFAULT 'booked',
    idempotency_key VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),

    CONSTRAINT fk_appointments_patient
        FOREIGN KEY (patient_id) REFERENCES patients(patient_id),

    CONSTRAINT fk_appointments_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id),

    CONSTRAINT fk_appointments_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id),

    CONSTRAINT fk_appointments_consultation
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types(consultation_type_id),

    UNIQUE KEY uq_patient_idempotency (patient_id, idempotency_key),
    INDEX idx_doctor_appointment_time (doctor_id, appointment_start),
    INDEX idx_patient_appointments (patient_id, appointment_start),
    INDEX idx_hospital_city_lookup (hospital_id)
);
