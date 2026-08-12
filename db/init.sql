-- db/init.sql
--
-- Runs automatically the FIRST time the Postgres container starts.
-- On AWS RDS you run this once by hand.
--
-- Everything is IF NOT EXISTS so re-running is safe.
-- If you CHANGE this file, the container will not re-run it -- you need:
--     docker compose down -v && docker compose up

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

CREATE TABLE IF NOT EXISTS dispatch.runsheets (
    id          VARCHAR(40)  PRIMARY KEY,               -- "RS-20260813-01-417"
    driver_id   VARCHAR(40)  NOT NULL,
    driver_name VARCHAR(120) NOT NULL,
    vehicle_id  VARCHAR(40)  NOT NULL,
    hub_id      VARCHAR(40)  NOT NULL,
    status      VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dispatch.delivery_attempts (
    id           BIGSERIAL   PRIMARY KEY,
    -- Deliberately NO foreign key to consignment.waybills. A real FK would
    -- create a hard database-level coupling between the two services and make
    -- splitting them later impossible. The AWB is validated over HTTP instead.
    awb          VARCHAR(20) NOT NULL,
    runsheet_id  VARCHAR(40) NOT NULL REFERENCES dispatch.runsheets(id),
    outcome      VARCHAR(20) NOT NULL,                  -- DELIVERED | RTO
    reason       TEXT,
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
