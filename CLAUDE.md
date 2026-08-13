# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A working logistics platform modelling Delhivery-style parcel operations: **five static front-end
apps behind an nginx gateway, two FastAPI microservices, PostgreSQL and Redis** — ten containers,
one `docker compose up`. Built as a DevOps learning project.

Entry point is **http://localhost/**. Everything runs locally; no cloud resources exist yet.

## Commands

Docker is the only prerequisite. **There is no usable local Python** — `python` resolves to the
Microsoft Store stub — so tests and the simulator run in containers.

```bash
docker compose up --build -d          # all 10 containers
docker compose ps                     # all should be (healthy)
docker compose logs -f dispatch-service
docker compose down                   # stop
docker compose down -v                # stop AND wipe the database
```

### Tests — 36 total, no infrastructure needed

Both service images have a `test` stage.

```bash
docker build --target test -t fp-consignment-test ./services/consignment-service
docker run --rm fp-consignment-test                      # 15 passed

docker build --target test -t fp-dispatch-test ./services/dispatch-service
docker run --rm fp-dispatch-test                         # 21 passed

# A single test or pattern:
docker run --rm fp-dispatch-test pytest -q -k gps
docker run --rm fp-consignment-test pytest -q tests/test_waybills.py::test_health_returns_ok
```

**Tests must never require a database or Redis.** `TestClient(app)` is used *without* a context
manager so FastAPI's lifespan never runs; every case either hits a handler that touches nothing or
is rejected by Pydantic before the handler executes. A test that reaches a live handler fails with
`RuntimeError: DATABASE_URL is not set`. Preserve this property — it is what lets tests run in CI
with nothing else up.

### Seed data

```bash
docker compose --profile sim run --rm simulator --parcels 20
docker compose --profile sim run --rm simulator --parcels 12 --seed 42   # reproducible
```

Books parcels, scans them through hubs, creates runsheets, streams GPS, delivers/RTOs. The driver
app and admin console need runsheets to exist, so run this before demoing them.

### Database

```bash
docker compose exec postgres psql -U fleetadmin -d fleetpulse
docker compose exec postgres psql -U fleetadmin -d fleetpulse -c "\dt dispatch.*"
```

⚠️ `db/init.sql` runs **only on a fresh volume**. After editing it:
`docker compose down -v && docker compose up --build -d` — which destroys all parcel data.

## URLs

| App | Path *(always works)* | Hostname *(needs hosts file)* | Port |
|---|---|---|---|
| Launcher | `/` | `fleetpulse.localhost` | 80 |
| Merchant Portal | `/merchant/` | `merchant.fleetpulse.localhost` | 3001 |
| Driver App | `/driver/` | `driver.fleetpulse.localhost` | 3002 |
| Hub Scanner | `/hub/` | `hub.fleetpulse.localhost` | 3003 |
| Customer Tracking | `/track/` | `track.fleetpulse.localhost` | 3004 |
| Admin Console | `/admin/` | `admin.fleetpulse.localhost` | 3005 |

**Default to the path URLs.** The hostnames need a hosts-file entry that is probably not present —
check with `Select-String -Path $env:WINDIR\System32\drivers\etc\hosts -Pattern fleetpulse` before
telling anyone a hostname works.

**Use `.localhost`, never `.local`.** RFC 6762 reserves `.local` for mDNS/Bonjour, so those lookups
can bypass or race the hosts file and fail intermittently. RFC 6761 reserves `.localhost` for
loopback and browsers resolve `*.localhost` internally. The gateway accepts both spellings for
backwards compatibility; new work should use `.localhost`.

## Architecture

```
browser → gateway :80 ─┬─ /merchant/ /driver/ /hub/ /track/ /admin/ → 5 static app containers
                       ├─ /api/consignment/* → consignment-service:8000
                       └─ /api/dispatch/*    → dispatch-service:8000
                                                     ↓            ↓
                                          PostgreSQL (2 schemas) · Redis
```

### The rule that defines this codebase

**`dispatch-service` must never write to the `consignment` schema.** Same database, same
credentials, trivially possible — and forbidden. `consignment-service` owns `ALLOWED_TRANSITIONS`
(`services/consignment-service/app/main.py`), the state machine returning **409** on illegal moves.
Two enforcement points would eventually disagree.

All cross-service traffic goes through
`services/dispatch-service/app/consignment_client.py` — the only inter-service call in the system.
Dependencies are **one-directional**: dispatch → consignment, never the reverse.

### The two Redis roles are deliberately different

| | consignment `app/cache.py` | dispatch `app/cache.py` |
|---|---|---|
| Role | **Cache** — Postgres is the truth | **Store** — nothing else holds GPS |
| On failure | Fails soft: logs, returns `None`, behaves like a miss | Propagates: endpoints return **503** |

Do not harmonise these. A cache that can take down the service is worse than no cache; a store that
silently discards writes is worse than an error.

Cache invalidation is `DEL`, never overwrite — safe if the surrounding transaction rolls back.

### Front-end structure

`apps/*` are **plain HTML + CSS + ES modules served by nginx — no framework, no build step, no
`node_modules`.** That is a deliberate decision (see `docs/FleetPulse-Apps.md` §0): four Node
toolchains would add ~1.5 GB and minutes per rebuild to a project whose purpose is DevOps practice.
Do not introduce a bundler without being asked.

`packages/web-shared/` holds the shared API client (`api.js`), design system (`base.css`) and UI
helpers (`ui.js`). Each app image is built **from the repo root** so it can copy the package in:

```yaml
build:
  context: .                              # repo root, NOT the app folder
  dockerfile: apps/driver-app/Dockerfile
```

Changing `packages/web-shared` requires rebuilding **every** app image.

### Deliberate choices that look like omissions

- **No `gps_pings` table.** ~864k writes/day of data stale in 10 seconds. Redis only, 1-hour TTL.
  `POST /api/v1/gps` returns **202 Accepted**, not 201, because the write is non-durable.
- **No FK crosses a schema boundary.** `runsheet_items.awb` and `delivery_attempts.awb` reference
  `consignment.waybills` by value; a real FK would block a future service split.
- **`scan_events` is append-only.** `waybills.current_status` is a denormalised convenience column
  rebuildable from it.
- **Partial failure is reported, not hidden.** `POST /runsheets` returns `assigned[]` *and*
  `failed[]`; `POST /delivery` returns **207 Multi-Status** when the attempt saved but the status
  update failed. Not bugs — without a broker there is no cross-service transaction. The intended fix
  is the outbox pattern in `docs/FleetPulse-Addon-Notification.md`.
- **No authentication anywhere.** Driver "login" is a `localStorage` picker; anyone can be any
  driver, hub or merchant. The admin console can create runsheets and read every parcel. Real auth
  is **backend first** — frontend-only auth is decoration when anyone can `curl` the API.

## Gotchas that have already cost time

**Container healthchecks must use `127.0.0.1`, not `localhost`.** In `nginx:alpine`, `localhost`
resolves to `::1` while `listen 80` binds IPv4 only — `wget http://localhost/healthz` returns
*connection refused* from a container serving perfectly. All six app images hit this; every one
reported `unhealthy` while working, which makes `docker compose ps` lie.

**Apps must use relative asset paths** (`./app.js`, `./base.css`). Absolute `/app.js` requested from
`localhost/driver/` escapes the gateway prefix and 404s.

**Docker Compose silently keeps old containers when a build fails.** A malformed Dockerfile produced
two "successful"-looking `up --build` runs with stale containers still serving. Always confirm with
`docker compose ps` uptime rather than trusting build output.

**`sed` multi-line edits on Dockerfiles are fragile.** An inserted comment inside a `HEALTHCHECK`
line-continuation is a parse error. Prefer the Edit tool or regenerate the block.

## Conventions

- **`.env.example` is the contract.** New env var in code → add it there with a safe placeholder in
  the same change. `docker-compose.yml` also carries defaults, so the stack starts without a `.env`.
- **Service discovery uses service names** (`http://consignment-service:8000`). Docker Compose DNS
  and Kubernetes DNS both resolve it — that is why no code changes when moving to a cluster.
- **Missing config fails loudly.** `db.py` raises `RuntimeError` naming the variable and the fix.
- **Memory limits yes, CPU limits no** — CPU limits cause throttling that presents as mysterious
  latency.
- `services/*/app/db.py` is intentionally duplicated rather than shared; two small helpers do not
  justify a library, and copying keeps each service independently deployable.

## Git

Branch **`code1`**, remote `https://github.com/shashu775/fleetpulse.git`, two commits so far.

**`apps/` and `packages/` are untracked**, and `frontend/` (the retired single-page console) shows
as deleted. The whole five-app front end is uncommitted. Commit only when asked.

## Environment

Windows 11, PowerShell 5.1 primary; the Bash tool is also available and takes POSIX syntax. In
PowerShell `&&` and `||` are parser errors, and `eval $(minikube docker-env)` must be
`minikube docker-env | Invoke-Expression`.

## Documentation

`docs/` holds ten design documents from an evolving design. Live ones:

| Document | Role |
|---|---|
| `FleetPulse-Architecture.md` | **How the running system works** — request flows, failure behaviour |
| `FleetPulse-Apps.md` | Front-end structure, full API surface, routing |
| `FleetPulse-Simple.md` | The original two-service build plan (Milestones 1–2 done) |
| `FleetPulse-Kubernetes.md` | Next: minikube + EKS |
| `FleetPulse-Addon-Observability.md` | Optional: metrics, dashboards, tracing |
| `FleetPulse-Addon-Notification.md` | Optional: 3rd service via transactional outbox |

`FleetPulse-Blueprint.md` and `FleetPulse-Zero-Cost.md` are **superseded** (4 services, RabbitMQ,
K3s); `FleetPulse-EventBridge.md` is off-path; `FleetPulse-Cost-Model.md` is production-scale
pricing reference. Do not reconcile the superseded documents with the code.

## Not built

- **Milestone 3–4** of `FleetPulse-Simple.md`: Terraform (VPC, EC2, RDS, ECR) and GitHub Actions.
  There is no `infra/` directory and `.github/` is empty.
- **Merchant**: bulk CSV upload, barcode label sheets, pickup requests, order list. The AWB is shown
  once after booking and cannot be found again from the UI — `GET /api/v1/waybills` exists and is
  already in the shared client, only the screen is missing.
- **Hub**: bag tagging, transit manifests, camera QR scanning.
