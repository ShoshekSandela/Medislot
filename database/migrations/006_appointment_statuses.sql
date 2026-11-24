-- MediSlot appointment lifecycle status migration.
-- Run once after 005_booking_engine.sql against the existing medislot database.

USE medislot;

ALTER TABLE appointments
  MODIFY COLUMN status ENUM(
    'BOOKED','CANCELLED','RESCHEDULED','COMPLETED',
    'CONFIRMED','CANCELLED_BY_PATIENT','CANCELLED_BY_DOCTOR','NO_SHOW'
  ) NOT NULL DEFAULT 'BOOKED';

UPDATE appointments
SET status = 'CONFIRMED'
WHERE status IN ('BOOKED', 'RESCHEDULED');

UPDATE appointments a
LEFT JOIN refund_ledger r ON r.appointment_id = a.appointment_id
SET a.status = CASE
  WHEN r.cancellation_type = 'DOCTOR' THEN 'CANCELLED_BY_DOCTOR'
  ELSE 'CANCELLED_BY_PATIENT'
END
WHERE a.status = 'CANCELLED';

ALTER TABLE appointments
  MODIFY COLUMN status ENUM(
    'CONFIRMED','COMPLETED','CANCELLED_BY_PATIENT',
    'CANCELLED_BY_DOCTOR','NO_SHOW'
  ) NOT NULL DEFAULT 'CONFIRMED';
