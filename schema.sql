-- Smart Parking Lot Allocation & Billing System  |  MySQL 8.0.16+
-- Run:  mysql -u root -p < schema.sql
DROP DATABASE IF EXISTS parking_db;
CREATE DATABASE parking_db CHARACTER SET utf8mb4;
USE parking_db;

-- ---------- Infrastructure ----------
CREATE TABLE facility (
  facility_id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  location VARCHAR(100) NOT NULL,
  total_capacity INT NOT NULL
);
CREATE TABLE floor (
  floor_id INT AUTO_INCREMENT PRIMARY KEY,
  facility_id INT NOT NULL,
  floor_number INT NOT NULL,
  FOREIGN KEY (facility_id) REFERENCES facility(facility_id) ON DELETE CASCADE
);
CREATE TABLE zone (
  zone_id INT AUTO_INCREMENT PRIMARY KEY,
  floor_id INT NOT NULL,
  zone_name VARCHAR(20) NOT NULL,
  FOREIGN KEY (floor_id) REFERENCES floor(floor_id) ON DELETE CASCADE
);
CREATE TABLE vehicle_type (
  vehicle_type_id INT AUTO_INCREMENT PRIMARY KEY,
  type_name VARCHAR(50) NOT NULL UNIQUE,
  base_multiplier DECIMAL(3,2) NOT NULL DEFAULT 1.00
);
CREATE TABLE slot (
  slot_id INT AUTO_INCREMENT PRIMARY KEY,
  zone_id INT NOT NULL,
  vehicle_type_id INT NOT NULL,
  slot_number VARCHAR(20) NOT NULL UNIQUE,
  status ENUM('AVAILABLE','RESERVED','OCCUPIED','MAINTENANCE') NOT NULL DEFAULT 'AVAILABLE',
  FOREIGN KEY (zone_id) REFERENCES zone(zone_id),
  FOREIGN KEY (vehicle_type_id) REFERENCES vehicle_type(vehicle_type_id)
);

-- ---------- People & vehicles ----------
CREATE TABLE customer (
  customer_id INT AUTO_INCREMENT PRIMARY KEY,
  full_name VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL UNIQUE,
  phone VARCHAR(20) NOT NULL UNIQUE,
  registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE vehicle (
  vehicle_id INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT NOT NULL,
  vehicle_type_id INT NOT NULL,
  license_plate VARCHAR(20) NOT NULL UNIQUE,
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id) ON DELETE CASCADE,
  FOREIGN KEY (vehicle_type_id) REFERENCES vehicle_type(vehicle_type_id)
);

-- ---------- Pricing ----------
CREATE TABLE tariff (
  tariff_id INT AUTO_INCREMENT PRIMARY KEY,
  vehicle_type_id INT NOT NULL UNIQUE,
  base_rate DECIMAL(8,2) NOT NULL,
  hourly_rate DECIMAL(8,2) NOT NULL,
  grace_mins INT NOT NULL DEFAULT 15,
  FOREIGN KEY (vehicle_type_id) REFERENCES vehicle_type(vehicle_type_id)
);

-- ---------- Reservations & sessions ----------
CREATE TABLE reservation (
  reservation_id INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT NOT NULL,
  vehicle_id INT NOT NULL,
  slot_id INT NOT NULL,
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP NOT NULL,
  status ENUM('PENDING','ACTIVE','COMPLETED','CANCELLED') NOT NULL DEFAULT 'PENDING',
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id),
  FOREIGN KEY (vehicle_id) REFERENCES vehicle(vehicle_id),
  FOREIGN KEY (slot_id) REFERENCES slot(slot_id),
  CONSTRAINT chk_res_window CHECK (end_time > start_time)
);
CREATE TABLE parking_session (
  session_id INT AUTO_INCREMENT PRIMARY KEY,
  vehicle_id INT NOT NULL,
  slot_id INT NOT NULL,
  reservation_id INT NULL,
  entry_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  exit_time TIMESTAMP NULL,
  status ENUM('ACTIVE','COMPLETED') NOT NULL DEFAULT 'ACTIVE',
  -- 1:1 allocation lock: only one ACTIVE session per slot and per vehicle
  active_slot_id INT GENERATED ALWAYS AS (IF(status='ACTIVE', slot_id, NULL)) STORED,
  active_vehicle_id INT GENERATED ALWAYS AS (IF(status='ACTIVE', vehicle_id, NULL)) STORED,
  UNIQUE KEY uq_active_slot (active_slot_id),
  UNIQUE KEY uq_active_vehicle (active_vehicle_id),
  FOREIGN KEY (vehicle_id) REFERENCES vehicle(vehicle_id),
  FOREIGN KEY (slot_id) REFERENCES slot(slot_id),
  FOREIGN KEY (reservation_id) REFERENCES reservation(reservation_id),
  CONSTRAINT chk_exit_after_entry CHECK (exit_time IS NULL OR exit_time >= entry_time)
);

-- ---------- Billing ----------
CREATE TABLE bill (
  bill_id INT AUTO_INCREMENT PRIMARY KEY,
  session_id INT NOT NULL UNIQUE,
  tariff_id INT NOT NULL,
  total_duration_mins INT NOT NULL,
  total_amount DECIMAL(10,2) NOT NULL,
  status ENUM('UNPAID','PAID') NOT NULL DEFAULT 'UNPAID',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (session_id) REFERENCES parking_session(session_id),
  FOREIGN KEY (tariff_id) REFERENCES tariff(tariff_id)
);
CREATE TABLE payment (
  payment_id INT AUTO_INCREMENT PRIMARY KEY,
  bill_id INT NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  payment_method ENUM('CASH','CARD','UPI') NOT NULL,
  transaction_ref VARCHAR(100) NULL UNIQUE,
  paid_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (bill_id) REFERENCES bill(bill_id)
);
CREATE TABLE pass (
  pass_id INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT NOT NULL,
  vehicle_id INT NOT NULL,
  pass_type ENUM('DAILY','MONTHLY','ANNUAL') NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id),
  FOREIGN KEY (vehicle_id) REFERENCES vehicle(vehicle_id),
  CONSTRAINT chk_pass_dates CHECK (end_date >= start_date)
);

-- ---------- Users & Authentication ----------
CREATE TABLE app_user (
  user_id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(50) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role ENUM('Admin','Attendant','Customer') NOT NULL,
  customer_id INT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id) ON DELETE SET NULL
);

-- ---------- Seed data ----------
INSERT INTO facility (name, location, total_capacity) VALUES ('Central Plaza Parking', 'Downtown', 48);
INSERT INTO floor (facility_id, floor_number) VALUES (1,1),(1,2);
INSERT INTO zone (floor_id, zone_name) VALUES (1,'A'),(1,'B'),(2,'C'),(2,'D');
INSERT INTO vehicle_type (type_name, base_multiplier) VALUES
  ('Compact',1.00),('SUV',1.50),('EV',1.20),('Handicapped',1.00);
INSERT INTO slot (zone_id, vehicle_type_id, slot_number)
SELECT z.zone_id,
       CASE WHEN n.i<=6 THEN 1 WHEN n.i<=9 THEN 2 WHEN n.i<=11 THEN 3 ELSE 4 END,
       CONCAT(z.zone_name,'-',LPAD(n.i,2,'0'))
FROM zone z
CROSS JOIN (SELECT 1 i UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6
            UNION SELECT 7 UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11 UNION SELECT 12) n;
INSERT INTO tariff (vehicle_type_id, base_rate, hourly_rate, grace_mins) VALUES
  (1,20,15,15),(2,30,20,15),(3,25,15,15),(4,10,10,30);
INSERT INTO customer (full_name, email, phone) VALUES
  ('Aarav Sharma','aarav@example.com','9000000001'),
  ('Diya Reddy','diya@example.com','9000000002'),
  ('Kabir Khan','kabir@example.com','9000000003');
INSERT INTO vehicle (customer_id, vehicle_type_id, license_plate) VALUES
  (1,1,'TS09AB1234'),(2,2,'TS10CD5678'),(3,3,'AP28EV9090'),(1,2,'TS09XY4321');
INSERT INTO app_user (username, password_hash, role, customer_id) VALUES
  ('admin', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9', 'Admin', NULL),
  ('attendant', 'ce6e1792f6b1062ad3f2b53acd5987a302fc07b4aff20363b4d4bafe67ab4ac7', 'Attendant', NULL),
  ('aarav', 'b041c0aeb35bb0fa4aa668ca5a920b590196fdaf9a00eb852c9b7f4d123cc6d6', 'Customer', 1),
  ('diya', 'b041c0aeb35bb0fa4aa668ca5a920b590196fdaf9a00eb852c9b7f4d123cc6d6', 'Customer', 2),
  ('kabir', 'b041c0aeb35bb0fa4aa668ca5a920b590196fdaf9a00eb852c9b7f4d123cc6d6', 'Customer', 3);

-- ---------- Scheduled Event for Auto-Expiry ----------
SET GLOBAL event_scheduler = ON;

DROP EVENT IF EXISTS evt_expire_reservations;
DELIMITER //
CREATE EVENT evt_expire_reservations
ON SCHEDULE EVERY 1 MINUTE
DO
BEGIN
  UPDATE reservation SET status='CANCELLED'
  WHERE status='PENDING' AND (start_time + INTERVAL 15 MINUTE < NOW() OR end_time < NOW());

  UPDATE slot s SET status='AVAILABLE'
  WHERE status='RESERVED' AND NOT EXISTS (
    SELECT 1 FROM reservation r WHERE r.slot_id=s.slot_id AND r.status='PENDING'
  );
END //
DELIMITER ;

