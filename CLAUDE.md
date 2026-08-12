# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state: design complete, zero implementation

There is **no source code and there are zero commits.** The repo contains 8 config/scaffold files,
9 design documents under `docs/`, and nothing else. Everything is untracked.

There are **no build, lint, or test commands** because there is nothing to build. Do not infer any
from directory names — `simulators/`, `infra/helm/`, `infra/terraform/`, `.github/workflows/`, and
`infra/docker/prometheus/` are all empty. When you create the first runnable thing, replace this
section with commands that actually work.

## ⚠️ The scaffold contradicts the active plan

This is the most important thing to know before touching anything.

The committed files encode decisions from an earlier, larger design. The plan the user selected —
`docs/FleetPulse-Simple.md` — **reverses most of them.** A future session that trusts the scaffold
will build the wrong system.

| Scaffold on disk says | Active plan (`FleetPulse-Simple.md`) says |
|---|---|
| 4 services (`consignment`, `facility`, `dispatch`, `notification`) | **2 services**: `consignment-service` (booking + hub scans) and `dispatch-service` (runsheets + GPS + delivery) |
| `RABBITMQ_USER` / `RABBITMQ_PASS` in `.env.example` | **No message broker at all.** Services talk over synchronous HTTP REST |
| `01-init-databases.sql` creates `consignment_db`, `facility_db`, `dispatch_db` | **One shared database**, two Postgres *schemas* (`consignment`, `dispatch`) |
| Go or Node implied by `.gitignore` (`bin/`, `*.exe`, `node_modules/`) | **Python 3.12 + FastAPI** |
| Language undecided | Decided: FastAPI, `psycopg` v3, `httpx`, `pytest` |

**Follow the plan, not the scaffold.** Concretely, when implementation starts:

- Delete `services/facility-service/` and `services/notification-service/`. Facility responsibilities
  fold into `consignment-service`; notification becomes a later optional add-on with a *different*
  design (see below).
- Rewrite `.env.example` — drop the `RABBITMQ_*` keys, add `DATABASE_URL`, `REDIS_URL`,
  `CONSIGNMENT_URL`.
- Replace `infra/docker/postgres-init/01-init-databases.sql` with `db/init.sql` creating two schemas
  in one database.
- Replace all four placeholder Dockerfiles. They are identical no-ops:
  ```dockerfile
  FROM alpine:latest
  CMD ["echo", "Service running..."]
  ```

## Which document to follow

Nine documents exist because requirements changed several times during design. Only three are live.

| Document | Status |
|---|---|
| **`docs/FleetPulse-Simple.md`** | ⭐ **The plan. Build from this.** 2 services, Compose, Terraform, GitHub Actions, 4 milestones |
| **`docs/FleetPulse-Architecture.md`** | ⭐ How the system works — request flows, failure behaviour. Read alongside Simple |
| `docs/FleetPulse-Kubernetes.md` | Active. Milestones 5–8, after Simple works. minikube + EKS |
| `docs/FleetPulse-Addon-Observability.md` | Optional add-on, after the core works |
| `docs/FleetPulse-Addon-Notification.md` | Optional add-on. A 3rd service using a **transactional outbox**, not a queue consumer |
| `docs/FleetPulse-Blueprint.md` | ❌ Superseded. 4 services, RabbitMQ, EKS, 14 weeks — too large |
| `docs/FleetPulse-Zero-Cost.md` | ❌ Superseded by Simple. Used K3s + 4 services + NATS |
| `docs/FleetPulse-EventBridge.md` | ⚠️ Off-path. Only relevant if brokers are revisited |
| `docs/FleetPulse-Cost-Model.md` | 📖 Reference. AWS pricing at production scale, not this build |

Do not reconcile the superseded documents with the active ones — they answer a different question
and are kept for their reasoning, not their instructions.

## Architecture of the active plan

Two FastAPI services. One shared PostgreSQL database with a schema per service. Redis for the
tracking cache and live GPS positions.

**The dependency graph is one-directional: `dispatch-service` → `consignment-service`.** Consignment
never calls Dispatch. Preserve this — circular service dependencies cause startup ordering and
cascading-timeout problems.

**`consignment-service` is the system of record for parcel status.** It owns `ALLOWED_TRANSITIONS`,
the state machine that rejects illegal moves with HTTP 409. Dispatch drives the last three
transitions (`OUT_FOR_DELIVERY`, `DELIVERED`, `RTO`) but performs none of them — it calls
`PATCH /api/v1/waybills/{awb}/status`.

**Dispatch must never write to the `consignment` schema**, even though the connection and
credentials make it trivially possible. The state machine must have exactly one enforcement point.
This single constraint is what makes the project microservices rather than one app in two folders.

Two data-placement decisions worth preserving:

- **GPS pings go to Redis only, never Postgres.** 100 vehicles at one ping per 10s is ~864k writes
  per day of data whose value expires in seconds. The GPS endpoint returns `202 Accepted`, not
  `201`, because the write is deliberately non-durable.
- **`scan_events` is append-only.** `waybills.current_status` is a denormalised convenience column;
  the event table is the truth and could rebuild it.

**Failure behaviour is reported honestly rather than hidden.** Without a broker there is no
cross-service transaction, so `POST /runsheets` returns `assigned[]` and `failed[]` lists, and
`POST /delivery` can return `207 Multi-Status`. Do not "fix" these by swallowing errors — the
notification add-on's outbox pattern is the intended solution.

## Every existing file has a UTF-8 BOM

All 8 scaffold files were written by PowerShell on Windows and begin with `U+FEFF`, including the
Dockerfiles and `01-init-databases.sql`. A BOM ahead of `FROM` or `CREATE DATABASE` breaks the tools
that consume them.

The Write tool does not add a BOM; `Out-File` and `Set-Content -Encoding utf8` in Windows PowerShell
5.1 do. If you hit an unexplained syntax error on line 1 of anything, check for a BOM first.

## Configuration contract

`.env.example` is the contract; `.env` is gitignored. When code needs a new variable, add it to
`.env.example` with a safe placeholder **in the same change**.

Current keys are stale (see the scaffold-contradiction table above). The plan's set is in
`FleetPulse-Simple.md` §2.4.

## Environment

Windows 11, PowerShell 5.1 primary. Bash is available but takes POSIX syntax — `&&` and `||` are
parser errors in PowerShell, and here-strings need `@'...'@` with the terminator at column 0.

Kubernetes work targets minikube on Docker Desktop; `eval $(minikube docker-env)` does not work in
PowerShell — use `minikube docker-env | Invoke-Expression`.

## Git

Initialized, `master` branch, **no commits, no remote.** Making the first commit is Milestone 1 of
the active plan and is the user's step to take — do not commit unless asked.
