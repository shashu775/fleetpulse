-- db/init.sql
--
-- Runs automatically the FIRST time the Postgres container starts.
-- On AWS RDS you run this once by hand.
--
-- Everything is IF NOT EXISTS so re-running is safe.
-- If you CHANGE this file, the container will not re-run it -- you need:
--     docker compose down -v && docker compose up
--
-- ...OR, because every statement here is idempotent, apply it in place against
-- a database you do not want to wipe:
--     docker compose exec -T postgres psql -U fleetadmin -d fleetpulse < db/init.sql
-- That is the no-data-loss path for picking up the dispatch.drivers roster.

-- Two schemas in ONE database. Cheap now, and if the services ever split
-- apart it is a `pg_dump --schema=dispatch` rather than untangling shared
-- tables. Each service writes only to its own schema.
CREATE SCHEMA IF NOT EXISTS consignment;
CREATE SCHEMA IF NOT EXISTS dispatch;


-- ============================================================
-- CONSIGNMENT: the parcel and its journey
-- Owned by consignment-service. Nothing else may write here.
-- ============================================================

CREATE TABLE IF NOT EXISTS consignment.waybills (
    awb              VARCHAR(20)  PRIMARY KEY,          -- e.g. "FP4820193756"
    merchant_name    VARCHAR(120) NOT NULL,
    consignee_name   VARCHAR(120) NOT NULL,
    consignee_phone  VARCHAR(20)  NOT NULL,
    consignee_addr   TEXT         NOT NULL,
    origin_hub       VARCHAR(40)  NOT NULL,             -- "HUB-BLR-01"
    destination_hub  VARCHAR(40)  NOT NULL,
    weight_grams     INTEGER      NOT NULL,
    payment_mode     VARCHAR(10)  NOT NULL,             -- PREPAID | COD
    cod_amount       NUMERIC(10,2) NOT NULL DEFAULT 0,
    -- Denormalised for fast reads. `scan_events` below is the real truth and
    -- could rebuild this column at any time.
    current_status   VARCHAR(30)  NOT NULL DEFAULT 'MANIFESTED',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- APPEND-ONLY. We never UPDATE or DELETE rows here, which is what makes the
-- tracking history trustworthy as an audit trail.
CREATE TABLE IF NOT EXISTS consignment.scan_events (
    id          BIGSERIAL   PRIMARY KEY,
    awb         VARCHAR(20) NOT NULL REFERENCES consignment.waybills(awb),
    status      VARCHAR(30) NOT NULL,
    hub_id      VARCHAR(40),
    remarks     TEXT,
    scanned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Serves "show me this parcel's history", the second-most-common query.
CREATE INDEX IF NOT EXISTS idx_scan_awb_time
    ON consignment.scan_events (awb, scanned_at DESC);


-- ============================================================
-- DISPATCH: drivers, runsheets, last mile
-- Owned by dispatch-service. Nothing else may write here.
-- ============================================================

-- The roster: who works out of which hub, and what they drive.
--
-- This exists because GET /api/v1/drivers used to derive its list from
-- `runsheets` -- meaning a driver only existed AFTER someone had already given
-- them a runsheet. On a fresh database the driver app's login picker was empty
-- and there was no way in. A roster is the thing you assign work FROM, so it
-- has to come first.
--
-- vehicle_id lives here rather than in its own table: it is 1:1 with the driver
-- at this scale, and `runsheets` already denormalises both. A real fleet has
-- vehicles moving between drivers, at which point this column becomes a FK to a
-- `dispatch.vehicles` table -- the shape below does not block that.
CREATE TABLE IF NOT EXISTS dispatch.drivers (
    driver_id   VARCHAR(40)  PRIMARY KEY,               -- "DRV-4417"
    driver_name VARCHAR(120) NOT NULL,
    vehicle_id  VARCHAR(40)  NOT NULL,
    hub_id      VARCHAR(40)  NOT NULL,                  -- the hub they report to
    phone       VARCHAR(20),
    -- ACTIVE drivers are offered for new runsheets; INACTIVE ones are kept so
    -- their historical runsheets still resolve to a name. Never DELETE a driver.
    status      VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The admin console's query: "who can I assign at this hub right now?"
CREATE INDEX IF NOT EXISTS idx_drivers_hub_status
    ON dispatch.drivers (hub_id, status);

-- One vehicle, one driver, enforced. Two drivers sharing a plate would make the
-- GPS store ambiguous -- `vehicle:<id>:location` is a single Redis key, so the
-- second driver's pings would silently overwrite the first's.
CREATE UNIQUE INDEX IF NOT EXISTS idx_drivers_vehicle
    ON dispatch.drivers (vehicle_id);

-- Seed roster: 3 drivers per hub, 18 total.
--
-- Plates carry the state code of the city the hub is in (KA Bangalore, TN
-- Chennai, TS Hyderabad, DL Delhi, MH Mumbai, WB Kolkata), so a vehicle_id on
-- a map or in a log tells you where it belongs without a lookup.
--
-- ON CONFLICT DO NOTHING makes this re-runnable, which is what lets the same
-- statement serve as the migration for an existing database (see below).
INSERT INTO dispatch.drivers (driver_id, driver_name, vehicle_id, hub_id, phone) VALUES
    -- HUB-BLR-01  Bangalore
    ('DRV-4417', 'Suresh Yadav',    'KA01AB1234', 'HUB-BLR-01', '9845012301'),
    ('DRV-4418', 'Girish Hegde',    'KA01AB5567', 'HUB-BLR-01', '9845012302'),
    ('DRV-4419', 'Lakshmi Bai',     'KA05CJ7781', 'HUB-BLR-01', '9845012303'),
    -- HUB-CHN-02  Chennai
    ('DRV-2201', 'Karthik Raja',    'TN09BX4410', 'HUB-CHN-02', '9840012301'),
    ('DRV-2202', 'Devi Priya',      'TN09BX6632', 'HUB-CHN-02', '9840012302'),
    ('DRV-2203', 'Murugan S',       'TN11CE2098', 'HUB-CHN-02', '9840012303'),
    -- HUB-HYD-01  Hyderabad
    ('DRV-3301', 'Naveen Reddy',    'TS07DK3345', 'HUB-HYD-01', '9848012301'),
    ('DRV-3302', 'Farhan Ali',      'TS07DK9987', 'HUB-HYD-01', '9848012302'),
    ('DRV-3303', 'Swathi Rao',      'TS08FN1120', 'HUB-HYD-01', '9848012303'),
    -- HUB-DEL-03  Delhi
    ('DRV-8823', 'Manoj Singh',     'DL03CD5678', 'HUB-DEL-03', '9811012301'),
    ('DRV-8824', 'Rakesh Tomar',    'DL03CD1145', 'HUB-DEL-03', '9811012302'),
    ('DRV-8825', 'Simran Kaur',     'DL08GT7734', 'HUB-DEL-03', '9811012303'),
    -- HUB-MUM-01  Mumbai
    ('DRV-1192', 'Vijay Kumar',     'MH02EF9012', 'HUB-MUM-01', '9820012301'),
    ('DRV-1193', 'Sachin Pawar',    'MH02EF3367', 'HUB-MUM-01', '9820012302'),
    ('DRV-1194', 'Ayesha Shaikh',   'MH04HL5521', 'HUB-MUM-01', '9820012303'),
    -- HUB-KOL-02  Kolkata
    ('DRV-6601', 'Bikash Ghosh',    'WB06JR8890', 'HUB-KOL-02', '9830012301'),
    ('DRV-6602', 'Tapan Mondal',    'WB06JR2214', 'HUB-KOL-02', '9830012302'),
    ('DRV-6603', 'Rina Dutta',      'WB20LM4407', 'HUB-KOL-02', '9830012303')
ON CONFLICT (driver_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS dispatch.runsheets (
    id          VARCHAR(40)  PRIMARY KEY,               -- "RS-20260813-01-417"
    -- Deliberately NOT a FK to dispatch.drivers, and the name and vehicle are
    -- copied rather than joined. A runsheet is a historical record of who drove
    -- what on a given day: if a driver later changes vehicle or leaves, last
    -- week's runsheet must still read the way it did when it was worked.
    driver_id   VARCHAR(40)  NOT NULL,
    driver_name VARCHAR(120) NOT NULL,
    vehicle_id  VARCHAR(40)  NOT NULL,
    hub_id      VARCHAR(40)  NOT NULL,
    status      VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- What is actually ON a runsheet. Without this, a driver app cannot answer
-- "what am I delivering today?" -- delivery_attempts only records parcels that
-- have already been attempted, which is the wrong end of the workflow.
CREATE TABLE IF NOT EXISTS dispatch.runsheet_items (
    runsheet_id  VARCHAR(40) NOT NULL REFERENCES dispatch.runsheets(id),
    awb          VARCHAR(20) NOT NULL,      -- no FK: cross-schema, see below
    sequence     INTEGER     NOT NULL,      -- delivery order on the route
    status       VARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- PENDING|DELIVERED|RTO
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (runsheet_id, awb)
);

-- The driver app's main query: "my pending stops, in route order".
CREATE INDEX IF NOT EXISTS idx_items_runsheet_status
    ON dispatch.runsheet_items (runsheet_id, status, sequence);

CREATE TABLE IF NOT EXISTS dispatch.delivery_attempts (
    id           BIGSERIAL   PRIMARY KEY,
    -- Deliberately NO foreign key to consignment.waybills. A real FK would
    -- create a hard database-level coupling between the two services and make
    -- splitting them later impossible. The AWB is validated over HTTP instead.
    awb          VARCHAR(20) NOT NULL,
    runsheet_id  VARCHAR(40) NOT NULL REFERENCES dispatch.runsheets(id),
    outcome      VARCHAR(20) NOT NULL,                  -- DELIVERED | RTO
    reason       TEXT,
    -- Proof of delivery, captured by the driver app.
    pod_type     VARCHAR(20),                           -- OTP | SIGNATURE | PHOTO
    pod_receiver VARCHAR(120),                          -- who actually took it
    -- Signature strokes as a data URL. Fine at this scale; a real system would
    -- put the image in S3 and store only the key.
    pod_data     TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attempts_awb
    ON dispatch.delivery_attempts (awb);


-- ============================================================
-- NOTE: there is deliberately NO gps_pings table.
--
-- 100 vehicles pinging every 10 seconds is ~864,000 writes/day, and only the
-- newest ping for each vehicle has any value. That workload would consume the
-- entire IO budget of a db.t3.micro for data that is stale in ten seconds.
--
-- GPS lives in Redis: one key per vehicle, 1-hour TTL, so a vehicle that stops
-- reporting expires on its own with no cleanup job. The POST /api/v1/gps
-- endpoint returns 202 Accepted (not 201) to be honest that the write is
-- deliberately non-durable.
-- ============================================================
