# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A working two-service logistics platform (Delhivery-style parcel operations) built as a DevOps
learning project. Python 3.12 + FastAPI, PostgreSQL, Redis, an nginx-served frontend, all under
Docker Compose. Milestones 1–2 of `docs/FleetPulse-Simple.md` are complete; Terraform and CI are not
built yet.

**The repo has zero git commits.** Everything is untracked. Making the first commit is the user's
step — do not commit unless asked.

## Commands

Docker is the only prerequisite. **There is no working local Python** on this machine (`python`
resolves to the Microsoft Store stub), so tests and the simulator run in containers.

```bash
docker compose up --build -d          # start everything -> http://localhost
docker compose ps                     # health of all 5 containers
docker compose logs -f dispatch-service
docker compose down                   # stop
docker compose down -v                # stop AND wipe the database
```

### Tests

Each service image has a `test` stage. 36 tests total (15 consignment, 21 dispatch).

```bash
docker build --target test -t fp-consignment-test ./services/consignment-service
docker run --rm fp-consignment-test

docker build --target test -t fp-dispatch-test ./services/dispatch-service
docker run --rm fp-dispatch-test

# One test / one pattern:
docker run --rm fp-dispatch-test pytest -q -k gps
docker run --rm fp-consignment-test pytest -q tests/test_waybills.py::test_health_returns_ok
```

Tests need **no database and no Redis**: `TestClient(app)` is used *without* a context manager so
FastAPI's lifespan never runs, and validation cases are rejected by Pydantic before any handler
executes. Preserve that property — a test that reaches a handler needing a live pool will fail with
a `RuntimeError` about `DATABASE_URL`.

### Traffic simulator

```bash
docker compose --profile sim run --rm simulator --parcels 20
docker compose --profile sim run --rm simulator --parcels 12 --seed 42   # reproducible
```

Books parcels, scans them through hubs, creates runsheets, streams GPS, delivers/RTOs. The fastest
way to put realistic data behind the UI.

### Database

```bash
docker compose exec postgres psql -U fleetadmin -d fleetpulse
docker compose exec postgres psql -U fleetadmin -d fleetpulse -c "\dt consignment.*"
```

`db/init.sql` runs **only on a fresh volume**. After editing it: `docker compose down -v && docker compose up -d`.

## Architecture

```
http://localhost (nginx)  ──┬─ static UI (frontend/)
                            ├─ /api/consignment/v1/*  ->  consignment-service:8000/api/v1/*
                            └─ /api/dispatch/v1/*     ->  dispatch-service:8000/api/v1/*

consignment-service :8001  ── owns parcels + THE STATE MACHINE
dispatch-service    :8002  ── owns runsheets/GPS/delivery, calls consignment over HTTP
PostgreSQL          :5432  ── one database, schemas `consignment` and `dispatch`
Redis               :6379  ── tracking cache (consignment) + live GPS (dispatch)
```

### The rule that defines this codebase

**`dispatch-service` must never write to the `consignment` schema.** Same database, same
credentials, trivially possible — and forbidden. `consignment-service` owns `ALLOWED_TRANSITIONS`
(`services/consignment-service/app/main.py`), the state machine that returns **409** on illegal
moves. If dispatch wrote directly, that rule would live in two places and eventually disagree.

All cross-service traffic goes through
`services/dispatch-service/app/consignment_client.py` — the only inter-service call in the system.
Dependencies are **one-directional**: dispatch → consignment, never the reverse.

### Parcel state machine

`MANIFESTED → IN_TRANSIT → ARRIVED_AT_FACILITY → OUT_FOR_DELIVERY → DELIVERED | RTO`, with an
`ARRIVED_AT_FACILITY ⇄ IN_TRANSIT` loop for multi-hub routes. Consignment drives the first three via
`POST /api/v1/scans`; dispatch drives the last three via `PATCH /api/v1/waybills/{awb}/status`.

`_apply_transition()` holds the row with `SELECT ... FOR UPDATE` so two concurrent scans cannot both
read the old status and both conclude their move is legal.

### The two Redis roles are deliberately different

| | consignment `app/cache.py` | dispatch `app/cache.py` |
|---|---|---|
| Role | **Cache** — Postgres is the truth | **Store** — nothing else holds GPS |
| On failure | Fails soft: logs, returns `None`, behaves like a miss | Propagates: endpoints return **503** |

Do not "harmonise" these. A cache that can take down the service is worse than no cache; a store
that silently discards writes is worse than an error.

Cache invalidation is `DEL`, never overwrite — safe if the surrounding transaction rolls back.
`GET /api/v1/waybills/{awb}` returns a `_cache: HIT|MISS` field, which the UI displays.

### Deliberate design choices that look like omissions

- **No `gps_pings` table.** ~864k writes/day of data stale in 10 seconds. Redis only, 1-hour TTL.
  `POST /api/v1/gps` returns **202 Accepted**, not 201, because the write is non-durable.
- **`delivery_attempts.awb` has no foreign key** to `consignment.waybills` — a real FK would couple
  the schemas at the database level and block a future service split.
- **`scan_events` is append-only.** `waybills.current_status` is a denormalised convenience column
  rebuildable from it.
- **Partial failure is reported, not hidden.** `POST /runsheets` returns `assigned[]` *and*
  `failed[]`; `POST /delivery` returns **207 Multi-Status** when the attempt saved but the status
  update failed. These are not bugs — without a broker there is no cross-service transaction. The
  intended fix is the outbox pattern in `docs/FleetPulse-Addon-Notification.md`.
- **`/api/v1/waybills` list is capped at `limit=100`** via `Query(le=100)`.

### Frontend

`frontend/` is plain HTML/CSS/JS — **no framework, no build step, no `node_modules`**. nginx serves
three files and proxies both APIs under one origin, which is why there is no CORS configuration
anywhere. Keep it that way; adding a bundler would add a toolchain this project deliberately avoids.

`frontend/nginx.conf` rewrites `/api/consignment/` → `/api/` on the upstream, so the browser calls
`/api/consignment/v1/waybills` and the service sees `/api/v1/waybills`.

## Conventions

- **`.env.example` is the contract.** New env var in code → add it there with a safe placeholder in
  the same change. `docker-compose.yml` also carries defaults, so the stack starts without a `.env`.
- **Service discovery uses service names** (`http://consignment-service:8000`). Docker Compose DNS
  and Kubernetes DNS both resolve it, which is why no code changes when moving to a cluster.
- **Missing config fails loudly.** `db.py` raises a `RuntimeError` naming the variable and the fix,
  not a bare `KeyError`.
- **No CPU limits on containers**, memory limits only — CPU limits cause throttling that presents as
  mysterious latency.
- `services/*/app/db.py` is intentionally duplicated across the two services rather than shared;
  two small helpers do not justify a shared library, and copying keeps each independently deployable.

## Environment

Windows 11, PowerShell 5.1 primary; the Bash tool is also available and takes POSIX syntax. In
PowerShell, `&&` and `||` are parser errors, and `eval $(minikube docker-env)` must be
`minikube docker-env | Invoke-Expression`.

## Documentation

`docs/` holds nine design documents from an evolving design. Only these are live:

| Document | Role |
|---|---|
| `FleetPulse-Simple.md` | The plan this code implements |
| `FleetPulse-Architecture.md` | Request flows and failure behaviour |
| `FleetPulse-Kubernetes.md` | Next: minikube + EKS (Milestones 5–8) |
| `FleetPulse-Addon-Observability.md` | Optional: metrics, dashboards, tracing |
| `FleetPulse-Addon-Notification.md` | Optional: 3rd service via transactional outbox |

`FleetPulse-Blueprint.md` and `FleetPulse-Zero-Cost.md` are **superseded** (4 services, RabbitMQ,
K3s); `FleetPulse-EventBridge.md` is off-path; `FleetPulse-Cost-Model.md` is production-scale
pricing reference. Do not reconcile the superseded documents with the code — they answer a
different question.

## Not built yet

Milestone 3 (Terraform: VPC, EC2, RDS, ECR) and Milestone 4 (GitHub Actions: test → build → ECR →
deploy) are specified in `FleetPulse-Simple.md` §4 and §3 but have no files in the repo. There is
no `infra/` directory, no `.github/workflows/`, and no `docker-compose.prod.yml`.
