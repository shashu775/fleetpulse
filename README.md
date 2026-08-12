# FleetPulse

A microservices logistics platform modelling Delhivery-style parcel operations — built as a
hands-on DevOps portfolio project.

Two Python/FastAPI services communicating over REST, backed by PostgreSQL and Redis, orchestrated
with Docker Compose.

---

## Quick start

Docker is the only prerequisite.

```bash
git clone <your-repo-url>
cd fleetpulse
cp .env.example .env

docker compose up --build -d
docker compose ps                 # all four containers should be healthy
```

Then open the interactive API docs:

| | URL |
|---|---|
| **Consignment & Hub API** | http://localhost:8001/docs |
| **Fleet & Dispatch API** | http://localhost:8002/docs |

Generate realistic traffic:

```bash
docker compose --profile sim run --rm simulator --parcels 20
```

That books 20 parcels, scans them through hubs, assigns driver runsheets, streams GPS pings, and
delivers or RTOs each one — exercising every endpoint including the cross-service call.

---

## Architecture

```
                       ┌──────────────────────────────┐
   HTTP :8001  ───────▶│  Consignment & Hub Service   │
                       │  AWB booking · labels        │
                       │  hub scans · STATE MACHINE   │
                       └──────┬───────────────┬───────┘
                              │               │
   HTTP :8002                 │               │
        │                     ▼               ▼
        │              ┌────────────┐  ┌────────────┐
        │              │ PostgreSQL │  │   Redis    │
        │              │ 2 schemas  │  │ cache +GPS │
        │              └────────────┘  └────────────┘
        │                     ▲               ▲
        ▼                     │               │
 ┌──────────────────────┐     │               │
 │ Fleet & Dispatch     │─────┴───────────────┘
 │ runsheets · GPS      │
 │ delivery outcomes    │──── REST ────▶ Consignment
 └──────────────────────┘   (the only inter-service call)
```

**One rule makes this microservices rather than one app in two folders:** dispatch-service never
writes to consignment's tables, even though the database connection makes it trivially possible.
Consignment owns the parcel state machine, so it must be the only thing that can change parcel
status. Dispatch asks over HTTP — see
[`consignment_client.py`](services/dispatch-service/app/consignment_client.py).

### Parcel lifecycle

```
MANIFESTED → IN_TRANSIT → ARRIVED_AT_FACILITY → OUT_FOR_DELIVERY → DELIVERED
                  ▲               │                     │
                  └───────────────┘                     └──────────→ RTO
                   (multi-hub hops)
```

Illegal transitions return **HTTP 409** rather than silently corrupting the record. The rule lives
in one place: `ALLOWED_TRANSITIONS` in
[`services/consignment-service/app/main.py`](services/consignment-service/app/main.py).

---

## API

### Consignment & Hub Service — `:8001`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/waybills` | Book a shipment, issue an AWB |
| `GET` | `/api/v1/waybills/{awb}` | Track (Redis first, then Postgres) |
| `GET` | `/api/v1/waybills/{awb}/history` | Full scan history |
| `GET` | `/api/v1/waybills/{awb}/label` | Printable HTML shipping label |
| `POST` | `/api/v1/scans` | Hub scan: `IN_TRANSIT` / `ARRIVED_AT_FACILITY` |
| `PATCH` | `/api/v1/waybills/{awb}/status` | Internal — called by dispatch |
| `GET` | `/health` | Liveness |

### Fleet & Dispatch Service — `:8002`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/runsheets` | Assign parcels to a driver |
| `GET` | `/api/v1/runsheets/{id}` | Runsheet and its delivery attempts |
| `POST` | `/api/v1/gps` | Vehicle position ping → Redis |
| `GET` | `/api/v1/vehicles/{id}/location` | Last known position |
| `POST` | `/api/v1/delivery` | Final outcome: `DELIVERED` / `RTO` |
| `GET` | `/health` | Liveness |

### Try it

```bash
# Book a parcel
AWB=$(curl -s -X POST localhost:8001/api/v1/waybills \
  -H 'Content-Type: application/json' -d '{
  "merchant_name":"Nykaa","consignee_name":"Ravi Kumar","consignee_phone":"9876543210",
  "consignee_addr":"12 MG Road, Bengaluru","origin_hub":"HUB-BLR-01",
  "destination_hub":"HUB-DEL-03","weight_grams":900,"payment_mode":"COD","cod_amount":1499
}' | grep -o '"awb":"[^"]*"' | cut -d'"' -f4)

# Watch the cache: MISS, then HIT
curl -s localhost:8001/api/v1/waybills/$AWB | grep -o '"_cache":"[^"]*"'
curl -s localhost:8001/api/v1/waybills/$AWB | grep -o '"_cache":"[^"]*"'

# The state machine rejects an illegal skip → 409
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8001/api/v1/scans \
  -H 'Content-Type: application/json' \
  -d "{\"awb\":\"$AWB\",\"status\":\"ARRIVED_AT_FACILITY\",\"hub_id\":\"HUB-DEL-03\"}"

# Shipping label — open in a browser
echo "http://localhost:8001/api/v1/waybills/$AWB/label"
```

---

## Tests

36 unit tests. They need **no database and no Redis** — TestClient runs the app in-process without
lifespan, and validation cases are rejected by Pydantic before any handler executes.

With Docker (no local Python needed):

```bash
docker build --target test -t fp-consignment-test ./services/consignment-service
docker run --rm fp-consignment-test          # 15 passed

docker build --target test -t fp-dispatch-test ./services/dispatch-service
docker run --rm fp-dispatch-test             # 21 passed
```

With a local Python 3.12:

```bash
cd services/consignment-service
pip install -r requirements.txt
pytest -q
pytest -q -k terminal                        # run a single test by name
```

---

## Design decisions worth explaining

**GPS never touches PostgreSQL.** 100 vehicles pinging every 10 seconds is ~864,000 writes/day of
data whose value expires in seconds. It lives in Redis with a 1-hour TTL, so a vehicle that stops
reporting expires on its own. `POST /api/v1/gps` returns **202 Accepted**, not 201, to be honest
that the write is deliberately non-durable.

**One database, two schemas.** Two services don't justify two databases and a distributed-transaction
problem. Separate `consignment` and `dispatch` schemas keep the ownership boundary explicit, so a
future split is a `pg_dump --schema=dispatch` rather than untangling shared tables.

**The two Redis roles are different.** For consignment it's a *cache* that fails soft — if Redis is
down, fall back to Postgres and carry on slower. For dispatch it's the *store* — last-known position
lives nowhere else, so failures surface as 503 rather than silently discarding data.

```bash
docker compose stop redis
curl -s localhost:8001/api/v1/waybills/$AWB      # 200 — still works, just slower
curl -s -o /dev/null -w '%{http_code}\n' \
     localhost:8002/api/v1/vehicles/KA01AB1234/location   # 503 — honest
docker compose start redis
```

**`scan_events` is append-only.** Never updated or deleted, so the tracking history is a trustworthy
audit trail. `waybills.current_status` is a denormalised convenience column that could be rebuilt
from it.

**Partial failure is reported, not hidden.** Without a message broker there's no transaction across
two services, so `POST /runsheets` returns both `assigned[]` and `failed[]`, and `POST /delivery` can
return **207 Multi-Status**. That's the real cost of synchronous REST, and naming it is better than
pretending it away.

---

## Project layout

```
fleetpulse/
├── docker-compose.yml            local stack: one command
├── db/init.sql                   2 schemas, 4 tables
├── services/
│   ├── consignment-service/      booking, scans, labels, state machine
│   └── dispatch-service/         runsheets, GPS, delivery
├── simulator/                    traffic generator (runs in Docker)
└── docs/                         design documents
```

## Common commands

```bash
docker compose up --build -d                        # start
docker compose ps                                   # health
docker compose logs -f dispatch-service             # follow logs
docker compose --profile sim run --rm simulator --parcels 20
docker compose down                                 # stop
docker compose down -v                              # stop AND wipe the DB
```

> Editing `db/init.sql` requires `docker compose down -v` — Postgres only runs init scripts on a
> fresh data volume.

---

## Documentation

| Document | Purpose |
|---|---|
| [FleetPulse-Simple.md](docs/FleetPulse-Simple.md) | The build plan this implements |
| [FleetPulse-Architecture.md](docs/FleetPulse-Architecture.md) | Request flows and failure behaviour |
| [FleetPulse-Kubernetes.md](docs/FleetPulse-Kubernetes.md) | Next: minikube and EKS |
| [FleetPulse-Addon-Observability.md](docs/FleetPulse-Addon-Observability.md) | Optional: metrics, dashboards, tracing |
| [FleetPulse-Addon-Notification.md](docs/FleetPulse-Addon-Notification.md) | Optional: third service via transactional outbox |

## Roadmap

- [x] **Milestone 1** — consignment-service, Postgres, Docker Compose, tests
- [x] **Milestone 2** — dispatch-service, Redis caching, cross-service REST, simulator
- [ ] **Milestone 3** — Terraform: VPC, EC2, RDS, ECR
- [ ] **Milestone 4** — GitHub Actions: test → build → ECR → deploy

## Tech stack

Python 3.12 · FastAPI · PostgreSQL 16 · Redis 7 · Docker Compose · pytest
