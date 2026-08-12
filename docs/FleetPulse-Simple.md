# FleetPulse Simple

A beginner-friendly, zero-cost DevOps portfolio project: two REST microservices modelling
Delhivery's parcel operations, deployed to AWS with Terraform and GitHub Actions.

**No message brokers. No Kubernetes. No service mesh.** Two services, one database, one EC2 box.

---

## Start here: why this version is the right one

If you have read the other documents in this folder, put them aside for now. They describe a
production system for a team, and they would take months. **This plan takes 2–3 weeks and you will
finish it** — which matters far more for a portfolio.

Here is the concrete reason your simplification was a good engineering call, not a compromise:

```
t3.micro = 1024 MB total (~958 MB actually usable)

The Kubernetes plan                    This plan
────────────────────────               ─────────────────────────
OS ................. 150 MB            OS ................. 150 MB
K3s control plane .. 500 MB  ← ouch    Docker daemon ....... 80 MB
4 services ......... 140 MB            consignment-service . 70 MB
broker + redis ..... 190 MB            dispatch-service .... 70 MB
Prometheus+Grafana . 380 MB            Redis (capped) ...... 50 MB
────────────────────────               ─────────────────────────
TOTAL ............ 1,360 MB  ✗         TOTAL .............. 420 MB  ✓
                                       HEADROOM ........... 538 MB
```

**Dropping Kubernetes is what buys the headroom — not dropping services.** A K3s control plane costs
500 MB before you run a single container of your own. On a 1 GB free-tier box that is half your
memory spent on orchestration you do not yet need.

When an interviewer asks why you did not use Kubernetes, that table is your answer. "I sized the
workload against the instance and Kubernetes did not fit the budget" is a *better* answer than
"I used Kubernetes because everyone does."

### What you will still learn — which is most of what matters

| Skill | Where you practise it |
|---|---|
| Containerisation & multi-stage builds | Phase 1 |
| Docker Compose orchestration | Phase 1 |
| Service-to-service HTTP communication | Phase 1 |
| Caching strategy with Redis | Phase 1 |
| CI/CD pipelines | Phase 2 |
| Container registries | Phase 2 |
| Automated deployment | Phase 2 |
| Infrastructure as Code | Phase 3 |
| VPC, subnets, security groups | Phase 3 |
| Managed databases | Phase 3 |
| Cloud cost control | Phase 3 |

That is a genuinely strong junior DevOps portfolio. Kubernetes can be your *next* project, and
you will learn it faster having built this one first.

---

## 1. Architecture

### 1.1 The picture

```mermaid
flowchart TB
    U["You / Postman /<br/>simulate_delhivery_scans.py"]

    subgraph EC2["AWS EC2 t3.micro — Docker Compose"]
        CS["Consignment &amp; Hub Service<br/>:8001<br/>AWB booking · labels · hub scans"]
        DS["Fleet &amp; Dispatch Service<br/>:8002<br/>runsheets · GPS · delivery"]
        RD[("Redis<br/>status cache +<br/>live GPS")]
    end

    RDS[("AWS RDS PostgreSQL<br/>db.t3.micro<br/>schemas: consignment, dispatch")]

    U -->|HTTP| CS
    U -->|HTTP| DS
    DS -->|"HTTP REST (httpx)<br/>verify AWB · update status"| CS
    CS --> RD
    DS --> RD
    CS --> RDS
    DS --> RDS
```

Only one arrow between the services, and it points one way: **Dispatch calls Consignment.** Dispatch
needs to check an AWB exists and tell Consignment when status changes. Consignment never calls
Dispatch. Keeping the dependency one-directional avoids circular calls, which are a genuine
headache once they appear.

### 1.2 The two services

**Consignment & Hub Service** — owns the parcel and its journey through facilities.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/v1/waybills` | Book a shipment, generate an AWB number |
| `GET` | `/api/v1/waybills/{awb}` | Current status (Redis first, then DB) |
| `GET` | `/api/v1/waybills/{awb}/label` | Shipping label as HTML |
| `GET` | `/api/v1/waybills/{awb}/history` | Full scan history |
| `POST` | `/api/v1/scans` | Hub scan: `MANIFESTED` / `IN_TRANSIT` / `ARRIVED_AT_FACILITY` |
| `PATCH` | `/api/v1/waybills/{awb}/status` | Internal — called by Dispatch |
| `GET` | `/health` | Liveness |

**Fleet & Dispatch Service** — owns drivers, vehicles, and the last mile.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/v1/runsheets` | Assign parcels to a driver → `OUT_FOR_DELIVERY` |
| `GET` | `/api/v1/runsheets/{id}` | Runsheet with its parcels |
| `POST` | `/api/v1/gps` | Vehicle GPS ping → Redis |
| `GET` | `/api/v1/vehicles/{id}/location` | Last known position |
| `POST` | `/api/v1/delivery` | Final outcome: `DELIVERED` or `RTO` |
| `GET` | `/health` | Liveness |

### 1.3 Parcel status flow (Delhivery's actual vocabulary)

```mermaid
stateDiagram-v2
    [*] --> MANIFESTED: booking created
    MANIFESTED --> IN_TRANSIT: picked up, moving between hubs
    IN_TRANSIT --> ARRIVED_AT_FACILITY: inbound hub scan
    ARRIVED_AT_FACILITY --> IN_TRANSIT: onward to next hub
    ARRIVED_AT_FACILITY --> OUT_FOR_DELIVERY: added to a runsheet
    OUT_FOR_DELIVERY --> DELIVERED: POD captured
    OUT_FOR_DELIVERY --> RTO: undeliverable, returning to origin
    DELIVERED --> [*]
    RTO --> [*]
```

Using the real terms costs nothing and makes the project read as domain-aware.

### 1.4 Technology choice: Python + FastAPI

You offered Node/Express or Python/FastAPI. **Pick FastAPI**, for three reasons:

1. **Free interactive API docs.** FastAPI generates a Swagger UI at `/docs` automatically. For a
   portfolio project this is genuinely valuable — a recruiter can click through your API in a
   browser. That alone is worth the choice.
2. **One language everywhere.** Your traffic simulator is Python. Same language for services and
   tooling means one set of tools to learn, not two.
3. **Automatic validation.** Pydantic models validate request bodies and return clear 422 errors
   without you writing any checking code.

If you already know JavaScript well, Express is a perfectly fine substitute — the architecture,
Docker setup, CI/CD, and Terraform are all identical. Do not switch languages just because a
document said so.

### 1.5 The shared database — and the one honest caveat

You asked for a single shared PostgreSQL database, and for two services that is the right call.
Two databases would mean two connection strings, two migration tools, and a distributed-transaction
problem you do not need yet.

**One small thing that costs you nothing now and helps a lot later: use two Postgres schemas inside
that one database.**

```sql
CREATE SCHEMA consignment;   -- waybills, scan_events
CREATE SCHEMA dispatch;      -- runsheets, delivery_attempts
```

Same database, same connection, same free-tier RDS instance. But the ownership boundary is visible,
and if you ever split the services apart, it is a `pg_dump --schema=dispatch` rather than untangling
which of forty tables belonged to whom.

It also gives you a good interview answer: *"I used a shared database because two services didn't
justify the overhead, but I separated schemas so the ownership boundary stayed explicit and a future
split would be mechanical."* That shows judgement, which is what the question is actually testing.

### 1.6 What Redis is for

Two jobs, both genuinely useful:

1. **Tracking status cache.** `GET /waybills/{awb}` is the most-called endpoint in any logistics
   system — customers refresh tracking pages constantly. Cache the status in Redis with a 5-minute
   TTL and invalidate it on every scan. You will be able to *show* the cache working in your metrics.
2. **Live GPS position.** GPS pings arrive constantly and only the newest one matters. Writing every
   ping to Postgres would hammer a `db.t3.micro` for no benefit. Redis holds the last known position
   per vehicle; Postgres never sees them.

That second one is a real architectural decision with a real justification — "high-write,
low-durability data does not belong in your relational database" — and it is worth being able to
explain.

---

## 2. Phase 1 — Local Setup with Docker Compose

### 2.1 Folder structure

```
fleetpulse/
├── README.md
├── .env.example                       # config contract — copy to .env
├── .gitignore
├── docker-compose.yml                 # ⬅ local dev: everything, one command
├── docker-compose.prod.yml            # ⬅ on EC2: pulls from ECR, uses RDS
│
├── services/
│   ├── consignment-service/
│   │   ├── app/
│   │   │   ├── main.py                # FastAPI app + routes
│   │   │   ├── models.py              # Pydantic request/response models
│   │   │   ├── db.py                  # database connection + queries
│   │   │   ├── cache.py               # Redis helpers
│   │   │   └── labels.py              # shipping label HTML
│   │   ├── tests/
│   │   │   └── test_waybills.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   │
│   └── dispatch-service/
│       ├── app/
│       │   ├── main.py
│       │   ├── models.py
│       │   ├── db.py
│       │   ├── cache.py
│       │   └── consignment_client.py  # ⬅ the HTTP call to the other service
│       ├── tests/
│       │   └── test_runsheets.py
│       ├── requirements.txt
│       └── Dockerfile
│
├── db/
│   └── init.sql                       # schemas + tables, runs on first boot
│
├── simulator/
│   ├── simulate_delhivery_scans.py
│   └── requirements.txt
│
├── infra/
│   └── terraform/
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       └── user_data.sh
│
└── .github/
    └── workflows/
        └── deploy.yml
```

> **About your current repo:** you have four service folders (`facility-service`,
> `notification-service`) that this plan does not use. Delete them — an empty folder in a portfolio
> repo looks unfinished. `git rm -r services/facility-service services/notification-service`.
> You can always add them back later.

### 2.2 `db/init.sql`

```sql
-- db/init.sql
-- Runs automatically the FIRST time the Postgres container starts.
-- On AWS RDS you run this once by hand (Milestone 4).

-- Two schemas in one database: cheap now, useful if you ever split the services.
CREATE SCHEMA IF NOT EXISTS consignment;
CREATE SCHEMA IF NOT EXISTS dispatch;

-- ============================================================
-- CONSIGNMENT: the parcel and its journey
-- ============================================================
CREATE TABLE IF NOT EXISTS consignment.waybills (
    awb              VARCHAR(20) PRIMARY KEY,     -- e.g. "FP1234567890"
    merchant_name    VARCHAR(120) NOT NULL,
    consignee_name   VARCHAR(120) NOT NULL,
    consignee_phone  VARCHAR(20)  NOT NULL,
    consignee_addr   TEXT         NOT NULL,
    origin_hub       VARCHAR(40)  NOT NULL,       -- "HUB-BLR-01"
    destination_hub  VARCHAR(40)  NOT NULL,
    weight_grams     INTEGER      NOT NULL,
    payment_mode     VARCHAR(10)  NOT NULL,       -- PREPAID | COD
    cod_amount       NUMERIC(10,2) DEFAULT 0,
    current_status   VARCHAR(30)  NOT NULL DEFAULT 'MANIFESTED',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Every scan ever recorded. Append-only: we never UPDATE this table,
-- which makes it a reliable audit trail.
CREATE TABLE IF NOT EXISTS consignment.scan_events (
    id          BIGSERIAL PRIMARY KEY,
    awb         VARCHAR(20) NOT NULL REFERENCES consignment.waybills(awb),
    status      VARCHAR(30) NOT NULL,
    hub_id      VARCHAR(40),
    remarks     TEXT,
    scanned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Speeds up "show me this parcel's history", the second-most-common query.
CREATE INDEX IF NOT EXISTS idx_scan_awb_time
    ON consignment.scan_events (awb, scanned_at DESC);

-- ============================================================
-- DISPATCH: drivers, runsheets, last mile
-- ============================================================
CREATE TABLE IF NOT EXISTS dispatch.runsheets (
    id          VARCHAR(40) PRIMARY KEY,          -- "RS-20260813-BLR-07"
    driver_id   VARCHAR(40) NOT NULL,
    driver_name VARCHAR(120) NOT NULL,
    vehicle_id  VARCHAR(40) NOT NULL,
    hub_id      VARCHAR(40) NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dispatch.delivery_attempts (
    id           BIGSERIAL PRIMARY KEY,
    awb          VARCHAR(20) NOT NULL,            -- no FK: different schema owner
    runsheet_id  VARCHAR(40) NOT NULL REFERENCES dispatch.runsheets(id),
    outcome      VARCHAR(20) NOT NULL,            -- DELIVERED | RTO | FAILED
    reason       TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attempts_awb
    ON dispatch.delivery_attempts (awb);

-- NOTE: there is deliberately NO gps_pings table.
-- GPS is high-write, low-value, and only the newest ping matters,
-- so it lives in Redis. Writing it here would waste db.t3.micro's IO.
```

### 2.3 `docker-compose.yml` (local development)

```yaml
# docker-compose.yml
# Local development. One command brings up everything:
#     docker compose up --build
#
# Postgres and Redis run as containers here. On AWS, Postgres is RDS
# (see docker-compose.prod.yml).

services:
  # ----------------------------------------------------------
  # DATABASE — local only
  # ----------------------------------------------------------
  postgres:
    image: postgres:16-alpine
    container_name: fp-postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "5432:5432"                 # so you can connect with pgAdmin/DBeaver
    volumes:
      # Files in this folder run automatically on FIRST start only.
      # If you change init.sql, run: docker compose down -v
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      # Other services wait for this to pass before starting.
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 3s
      retries: 10

  # ----------------------------------------------------------
  # CACHE
  # ----------------------------------------------------------
  redis:
    image: redis:7-alpine
    container_name: fp-redis
    # maxmemory protects a 1 GB EC2 box: Redis will evict old keys
    # instead of growing until the kernel kills something.
    command: ["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10

  # ----------------------------------------------------------
  # SERVICE 1 — Consignment & Hub
  # ----------------------------------------------------------
  consignment-service:
    build: ./services/consignment-service
    container_name: fp-consignment
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      REDIS_URL: redis://redis:6379/0
      LOG_LEVEL: INFO
    ports:
      - "8001:8000"                 # host 8001 -> container 8000
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    restart: unless-stopped
    volumes:
      # Live code reload while developing. REMOVE this line in production.
      - ./services/consignment-service/app:/code/app

  # ----------------------------------------------------------
  # SERVICE 2 — Fleet & Dispatch
  # ----------------------------------------------------------
  dispatch-service:
    build: ./services/dispatch-service
    container_name: fp-dispatch
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      REDIS_URL: redis://redis:6379/0
      # How dispatch finds consignment. "consignment-service" is the
      # service name above — Docker's internal DNS resolves it.
      CONSIGNMENT_URL: http://consignment-service:8000
      LOG_LEVEL: INFO
    ports:
      - "8002:8000"
    depends_on:
      postgres:            { condition: service_healthy }
      redis:               { condition: service_healthy }
      consignment-service: { condition: service_started }
    restart: unless-stopped
    volumes:
      - ./services/dispatch-service/app:/code/app

volumes:
  pgdata:
```

```bash
cp .env.example .env       # then edit the passwords
docker compose up --build

# Open these in your browser:
#   http://localhost:8001/docs   <- Consignment API (interactive!)
#   http://localhost:8002/docs   <- Dispatch API
```

### 2.4 `.env.example`

```ini
# .env.example — copy to .env and edit. .env is gitignored; this file is not.
# Rule: when you add a new variable to the code, add it here in the same commit.

POSTGRES_USER=fleetadmin
POSTGRES_PASSWORD=change_me_locally
POSTGRES_DB=fleetpulse

REDIS_URL=redis://redis:6379/0
LOG_LEVEL=INFO

# Used only by docker-compose.prod.yml on EC2
ECR_REGISTRY=123456789012.dkr.ecr.us-east-1.amazonaws.com
IMAGE_TAG=latest
DATABASE_URL=postgresql://fleetadmin:CHANGEME@your-rds-endpoint.rds.amazonaws.com:5432/fleetpulse
```

### 2.5 `services/consignment-service/Dockerfile`

```dockerfile
# services/consignment-service/Dockerfile
# Multi-stage build: the "builder" stage compiles dependencies, and only the
# installed packages get copied into the final image. Smaller image =
# faster deploys and less of your 500 MB ECR free-tier allowance used.

# ---------- Stage 1: build dependencies ----------
FROM python:3.12-slim AS builder
WORKDIR /code
COPY requirements.txt .
# --user installs into /root/.local, which we copy out in stage 2.
RUN pip install --no-cache-dir --user -r requirements.txt

# ---------- Stage 2: the actual runtime image ----------
FROM python:3.12-slim
WORKDIR /code

# Don't run as root inside the container. If someone breaks in through
# your app, they land as an unprivileged user.
RUN useradd --create-home --shell /bin/bash appuser

COPY --from=builder /root/.local /home/appuser/.local
COPY ./app ./app

ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser
EXPOSE 8000

# Docker checks this; an unhealthy container shows up in `docker compose ps`.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`requirements.txt`:

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
psycopg[binary,pool]==3.2.1
redis==5.0.8
httpx==0.27.2
pydantic==2.9.0
pytest==8.3.2
```

### 2.6 Consignment Service — `app/main.py`

```python
# services/consignment-service/app/main.py
"""Consignment & Hub Service — AWB booking, labels, and hub scans."""

import os
import random
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .db import get_conn, init_pool
from .cache import cache_get, cache_set, cache_delete
from .models import BookingRequest, ScanRequest, StatusUpdate, WaybillResponse
from .labels import render_label

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("consignment")

app = FastAPI(
    title="FleetPulse — Consignment & Hub Service",
    description="Waybill booking, shipping labels, and warehouse hub scans.",
    version="1.0.0",
)

# Which status changes are allowed. Rejecting impossible transitions
# (e.g. DELIVERED -> MANIFESTED) catches bugs early and is easy to test.
ALLOWED_TRANSITIONS = {
    "MANIFESTED":          {"IN_TRANSIT"},
    "IN_TRANSIT":          {"ARRIVED_AT_FACILITY"},
    "ARRIVED_AT_FACILITY": {"IN_TRANSIT", "OUT_FOR_DELIVERY"},
    "OUT_FOR_DELIVERY":    {"DELIVERED", "RTO"},
    "DELIVERED":           set(),   # terminal
    "RTO":                 set(),   # terminal
}

CACHE_TTL_SECONDS = 300   # 5 minutes


@app.on_event("startup")
def startup() -> None:
    init_pool()
    log.info("consignment-service ready")


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Used by Docker's HEALTHCHECK and by you when debugging."""
    return {"status": "ok", "service": "consignment"}


def generate_awb() -> str:
    """Delhivery-style airway bill number, e.g. FP4820193756."""
    return f"FP{random.randint(10**9, 10**10 - 1)}"


@app.post("/api/v1/waybills", status_code=201, tags=["booking"])
def create_waybill(req: BookingRequest) -> dict:
    """Book a shipment and return its AWB number."""
    awb = generate_awb()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO consignment.waybills
                (awb, merchant_name, consignee_name, consignee_phone, consignee_addr,
                 origin_hub, destination_hub, weight_grams, payment_mode, cod_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (awb, req.merchant_name, req.consignee_name, req.consignee_phone,
             req.consignee_addr, req.origin_hub, req.destination_hub,
             req.weight_grams, req.payment_mode, req.cod_amount),
        )
        # Record the first scan so history is complete from the start.
        cur.execute(
            """INSERT INTO consignment.scan_events (awb, status, hub_id, remarks)
               VALUES (%s, 'MANIFESTED', %s, 'Shipment booked')""",
            (awb, req.origin_hub),
        )
        conn.commit()

    log.info("booked awb=%s origin=%s", awb, req.origin_hub)
    return {"awb": awb, "status": "MANIFESTED",
            "tracking_url": f"/api/v1/waybills/{awb}"}


@app.get("/api/v1/waybills/{awb}", tags=["tracking"])
def get_waybill(awb: str) -> dict:
    """Tracking lookup. Tries Redis first — this is the hot path."""
    cached = cache_get(f"awb:{awb}")
    if cached:
        cached["_cache"] = "HIT"     # visible in the response, handy for demos
        return cached

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT awb, merchant_name, consignee_name, consignee_addr,
                      origin_hub, destination_hub, current_status,
                      payment_mode, cod_amount, created_at, updated_at
               FROM consignment.waybills WHERE awb = %s""",
            (awb,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"AWB {awb} not found")

    result = {
        "awb": row[0], "merchant_name": row[1], "consignee_name": row[2],
        "consignee_addr": row[3], "origin_hub": row[4], "destination_hub": row[5],
        "current_status": row[6], "payment_mode": row[7],
        "cod_amount": float(row[8]), "created_at": row[9].isoformat(),
        "updated_at": row[10].isoformat(),
    }
    cache_set(f"awb:{awb}", result, CACHE_TTL_SECONDS)
    result["_cache"] = "MISS"
    return result


@app.get("/api/v1/waybills/{awb}/history", tags=["tracking"])
def get_history(awb: str) -> dict:
    """Every scan for this parcel, newest first."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT status, hub_id, remarks, scanned_at
               FROM consignment.scan_events
               WHERE awb = %s ORDER BY scanned_at DESC""",
            (awb,),
        )
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail=f"AWB {awb} not found")

    return {"awb": awb, "scans": [
        {"status": r[0], "hub_id": r[1], "remarks": r[2], "scanned_at": r[3].isoformat()}
        for r in rows
    ]}


@app.post("/api/v1/scans", status_code=201, tags=["hub-operations"])
def record_scan(req: ScanRequest) -> dict:
    """A warehouse hub scans a parcel: MANIFESTED / IN_TRANSIT / ARRIVED_AT_FACILITY."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_status FROM consignment.waybills WHERE awb = %s",
                    (req.awb,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"AWB {req.awb} not found")

        current = row[0]
        if req.status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot move from {current} to {req.status}",
            )

        cur.execute(
            """UPDATE consignment.waybills
               SET current_status = %s, updated_at = now() WHERE awb = %s""",
            (req.status, req.awb),
        )
        cur.execute(
            """INSERT INTO consignment.scan_events (awb, status, hub_id, remarks)
               VALUES (%s, %s, %s, %s)""",
            (req.awb, req.status, req.hub_id, req.remarks),
        )
        conn.commit()

    # The cached copy is now wrong — delete it so the next read re-fetches.
    cache_delete(f"awb:{req.awb}")

    log.info("scan awb=%s %s -> %s at %s", req.awb, current, req.status, req.hub_id)
    return {"awb": req.awb, "previous_status": current, "new_status": req.status}


@app.patch("/api/v1/waybills/{awb}/status", tags=["internal"])
def update_status(awb: str, req: StatusUpdate) -> dict:
    """Internal endpoint — called by dispatch-service over HTTP.

    In a bigger system this would be authenticated. For a learning project
    it is enough that it is not exposed publicly (see the security group).
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_status FROM consignment.waybills WHERE awb = %s", (awb,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"AWB {awb} not found")

        current = row[0]
        if req.status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise HTTPException(status_code=409,
                                detail=f"Cannot move from {current} to {req.status}")

        cur.execute("""UPDATE consignment.waybills
                       SET current_status = %s, updated_at = now() WHERE awb = %s""",
                    (req.status, awb))
        cur.execute("""INSERT INTO consignment.scan_events (awb, status, hub_id, remarks)
                       VALUES (%s, %s, %s, %s)""",
                    (awb, req.status, req.hub_id, req.remarks))
        conn.commit()

    cache_delete(f"awb:{awb}")
    return {"awb": awb, "previous_status": current, "new_status": req.status}


@app.get("/api/v1/waybills/{awb}/label", response_class=HTMLResponse, tags=["booking"])
def get_label(awb: str) -> str:
    """Printable shipping label. Open it in a browser to see it rendered."""
    data = get_waybill(awb)
    return render_label(data)
```

### 2.7 Supporting modules

```python
# services/consignment-service/app/db.py
"""Database connection pooling.

A pool reuses connections instead of opening a new one per request.
This matters on db.t3.micro, which allows only ~85 connections total.
"""
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,        # 2 services x 5 = 10 connections. Well within limits.
            open=True,
        )


@contextmanager
def get_conn():
    """Usage:  with get_conn() as conn, conn.cursor() as cur: ..."""
    if _pool is None:
        init_pool()
    with _pool.connection() as conn:
        yield conn
```

```python
# services/consignment-service/app/cache.py
"""Redis helpers. Every function fails SOFT.

If Redis is down the app must still work, just slower. A cache that can
take down your application is worse than no cache at all.
"""
import json
import logging
import os
import redis

log = logging.getLogger("cache")
_r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"),
                    decode_responses=True, socket_timeout=2)


def cache_get(key: str) -> dict | None:
    try:
        raw = _r.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:
        log.warning("cache read failed (continuing without cache): %s", e)
        return None


def cache_set(key: str, value: dict, ttl: int) -> None:
    try:
        _r.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        log.warning("cache write failed: %s", e)


def cache_delete(key: str) -> None:
    try:
        _r.delete(key)
    except Exception as e:
        log.warning("cache delete failed: %s", e)
```

```python
# services/consignment-service/app/models.py
"""Pydantic models. FastAPI uses these to validate input and build /docs."""
from pydantic import BaseModel, Field
from typing import Literal, Optional


class BookingRequest(BaseModel):
    merchant_name: str = Field(..., examples=["Nykaa"])
    consignee_name: str = Field(..., examples=["Ravi Kumar"])
    consignee_phone: str = Field(..., min_length=10, max_length=15)
    consignee_addr: str
    origin_hub: str = Field(..., examples=["HUB-BLR-01"])
    destination_hub: str = Field(..., examples=["HUB-DEL-03"])
    weight_grams: int = Field(..., gt=0, le=50000)
    payment_mode: Literal["PREPAID", "COD"]
    cod_amount: float = Field(default=0, ge=0)


class ScanRequest(BaseModel):
    awb: str
    status: Literal["IN_TRANSIT", "ARRIVED_AT_FACILITY"]
    hub_id: str
    remarks: Optional[str] = None


class StatusUpdate(BaseModel):
    status: Literal["OUT_FOR_DELIVERY", "DELIVERED", "RTO"]
    hub_id: Optional[str] = None
    remarks: Optional[str] = None
```

```python
# services/consignment-service/app/labels.py
"""Shipping label as HTML. Open in a browser and Ctrl+P to print."""


def render_label(d: dict) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Label {d['awb']}</title>
<style>
  body {{ font-family: monospace; padding: 20px; }}
  .label {{ border: 3px solid #000; width: 400px; padding: 16px; }}
  .awb {{ font-size: 26px; font-weight: bold; letter-spacing: 2px; }}
  .row {{ margin: 8px 0; }}
  .cod {{ background: #000; color: #fff; padding: 6px; font-weight: bold; }}
  hr {{ border: none; border-top: 2px dashed #000; }}
</style></head>
<body>
  <div class="label">
    <div style="text-align:center"><strong>FLEETPULSE LOGISTICS</strong></div>
    <hr>
    <div class="awb">{d['awb']}</div>
    <div class="row">{d['origin_hub']} &rarr; {d['destination_hub']}</div>
    <hr>
    <div class="row"><strong>TO:</strong> {d['consignee_name']}</div>
    <div class="row">{d['consignee_addr']}</div>
    <hr>
    <div class="row"><strong>FROM:</strong> {d['merchant_name']}</div>
    <div class="row">Status: {d['current_status']}</div>
    {'<div class="cod">COD &#8377;' + str(d['cod_amount']) + '</div>'
     if d['payment_mode'] == 'COD' else '<div class="row">PREPAID</div>'}
  </div>
</body></html>"""
```

### 2.8 Dispatch Service — the cross-service HTTP call

This file is the heart of "microservices" in this project. Read it carefully.

```python
# services/dispatch-service/app/consignment_client.py
"""HTTP client for talking to consignment-service.

This is what makes this a microservices project rather than one app:
dispatch does not touch consignment's tables. It asks over HTTP.
"""
import logging
import os
import httpx

log = logging.getLogger("consignment_client")

BASE_URL = os.getenv("CONSIGNMENT_URL", "http://consignment-service:8000")
TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class ConsignmentError(Exception):
    """Raised when consignment-service says no, or cannot be reached."""


def get_waybill(awb: str) -> dict:
    """Check an AWB exists before we put it on a runsheet."""
    try:
        r = httpx.get(f"{BASE_URL}/api/v1/waybills/{awb}", timeout=TIMEOUT)
    except httpx.RequestError as e:
        # The other service is down or unreachable. Fail loudly — do NOT
        # guess, because guessing here corrupts parcel state.
        raise ConsignmentError(f"consignment-service unreachable: {e}") from e

    if r.status_code == 404:
        raise ConsignmentError(f"AWB {awb} does not exist")
    if r.status_code >= 400:
        raise ConsignmentError(f"consignment-service returned {r.status_code}: {r.text}")
    return r.json()


def update_status(awb: str, status: str, hub_id: str | None = None,
                  remarks: str | None = None) -> dict:
    """Tell consignment-service the parcel moved (OUT_FOR_DELIVERY/DELIVERED/RTO)."""
    payload = {"status": status, "hub_id": hub_id, "remarks": remarks}
    try:
        r = httpx.patch(f"{BASE_URL}/api/v1/waybills/{awb}/status",
                        json=payload, timeout=TIMEOUT)
    except httpx.RequestError as e:
        raise ConsignmentError(f"consignment-service unreachable: {e}") from e

    if r.status_code == 409:
        raise ConsignmentError(f"Illegal status change for {awb}: {r.json().get('detail')}")
    if r.status_code >= 400:
        raise ConsignmentError(f"consignment-service returned {r.status_code}: {r.text}")

    log.info("updated awb=%s -> %s", awb, status)
    return r.json()
```

```python
# services/dispatch-service/app/main.py
"""Fleet & Dispatch Service — runsheets, GPS, final delivery."""

import os
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from .db import get_conn, init_pool
from .cache import redis_client
from .models import RunsheetRequest, GPSPing, DeliveryRequest
from . import consignment_client as cc

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("dispatch")

app = FastAPI(
    title="FleetPulse — Fleet & Dispatch Service",
    description="Driver runsheets, live GPS tracking, and final delivery status.",
    version="1.0.0",
)


@app.on_event("startup")
def startup() -> None:
    init_pool()
    log.info("dispatch-service ready")


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "service": "dispatch"}


@app.post("/api/v1/runsheets", status_code=201, tags=["dispatch"])
def create_runsheet(req: RunsheetRequest) -> dict:
    """Assign parcels to a driver. Each parcel becomes OUT_FOR_DELIVERY."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    runsheet_id = f"RS-{today}-{req.hub_id.split('-')[-1]}-{req.driver_id[-3:]}"

    # STEP 1: validate every AWB with the other service BEFORE writing anything.
    # If one is bad we reject the whole request rather than half-creating it.
    for awb in req.awbs:
        try:
            cc.get_waybill(awb)
        except cc.ConsignmentError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # STEP 2: create the runsheet locally.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO dispatch.runsheets
                   (id, driver_id, driver_name, vehicle_id, hub_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (runsheet_id, req.driver_id, req.driver_name, req.vehicle_id, req.hub_id),
        )
        conn.commit()

    # STEP 3: tell consignment-service each parcel is out for delivery.
    assigned, failed = [], []
    for awb in req.awbs:
        try:
            cc.update_status(awb, "OUT_FOR_DELIVERY", req.hub_id,
                             f"Assigned to {req.driver_name} ({runsheet_id})")
            assigned.append(awb)
        except cc.ConsignmentError as e:
            # Partial failure is possible without a message broker. We report
            # it honestly instead of pretending everything worked.
            log.error("could not assign %s: %s", awb, e)
            failed.append({"awb": awb, "error": str(e)})

    return {
        "runsheet_id": runsheet_id,
        "driver": req.driver_name,
        "vehicle": req.vehicle_id,
        "assigned": assigned,
        "failed": failed,
    }


@app.post("/api/v1/gps", status_code=202, tags=["tracking"])
def gps_ping(ping: GPSPing) -> dict:
    """Record a vehicle's position.

    Goes to Redis ONLY — never Postgres. Hundreds of these arrive per minute
    and only the newest matters. Writing them to db.t3.micro would be a
    self-inflicted performance problem.
    """
    key = f"vehicle:{ping.vehicle_id}:location"
    value = {
        "lat": ping.lat, "lon": ping.lon,
        "speed_kmph": ping.speed_kmph,
        "runsheet_id": ping.runsheet_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    # 1-hour TTL: a vehicle that stops reporting disappears on its own.
    redis_client().setex(key, 3600, json.dumps(value))
    return {"accepted": True, "vehicle_id": ping.vehicle_id}


@app.get("/api/v1/vehicles/{vehicle_id}/location", tags=["tracking"])
def get_location(vehicle_id: str) -> dict:
    raw = redis_client().get(f"vehicle:{vehicle_id}:location")
    if not raw:
        raise HTTPException(status_code=404,
                            detail=f"No recent location for {vehicle_id}")
    return {"vehicle_id": vehicle_id, **json.loads(raw)}


@app.post("/api/v1/delivery", status_code=201, tags=["dispatch"])
def record_delivery(req: DeliveryRequest) -> dict:
    """Final outcome: DELIVERED or RTO."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO dispatch.delivery_attempts
                   (awb, runsheet_id, outcome, reason)
               VALUES (%s, %s, %s, %s)""",
            (req.awb, req.runsheet_id, req.outcome, req.reason),
        )
        conn.commit()

    try:
        cc.update_status(req.awb, req.outcome, remarks=req.reason)
    except cc.ConsignmentError as e:
        # We recorded the attempt but could not update the parcel. Say so —
        # a 207 tells the caller "partially done", which is the truth.
        raise HTTPException(
            status_code=207,
            detail=f"Attempt saved, but status update failed: {e}",
        )

    return {"awb": req.awb, "outcome": req.outcome, "runsheet_id": req.runsheet_id}
```

```python
# services/dispatch-service/app/models.py
from pydantic import BaseModel, Field
from typing import Literal, Optional


class RunsheetRequest(BaseModel):
    driver_id: str = Field(..., examples=["DRV-4417"])
    driver_name: str = Field(..., examples=["Suresh Yadav"])
    vehicle_id: str = Field(..., examples=["KA01AB1234"])
    hub_id: str = Field(..., examples=["HUB-BLR-01"])
    awbs: list[str] = Field(..., min_length=1, max_length=50)


class GPSPing(BaseModel):
    vehicle_id: str
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    speed_kmph: float = Field(default=0, ge=0, le=200)
    runsheet_id: Optional[str] = None


class DeliveryRequest(BaseModel):
    awb: str
    runsheet_id: str
    outcome: Literal["DELIVERED", "RTO"]
    reason: Optional[str] = None
```

### 2.9 A test to start with

```python
# services/consignment-service/tests/test_waybills.py
"""Run with:  pytest -q

These use FastAPI's TestClient, which calls your app in-process —
no server and no database container needed for the validation tests.
"""
from fastapi.testclient import TestClient
from app.main import app, ALLOWED_TRANSITIONS, generate_awb

client = TestClient(app)


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_awb_format_is_fp_plus_ten_digits():
    awb = generate_awb()
    assert awb.startswith("FP")
    assert len(awb) == 12
    assert awb[2:].isdigit()


def test_delivered_is_a_terminal_state():
    """Nothing may follow DELIVERED — this guards against a real bug class."""
    assert ALLOWED_TRANSITIONS["DELIVERED"] == set()
    assert ALLOWED_TRANSITIONS["RTO"] == set()


def test_cannot_skip_straight_to_delivered():
    assert "DELIVERED" not in ALLOWED_TRANSITIONS["MANIFESTED"]


def test_booking_rejects_negative_weight():
    r = client.post("/api/v1/waybills", json={
        "merchant_name": "Test", "consignee_name": "Test",
        "consignee_phone": "9999999999", "consignee_addr": "Somewhere",
        "origin_hub": "HUB-BLR-01", "destination_hub": "HUB-DEL-03",
        "weight_grams": -5, "payment_mode": "PREPAID",
    })
    assert r.status_code == 422   # Pydantic rejected it before your code ran
```

### 2.10 `simulator/simulate_delhivery_scans.py`

```python
#!/usr/bin/env python3
"""
simulate_delhivery_scans.py — fake traffic for FleetPulse.

Creates AWBs, walks them through hub scans, assigns runsheets,
sends GPS pings, and delivers them.

    python simulate_delhivery_scans.py --parcels 20
    python simulate_delhivery_scans.py --parcels 100 --gps-pings 10
"""
import argparse
import random
import sys
import time

import requests

CONSIGNMENT = "http://localhost:8001"
DISPATCH = "http://localhost:8002"

HUBS = ["HUB-BLR-01", "HUB-CHN-02", "HUB-HYD-01",
        "HUB-DEL-03", "HUB-MUM-01", "HUB-KOL-02"]

# Rough real coordinates so the GPS pings look plausible on a map.
HUB_COORDS = {
    "HUB-BLR-01": (12.9716, 77.5946), "HUB-CHN-02": (13.0827, 80.2707),
    "HUB-HYD-01": (17.3850, 78.4867), "HUB-DEL-03": (28.7041, 77.1025),
    "HUB-MUM-01": (19.0760, 72.8777), "HUB-KOL-02": (22.5726, 88.3639),
}

MERCHANTS = ["Nykaa", "Meesho", "Ajio", "FirstCry", "boAt", "Lenskart"]
NAMES = ["Ravi Kumar", "Priya Sharma", "Amit Patel", "Sneha Reddy", "Arjun Nair"]
DRIVERS = [("DRV-4417", "Suresh Yadav", "KA01AB1234"),
           ("DRV-8823", "Manoj Singh", "DL03CD5678"),
           ("DRV-1192", "Vijay Kumar", "MH02EF9012")]


def book_parcel() -> tuple[str, str, str] | None:
    """POST a new booking. Returns (awb, origin, destination)."""
    origin, dest = random.sample(HUBS, 2)
    is_cod = random.random() < 0.6          # ~60% COD, realistic for India
    body = {
        "merchant_name": random.choice(MERCHANTS),
        "consignee_name": random.choice(NAMES),
        "consignee_phone": f"9{random.randint(10**8, 10**9 - 1)}",
        "consignee_addr": f"{random.randint(1,999)}, {random.choice(['MG Road','Park Street'])}",
        "origin_hub": origin,
        "destination_hub": dest,
        "weight_grams": random.randint(100, 5000),
        "payment_mode": "COD" if is_cod else "PREPAID",
        "cod_amount": round(random.uniform(299, 4999), 2) if is_cod else 0,
    }
    r = requests.post(f"{CONSIGNMENT}/api/v1/waybills", json=body, timeout=10)
    if r.status_code != 201:
        print(f"  ✗ booking failed: {r.status_code} {r.text[:120]}")
        return None
    awb = r.json()["awb"]
    print(f"  ✓ {awb}  {origin} → {dest}  {'COD' if is_cod else 'PREPAID'}")
    return awb, origin, dest


def hub_scan(awb: str, status: str, hub: str, remarks: str) -> bool:
    r = requests.post(f"{CONSIGNMENT}/api/v1/scans", timeout=10,
                      json={"awb": awb, "status": status,
                            "hub_id": hub, "remarks": remarks})
    ok = r.status_code == 201
    print(f"  {'✓' if ok else '✗'} {awb}  {status:<22} @ {hub}"
          + ("" if ok else f"  ({r.status_code} {r.text[:80]})"))
    return ok


def send_gps(vehicle: str, runsheet: str, origin: str, dest: str, n: int) -> None:
    """Interpolate a straight line between two hubs, one ping per step."""
    lat1, lon1 = HUB_COORDS[origin]
    lat2, lon2 = HUB_COORDS[dest]
    for i in range(n):
        f = (i + 1) / n
        requests.post(f"{DISPATCH}/api/v1/gps", timeout=10, json={
            "vehicle_id": vehicle,
            # Small random jitter so it looks like a road, not a ruler.
            "lat": lat1 + (lat2 - lat1) * f + random.uniform(-0.01, 0.01),
            "lon": lon1 + (lon2 - lon1) * f + random.uniform(-0.01, 0.01),
            "speed_kmph": round(random.uniform(15, 55), 1),
            "runsheet_id": runsheet,
        })
    print(f"  ✓ {vehicle}  sent {n} GPS pings")


def main() -> int:
    ap = argparse.ArgumentParser(description="FleetPulse traffic simulator")
    ap.add_argument("--parcels", type=int, default=10, help="how many to create")
    ap.add_argument("--gps-pings", type=int, default=5, help="pings per runsheet")
    ap.add_argument("--delay", type=float, default=0.1, help="seconds between calls")
    args = ap.parse_args()

    # Fail early with a clear message rather than a stack trace.
    for name, url in (("consignment", CONSIGNMENT), ("dispatch", DISPATCH)):
        try:
            requests.get(f"{url}/health", timeout=3).raise_for_status()
        except Exception:
            print(f"ERROR: {name}-service is not reachable at {url}")
            print("Start the stack first:  docker compose up -d")
            return 1

    print(f"\n{'='*62}\n  FleetPulse simulator — {args.parcels} parcels\n{'='*62}\n")

    print("STEP 1 — Booking parcels")
    parcels = []
    for _ in range(args.parcels):
        p = book_parcel()
        if p:
            parcels.append(p)
        time.sleep(args.delay)

    print("\nSTEP 2 — Line haul (hub to hub)")
    for awb, origin, dest in parcels:
        hub_scan(awb, "IN_TRANSIT", origin, "Departed origin facility")
        time.sleep(args.delay)
        hub_scan(awb, "ARRIVED_AT_FACILITY", dest, "Received at destination facility")
        time.sleep(args.delay)

    print("\nSTEP 3 — Runsheets and GPS")
    delivered_ok = []
    for chunk_start in range(0, len(parcels), 5):
        chunk = parcels[chunk_start:chunk_start + 5]
        driver_id, driver_name, vehicle = random.choice(DRIVERS)
        dest = chunk[0][2]

        r = requests.post(f"{DISPATCH}/api/v1/runsheets", timeout=15, json={
            "driver_id": driver_id, "driver_name": driver_name,
            "vehicle_id": vehicle, "hub_id": dest,
            "awbs": [awb for awb, _, _ in chunk],
        })
        if r.status_code != 201:
            print(f"  ✗ runsheet failed: {r.status_code} {r.text[:120]}")
            continue

        rs = r.json()
        print(f"  ✓ {rs['runsheet_id']}  {driver_name}  "
              f"{len(rs['assigned'])} assigned, {len(rs['failed'])} failed")
        send_gps(vehicle, rs["runsheet_id"], chunk[0][1], dest, args.gps_pings)
        delivered_ok += [(awb, rs["runsheet_id"]) for awb in rs["assigned"]]

    print("\nSTEP 4 — Final delivery")
    n_delivered = n_rto = 0
    for awb, runsheet in delivered_ok:
        # ~88% delivered, ~12% RTO — close to real Indian e-commerce rates.
        if random.random() < 0.88:
            outcome, reason = "DELIVERED", "Handed to consignee"
            n_delivered += 1
        else:
            outcome, reason = "RTO", random.choice(
                ["Consignee unavailable", "Address incorrect", "Refused delivery"])
            n_rto += 1

        r = requests.post(f"{DISPATCH}/api/v1/delivery", timeout=10, json={
            "awb": awb, "runsheet_id": runsheet,
            "outcome": outcome, "reason": reason})
        print(f"  {'✓' if r.status_code == 201 else '✗'} {awb}  {outcome}")
        time.sleep(args.delay)

    print(f"\n{'='*62}")
    print(f"  Booked {len(parcels)} · Delivered {n_delivered} · RTO {n_rto}")
    print(f"  Try:  curl {CONSIGNMENT}/api/v1/waybills/{parcels[0][0]}/history")
    print(f"{'='*62}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 3. Phase 2 — CI/CD with GitHub Actions

### 3.1 What the pipeline does

```
git push  ──▶  run tests  ──▶  build images  ──▶  push to ECR  ──▶  SSH to EC2  ──▶  restart
                   │
                   └── tests fail? stop here. Nothing is deployed.
```

### 3.2 `.github/workflows/deploy.yml`

```yaml
# .github/workflows/deploy.yml
name: Test, Build and Deploy

on:
  push:
    branches: [main]
  pull_request:            # tests run on PRs too, but no deploy

env:
  AWS_REGION: us-east-1
  ECR_REPO_CONSIGNMENT: fleetpulse/consignment-service
  ECR_REPO_DISPATCH: fleetpulse/dispatch-service

jobs:
  # ==========================================================
  # JOB 1 — Tests. Everything else depends on this passing.
  # ==========================================================
  test:
    name: Run unit tests
    runs-on: ubuntu-latest
    strategy:
      # Test both services even if the first one fails, so you see
      # every problem in one run instead of fixing them one at a time.
      fail-fast: false
      matrix:
        service: [consignment-service, dispatch-service]

    steps:
      - name: Check out the code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'
          cache-dependency-path: services/${{ matrix.service }}/requirements.txt

      - name: Install dependencies
        working-directory: services/${{ matrix.service }}
        run: |
          pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run pytest
        working-directory: services/${{ matrix.service }}
        run: pytest -q

  # ==========================================================
  # JOB 2 — Build images and push to ECR (main branch only)
  # ==========================================================
  build-and-push:
    name: Build and push to ECR
    runs-on: ubuntu-latest
    needs: test                                    # only if tests passed
    if: github.ref == 'refs/heads/main'

    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Log in to Amazon ECR
        id: ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push consignment-service
        env:
          REGISTRY: ${{ steps.ecr.outputs.registry }}
        run: |
          # Tag twice: the git SHA (so you can roll back to an exact build)
          # and 'latest' (so docker-compose.prod.yml stays simple).
          docker build -t $REGISTRY/$ECR_REPO_CONSIGNMENT:${{ github.sha }} \
                       -t $REGISTRY/$ECR_REPO_CONSIGNMENT:latest \
                       ./services/consignment-service
          docker push $REGISTRY/$ECR_REPO_CONSIGNMENT:${{ github.sha }}
          docker push $REGISTRY/$ECR_REPO_CONSIGNMENT:latest

      - name: Build and push dispatch-service
        env:
          REGISTRY: ${{ steps.ecr.outputs.registry }}
        run: |
          docker build -t $REGISTRY/$ECR_REPO_DISPATCH:${{ github.sha }} \
                       -t $REGISTRY/$ECR_REPO_DISPATCH:latest \
                       ./services/dispatch-service
          docker push $REGISTRY/$ECR_REPO_DISPATCH:${{ github.sha }}
          docker push $REGISTRY/$ECR_REPO_DISPATCH:latest

  # ==========================================================
  # JOB 3 — Deploy: SSH in and restart with the new images
  # ==========================================================
  deploy:
    name: Deploy to EC2
    runs-on: ubuntu-latest
    needs: build-and-push
    if: github.ref == 'refs/heads/main'

    steps:
      - name: SSH and redeploy
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.EC2_HOST }}          # Elastic IP
          username: ec2-user
          key: ${{ secrets.EC2_SSH_KEY }}        # full private key contents
          script_stop: true                       # abort on first failing line
          script: |
            set -e
            cd /home/ec2-user/fleetpulse

            # ECR login tokens last 12 hours, so refresh on every deploy.
            aws ecr get-login-password --region ${{ env.AWS_REGION }} \
              | docker login --username AWS --password-stdin ${{ secrets.ECR_REGISTRY }}

            docker compose -f docker-compose.prod.yml pull
            docker compose -f docker-compose.prod.yml up -d

            # Remove old images so the 8 GB disk doesn't fill up over time.
            docker image prune -af --filter "until=72h"

            sleep 5
            docker compose -f docker-compose.prod.yml ps
            curl -fsS http://localhost:8001/health && echo " consignment OK"
            curl -fsS http://localhost:8002/health && echo " dispatch OK"
```

### 3.3 GitHub Secrets to create

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | From an IAM user with ECR push permission only |
| `AWS_SECRET_ACCESS_KEY` | Same IAM user |
| `ECR_REGISTRY` | `123456789012.dkr.ecr.us-east-1.amazonaws.com` |
| `EC2_HOST` | Your Elastic IP |
| `EC2_SSH_KEY` | Whole contents of your `.pem` file, including the BEGIN/END lines |

Create the IAM user with **`AmazonEC2ContainerRegistryPowerUser`** only. Do not use your root
account keys, and never grant `AdministratorAccess` to a CI user.

> **Level-up later:** these are long-lived keys. The professional alternative is GitHub OIDC, where
> Actions swaps a short-lived token for temporary credentials and there is no stored secret at all.
> It is more setup than you need today — but knowing it exists is worth a sentence in an interview.

### 3.4 `docker-compose.prod.yml`

```yaml
# docker-compose.prod.yml — lives on the EC2 instance.
# Differences from local: pulls prebuilt images from ECR, uses RDS
# instead of a Postgres container, no source-code volume mounts.

services:
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s

  consignment-service:
    image: ${ECR_REGISTRY}/fleetpulse/consignment-service:${IMAGE_TAG:-latest}
    environment:
      DATABASE_URL: ${DATABASE_URL}       # RDS endpoint from .env
      REDIS_URL: redis://redis:6379/0
      LOG_LEVEL: INFO
    ports:
      - "8001:8000"
    depends_on:
      redis: { condition: service_healthy }
    restart: unless-stopped
    # Memory caps: on a 1 GB box, one leaky container must not take
    # down the whole machine.
    deploy:
      resources:
        limits: { memory: 200M }

  dispatch-service:
    image: ${ECR_REGISTRY}/fleetpulse/dispatch-service:${IMAGE_TAG:-latest}
    environment:
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: redis://redis:6379/0
      CONSIGNMENT_URL: http://consignment-service:8000
      LOG_LEVEL: INFO
    ports:
      - "8002:8000"
    depends_on:
      redis: { condition: service_healthy }
      consignment-service: { condition: service_started }
    restart: unless-stopped
    deploy:
      resources:
        limits: { memory: 200M }
```

---

## 4. Phase 3 — Terraform

### 4.1 `infra/terraform/main.tf`

```hcl
# infra/terraform/main.tf
# Everything AWS needs for FleetPulse, in one readable file.
#
#   terraform init      download the AWS provider
#   terraform plan      preview what will be created
#   terraform apply     actually create it
#   terraform destroy   delete everything (do this between study sessions!)

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = "fleetpulse"
      ManagedBy = "terraform"
    }
  }
}

# ==========================================================================
# NETWORKING
# A VPC is your own private network inside AWS.
# ==========================================================================
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"    # ~65k private IPs
  enable_dns_support   = true
  enable_dns_hostnames = true             # needed for the RDS hostname to resolve
  tags                 = { Name = "fleetpulse-vpc" }
}

# The door between your VPC and the internet. Free.
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "fleetpulse-igw" }
}

# Subnet 1: where the EC2 instance lives.
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = true
  tags                    = { Name = "fleetpulse-public-a" }
}

# Subnet 2: RDS requires a subnet group spanning at least TWO availability
# zones, even for a single-AZ database. Nothing else uses this subnet.
resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "${var.aws_region}b"
  map_public_ip_on_launch = true
  tags                    = { Name = "fleetpulse-public-b" }
}

# Route table: send anything not local out through the internet gateway.
# This is what replaces a NAT Gateway — and it costs $0 instead of $33/month.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "fleetpulse-public-rt" }
}

resource "aws_route_table_association" "a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

# ==========================================================================
# SECURITY GROUPS  (firewalls)
# ==========================================================================
resource "aws_security_group" "app" {
  name        = "fleetpulse-app-sg"
  description = "FleetPulse EC2 instance"
  vpc_id      = aws_vpc.main.id

  # SSH. Restricted to your IP for manual access.
  # NOTE: GitHub Actions runners use rotating IPs, so CI deploys need
  # port 22 reachable. Two options:
  #   a) set my_ip_cidr = "0.0.0.0/0"  (key-only auth, no passwords) - simple
  #   b) use AWS SSM Session Manager instead of SSH  - no open port at all
  # Start with (a), move to (b) once everything works.
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Your two services. Restricted to your IP: these are dev APIs with no
  # authentication, so do NOT expose them to the whole internet.
  ingress {
    description = "FleetPulse services"
    from_port   = 8001
    to_port     = 8002
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "fleetpulse-app-sg" }
}

resource "aws_security_group" "db" {
  name        = "fleetpulse-db-sg"
  description = "RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  # Only the EC2 instance may reach the database. Note this references the
  # OTHER SECURITY GROUP, not an IP address — so the rule keeps working
  # even if the instance is replaced and gets a new IP.
  ingress {
    description     = "Postgres from the app instance"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = { Name = "fleetpulse-db-sg" }
}

# ==========================================================================
# ECR — container image storage
# ==========================================================================
resource "aws_ecr_repository" "services" {
  for_each = toset(["consignment-service", "dispatch-service"])
  name     = "fleetpulse/${each.key}"

  image_scanning_configuration { scan_on_push = true }
  force_delete = true    # lets `terraform destroy` remove non-empty repos
}

# The free tier gives you 500 MB. Without this policy, every CI run adds an
# image and you will quietly cross the limit in a few weeks.
resource "aws_ecr_lifecycle_policy" "cleanup" {
  for_each   = aws_ecr_repository.services
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the 3 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 }
      action       = { type = "expire" }
    }]
  })
}

# ==========================================================================
# EC2 — the server
# ==========================================================================

# Always fetch the newest Amazon Linux 2023 AMI rather than hard-coding an ID
# (AMI ids differ per region and go stale).
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
}

# Lets the instance pull from ECR without storing any AWS keys on it.
resource "aws_iam_role" "ec2" {
  name = "fleetpulse-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Optional but recommended: browser-based shell with no SSH key needed.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "fleetpulse-ec2-profile"
  role = aws_iam_role.ec2.name
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type       # t3.micro
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  key_name               = var.key_pair_name       # create this in the console first

  user_data = file("${path.module}/user_data.sh")

  # ⚠️ THE MOST IMPORTANT COST SETTING IN THIS FILE.
  # t3 instances default to "unlimited" mode, which CHARGES $0.05 per
  # vCPU-hour whenever CPU goes above the baseline. "standard" makes the
  # instance slow down instead of billing you. Leave this in.
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_size = 20          # free tier allows 30 GB
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"  # IMDSv2 only — basic security hardening
  }

  tags = { Name = "fleetpulse-app" }
}

# A fixed public IP that survives instance restarts.
# Free while attached to a RUNNING instance (750 hrs/month on the free tier).
resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
  tags     = { Name = "fleetpulse-eip" }
}

# ==========================================================================
# RDS — managed PostgreSQL
# ==========================================================================
resource "aws_db_subnet_group" "main" {
  name       = "fleetpulse-db-subnets"
  subnet_ids = [aws_subnet.public_a.id, aws_subnet.public_b.id]
  tags       = { Name = "fleetpulse-db-subnets" }
}

resource "aws_db_instance" "postgres" {
  identifier     = "fleetpulse-db"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = "db.t3.micro"        # free tier eligible

  allocated_storage     = 20            # exactly the free-tier allowance
  max_allocated_storage = 0             # ⚠️ 0 disables autoscaling. If storage
                                        # grows past 20 GB you start paying.
  storage_type          = "gp2"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password            # from TF_VAR_db_password, never committed

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]

  publicly_accessible = false           # no public endpoint — only your EC2 can reach it
  multi_az            = false           # Multi-AZ is NOT free tier

  backup_retention_period      = 1
  skip_final_snapshot          = true   # so `terraform destroy` doesn't hang
  deletion_protection          = false
  performance_insights_enabled = false  # NOT free on t3.micro
  monitoring_interval          = 0      # Enhanced Monitoring is NOT free

  tags = { Name = "fleetpulse-db" }
}
```

### 4.2 `infra/terraform/user_data.sh`

```bash
#!/bin/bash
# infra/terraform/user_data.sh
# Runs ONCE, automatically, the first time the EC2 instance boots.
# Check progress with:  sudo tail -f /var/log/user-data.log

set -euxo pipefail
exec > >(tee /var/log/user-data.log) 2>&1

dnf update -y

# ---- Docker ----
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user      # lets ec2-user run docker without sudo

# ---- Docker Compose v2 (as a CLI plugin) ----
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# ---- 1 GB swap ----
# Not strictly needed for this workload, but it turns "the machine froze"
# into "the machine got slow" if something misbehaves. Cheap insurance.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---- App directory ----
mkdir -p /home/ec2-user/fleetpulse
chown -R ec2-user:ec2-user /home/ec2-user/fleetpulse

echo "user-data finished at $(date)"
```

### 4.3 `variables.tf` and `outputs.tf`

```hcl
# infra/terraform/variables.tf
variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "Free tier: t2.micro or t3.micro"
  type        = string
  default     = "t3.micro"
}

variable "key_pair_name" {
  description = "Name of an EC2 key pair you created in the console"
  type        = string
}

variable "my_ip_cidr" {
  description = "Your public IP with /32, e.g. 203.0.113.42/32"
  type        = string
}

variable "db_name" {
  type    = string
  default = "fleetpulse"
}

variable "db_username" {
  type    = string
  default = "fleetadmin"
}

variable "db_password" {
  description = "Set via TF_VAR_db_password — never hard-code this"
  type        = string
  sensitive   = true
}
```

```hcl
# infra/terraform/outputs.tf
output "app_public_ip" {
  description = "Elastic IP — use this for SSH and for the EC2_HOST secret"
  value       = aws_eip.app.public_ip
}

output "ssh_command" {
  value = "ssh -i ~/.ssh/${var.key_pair_name}.pem ec2-user@${aws_eip.app.public_ip}"
}

output "rds_endpoint" {
  value = aws_db_instance.postgres.endpoint
}

output "database_url" {
  description = "Put this in .env on the EC2 instance"
  value       = "postgresql://${var.db_username}:PASSWORD@${aws_db_instance.postgres.endpoint}/${var.db_name}"
  sensitive   = true
}

output "ecr_registry" {
  value = split("/", aws_ecr_repository.services["consignment-service"].repository_url)[0]
}
```

### 4.4 Running it

```bash
cd infra/terraform

# Create an EC2 key pair first: AWS Console -> EC2 -> Key Pairs -> Create.
# Download the .pem and: chmod 400 ~/.ssh/fleetpulse-key.pem

export TF_VAR_db_password='pick-a-strong-password'
export TF_VAR_key_pair_name='fleetpulse-key'
export TF_VAR_my_ip_cidr="$(curl -s https://checkip.amazonaws.com)/32"

terraform init
terraform plan        # READ THIS. It shows exactly what will be created.
terraform apply

terraform output
```

### 4.5 ⚠️ Before you apply anything: set a budget alarm

**Do this first, not later.** Ten minutes now prevents an unpleasant surprise.

1. AWS Console → **Billing and Cost Management** → **Billing preferences**
2. Tick **Receive Free Tier Alerts** and **Receive Billing Alerts** → Save
3. → **Budgets** → **Create budget** → **Customize (advanced)** → **Cost budget**
4. Period **Monthly**, Amount **$1.00**
5. Add three alerts, all to your email:
   - 80% of **actual** spend
   - 100% of **actual** spend
   - 100% of **forecasted** spend ← the useful one; it warns you days early

Budgets are free (2 of them). The forecast alert tells you "you're trending toward $12 this month"
on day three, which is when you can still do something about it.

### 4.6 The three things that quietly cost money

| Trap | Why | Guard |
|---|---|---|
| **t3 unlimited CPU credits** | Bills $0.05/vCPU-hour above baseline | `cpu_credits = "standard"` ✓ in the code above |
| **Public IPv4** | Chargeable since Feb 2024; free tier covers 750 hrs = one running instance | Keep exactly one EIP, attached |
| **ECR over 500 MB** | Every CI run pushes an image | Lifecycle policy ✓ in the code above |

And the big one: **`terraform destroy` between study sessions.** The whole stack rebuilds in about
10 minutes. Running it 24×7 for a month uses your entire free-tier allowance for no reason.

---

## 5. Learning Roadmap

Four milestones over 2–3 weeks. **Do them in order, and do not start the next one until the current
one's checkpoint works.** That rule is the whole trick to not feeling overwhelmed.

### Milestone 1 — One service, running locally (Days 1–4)

- [ ] Set up the folder structure; delete the unused `facility-service` / `notification-service` folders
- [ ] Write `db/init.sql`
- [ ] Build consignment-service: `POST /waybills`, `GET /waybills/{awb}`, `GET /health`
- [ ] Write the Dockerfile
- [ ] `docker-compose.yml` with just Postgres + consignment-service
- [ ] Write the 5 tests in §2.9; get them green
- [ ] **`git init` and make your first commit** — the repo currently has zero

> ✅ **Checkpoint:** `docker compose up` works. You can open `http://localhost:8001/docs`, book a
> parcel through the Swagger UI, and fetch it back. `pytest` passes.
>
> 🎉 You now have a containerised REST API with a database. That is a real milestone — take a break.

### Milestone 2 — Two services talking (Days 5–9)

- [ ] Add Redis to Compose; add caching to `GET /waybills/{awb}`
- [ ] Add `POST /scans` with the status-transition rules
- [ ] Build dispatch-service: runsheets, GPS, delivery
- [ ] Write `consignment_client.py` — **the cross-service HTTP call**
- [ ] Add the shipping label endpoint (it is fun, and it demos well)
- [ ] Write `simulate_delhivery_scans.py`

> ✅ **Checkpoint:** `python simulate_delhivery_scans.py --parcels 20` runs start to finish. Every
> parcel reaches `DELIVERED` or `RTO`. `GET /waybills/{awb}` shows `"_cache": "HIT"` on the second call.
>
> 🎉 You have a working microservices system. Record a 60-second screen capture — it is your best
> portfolio asset and takes two minutes.

### Milestone 3 — On AWS (Days 10–16)

- [ ] **Set the budget alarm first** (§4.5)
- [ ] Create an EC2 key pair in the console
- [ ] Write `main.tf`, `variables.tf`, `outputs.tf`, `user_data.sh`
- [ ] `terraform apply`
- [ ] SSH in; confirm Docker is installed (`docker --version`)
- [ ] Connect to RDS from the instance and run `init.sql`
- [ ] Copy `docker-compose.prod.yml` and a `.env` to the instance
- [ ] Build and push images to ECR by hand once (so you understand what CI will automate)
- [ ] `docker compose -f docker-compose.prod.yml up -d`
- [ ] Run the simulator from your laptop against the Elastic IP
- [ ] **`terraform destroy`, then `terraform apply` again** — proves your IaC is complete

> ✅ **Checkpoint:** `curl http://<your-elastic-ip>:8001/health` returns `{"status":"ok"}` from your
> own machine, and the simulator works against the cloud.
>
> 🎉 This is the big one. You have provisioned cloud infrastructure with code and deployed to it.

### Milestone 4 — Automated deployment (Days 17–21)

- [ ] Create the IAM user for CI (ECR permissions only)
- [ ] Add the five GitHub Secrets
- [ ] Write `.github/workflows/deploy.yml`
- [ ] Push a trivial change and watch the pipeline run
- [ ] **Deliberately break a test and confirm the deploy is blocked** — this is the part that proves
      the pipeline is real, and it is the best story to tell about it
- [ ] Write a proper `README.md`: architecture diagram, setup steps, screenshots
- [ ] Add the repo link to your CV and LinkedIn

> ✅ **Checkpoint:** you change one line of code, `git push`, and 3 minutes later it is live on AWS
> with no manual steps.
>
> 🎉 That is a complete DevOps loop: code → test → build → registry → deploy. You are done.

### If you want to keep going

In rough order of value for a job search:

1. **Nginx reverse proxy + free HTTPS** via Caddy or certbot — one domain, no port numbers, real TLS
2. **A tiny Prometheus + Grafana** on the box (you now have ~500 MB headroom, so it fits)
3. **A simple frontend** — a tracking page that calls your API. Recruiters click on things
4. **Structured JSON logging** and a `/metrics` endpoint
5. *Then* Kubernetes — as a **separate** project, so this one stays clean and finished

---

## 6. Talking about this project

Three questions you will be asked, and honest answers that show judgement.

**"Why not Kubernetes?"**
> "I sized the workload first. A K3s control plane needs about 500 MB and the free-tier instance has
> 1 GB — that is half my memory spent on orchestration for two containers on one host. Docker Compose
> did the job in 420 MB. I'd move to Kubernetes when I need multi-node scheduling or autoscaling,
> which this doesn't."

**"Why a shared database instead of one per service?"**
> "Two services didn't justify the overhead of two databases and cross-database consistency. But I
> split them into separate Postgres schemas so the ownership boundary is explicit — if I ever split
> the services, it's a `pg_dump --schema` rather than untangling shared tables."

**"How did you keep it free?"**
> "Three things catch people. t3 instances default to unlimited CPU credit mode, which bills per
> vCPU-hour above baseline, so I pinned `cpu_credits = "standard"`. Public IPv4 has been chargeable
> since 2024, so I run exactly one Elastic IP. And ECR's free tier is 500 MB, so I set a lifecycle
> policy keeping three images per repo. Then a Budgets alert with a *forecast* threshold, because
> actual-spend alerts only tell you the money is already gone."

That last answer is the one that stands out. Most junior candidates have never thought about cloud
cost at all.

---

## One last thing

You are going to hit a point — probably around Milestone 3 — where something does not work and the
error message makes no sense. That is not a sign you chose the wrong project. It is the actual work.
Read the logs (`docker compose logs -f`, `sudo tail -f /var/log/user-data.log`), change one thing at
a time, and it will come apart.

Good luck. Build the small thing well.
