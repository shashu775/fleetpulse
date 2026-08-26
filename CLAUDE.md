# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A working logistics platform modelling Delhivery-style parcel operations: **five static front-end
apps served by one nginx container, two FastAPI microservices, PostgreSQL and Redis** — five
containers, one `docker compose up`. Built as a DevOps learning project.

Entry point is **http://localhost/**. The same stack also runs on Docker Desktop's Kubernetes
(`infra/k8s/base/`), and Terraform for an EKS cluster exists but **has never been applied** — no
cloud resources exist. See Infrastructure.

> Previously ten containers (a gateway plus one nginx per app). Consolidated because static assets
> have no independent scaling profile, no state and no independent lifecycle — and since every app
> embeds `packages/web-shared`, a change there rebuilt all five images anyway. See
> `docs/FleetPulse-Architecture-Review.md`.

## Commands

Docker is the only prerequisite. **There is no usable local Python** — `python` resolves to the
Microsoft Store stub — so tests and the simulator run in containers.

```bash
docker compose up --build -d          # all 5 containers
docker compose ps                     # all should be (healthy)
docker compose logs -f dispatch-service
docker compose down                   # stop
docker compose down -v                # stop AND wipe the database
```

### Tests — 43 total, no infrastructure needed

Both service images have a `test` stage.

```bash
docker build --target test -t fp-consignment-test ./services/consignment-service
docker run --rm fp-consignment-test                      # 15 passed

docker build --target test -t fp-dispatch-test ./services/dispatch-service
docker run --rm fp-dispatch-test                         # 28 passed

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

Every statement in it is idempotent (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`), so there is a
no-data-loss path — prefer this when the change is additive:

```bash
docker compose exec -T postgres psql -U fleetadmin -d fleetpulse < db/init.sql
```

Keep it that way: **a new statement in `init.sql` must be safe to re-run**, or this path breaks.

### The driver roster

`dispatch.drivers` seeds 3 drivers per hub, 18 across the 6 hubs, each with one vehicle whose plate
carries the hub city's state code (`KA…` Bangalore, `WB…` Kolkata). `GET /api/v1/drivers?hub_id=`
serves it; `GET /api/v1/hubs` groups it by hub.

It exists because the endpoint used to derive its list from `dispatch.runsheets` — so **a driver did
not exist until someone had already assigned them work**, leaving the driver app's login picker empty
on a fresh database with no way to create the first runsheet. The runsheet count survives as a
`LEFT JOIN`, so an unassigned driver appears with `runsheets: 0` rather than not appearing.

`status` defaults to `ACTIVE`; pass `status=ALL` to include leavers. **Not a blank** — the shared
client's `qs()` strips empty values before they reach the wire.

### Kubernetes (Docker Desktop)

`infra/k8s/base/*.yml` has been applied for real on Docker Desktop's Kubernetes. Images are
`travel2024/{web,consignment,dispatch}:latest` with `imagePullPolicy: IfNotPresent` — Docker
Desktop shares the local Docker daemon, so a locally built image is already present.

```powershell
kubectl create configmap postgres-init-scripts -n fleetpulse --from-file=db/init.sql   # FIRST
kubectl apply -f infra/k8s/base/
kubectl get pods -n fleetpulse
```

The ConfigMap is **hand-made and not part of `kubectl apply -f`** — it vanishes with the namespace
and must exist *before* Postgres starts, or `postgres-0` sits in `ContainerCreating` forever.

**An Ingress object routes nothing without a controller, and Docker Desktop ships none.**
`kubectl get ingressclass` returning "No resources found" is the whole diagnosis: pods healthy,
`kubectl get ingress` looking normal, and nothing listening on `:80`. Use minikube
(`minikube addons enable ingress`) or port-forward.

### Terraform / EKS

```bash
cd infra/terraform/environments/dev
terraform init && terraform plan          # apply takes ~15-20 min
aws eks update-kubeconfig --name fleetpulse-tf --region us-east-1
kubectl delete ingress,pvc --all -A       # BEFORE destroy — see below
terraform destroy
```

## URLs

Everything is served by the single `web` container on port 80. **The per-app ports 3001–3005 no
longer exist** — they belonged to the old one-container-per-app layout.

| App | Path *(always works)* | Hostname *(needs hosts file)* |
|---|---|---|
| Launcher | `/` | `fleetpulse.localhost` |
| Merchant Portal | `/merchant/` | `merchant.fleetpulse.localhost` |
| Driver App | `/driver/` | `driver.fleetpulse.localhost` |
| Hub Scanner | `/hub/` | `hub.fleetpulse.localhost` |
| Customer Tracking | `/track/` | `track.fleetpulse.localhost` |
| Admin Console | `/admin/` | `admin.fleetpulse.localhost` |

Swagger UI remains on `127.0.0.1:8001/docs` and `127.0.0.1:8002/docs`.

**Default to the path URLs** — they always work, from any client.

**⚠️ The hostnames work in a browser and NOT from curl / PowerShell / any script.** Measured on
this machine: `[System.Net.Dns]::GetHostAddresses("fleetpulse.localhost")` fails with *No such
host is known*, and so does `driver.fleetpulse.localhost`, while plain `localhost` resolves to
`127.0.0.1`. **Windows does not resolve `*.localhost`** — the hosts file has no entry and its own
comment says name resolution "is handled within DNS itself", which covers only the bare name.
Browsers (Chrome, Edge, Firefox) resolve `*.localhost` internally per RFC 6761, which is why the
same URL works there and nowhere else.

So a hostname failing from a script proves nothing about the app. To test a vhost from the
command line, bypass DNS with an explicit Host header:

```bash
curl -H "Host: driver.fleetpulse.localhost" http://127.0.0.1/     # 200
```

Add hosts-file entries if you want scripts to use the hostnames. **Never trust a scripted probe
of a `.localhost` name as evidence the site is down** — check `127.0.0.1` first.

**Use `.localhost`, never `.local`.** RFC 6762 reserves `.local` for mDNS/Bonjour, so those lookups
can bypass or race the hosts file and fail intermittently. RFC 6761 reserves `.localhost` for
loopback. `apps/web/nginx.conf` accepts both spellings for backwards compatibility; new work should
use `.localhost`.

**Kubernetes uses a different hostname on purpose: `fleetpulse.test`.** `infra/k8s/base/08-ingress.yml`
sets it, and unlike `.localhost` it **does** need a hosts entry (`127.0.0.1 fleetpulse.test`, or
`minikube ip`). Keeping the two different is what tells you which stack you are looking at. Both `.test` and `.localhost` are RFC 6761 reserved, so neither is wrong — **do not
"reconcile" one to the other.**

### ⚠️ Port binding is a security control here

Every port except `80` is bound to `127.0.0.1`. Before this was fixed, Postgres, Redis (no
password) and the admin console were reachable from the whole LAN. **Never add a `ports:` entry
without the `127.0.0.1:` prefix** unless you genuinely intend it to be public.

## Architecture

```
browser → web :80 ─┬─ /  /merchant/ /driver/ /hub/ /track/ /admin/   (static files, no proxy)
                   ├─ vhosts: <app>.fleetpulse.localhost             (root → that app's dir)
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
`services/dispatch-service/app/consignment_client.py` — the only *service-to-service* call in the
system. Dependencies are **one-directional**: dispatch → consignment, never the reverse.

**Two doors lead to `OUT_FOR_DELIVERY`, and the second one 409s.** Creating a runsheet moves every
parcel there via that client. The driver app *also* moves parcels there itself, calling the
internal `PATCH /api/v1/waybills/{awb}/status` directly from the browser (`markOutForDelivery` in
`apps/driver-app/app.js`) when the driver scans the van load. Since `ALLOWED_TRANSITIONS` has no
`OUT_FOR_DELIVERY → OUT_FOR_DELIVERY` edge, the second call on the same parcel returns **409** —
which is the state machine working, not a bug to fix. Worth knowing that a *frontend* calls the
`internal`-tagged endpoint, so "internal" means "not for merchants", not "services only".

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
helpers (`ui.js`).

**All five apps ship in one image, `apps/web/`.** The app source folders
(`apps/merchant-portal/`, `apps/driver-app/`, …) are still the sources — they just no longer have
their own Dockerfile or nginx.conf. `apps/web/Dockerfile` copies each into a subdirectory of the
same image:

```dockerfile
COPY packages/web-shared/  /usr/share/nginx/html/driver/    # shared first
COPY apps/driver-app/      /usr/share/nginx/html/driver/    # app wins on collision
```

**The shared package is copied into *every* app subdirectory, not once at the root.** That looks
redundant and is deliberate: apps use relative paths (`./api.js`, `./base.css`), which resolve
inside whichever directory served the page. A single root copy would 404 for all five. It is what
made the consolidation a zero-source-change refactor — do not "optimise" it away.

Build context is the **repo root** (`context: .`), because the image needs both `apps/` and
`packages/`.

#### Changing an existing app

Edit `apps/<name>/*` normally, then `docker compose up -d --build web`. There is no volume mount on
`web`, so a rebuild is required to see any front-end change — unlike the two backends, which run
with `uvicorn --reload` and pick up edits live.

#### Adding a new app

Four edits, all required. Missing any one fails in a different, confusing way:

1. **`apps/<name>/index.html`** — use **relative** asset paths (`./base.css`, `./api.js`). Absolute
   paths 404 behind the path prefix.
2. **`apps/web/Dockerfile`** — two `COPY` lines, shared package *first* so the app wins on collision:
   ```dockerfile
   COPY packages/web-shared/ /usr/share/nginx/html/<name>/
   COPY apps/<name>/         /usr/share/nginx/html/<name>/
   ```
3. **`apps/web/nginx.conf`**, default server — a location plus its no-slash redirect:
   ```nginx
   location /<name>/ { try_files $uri $uri/ /<name>/index.html; }
   location = /<name> { return 301 /<name>/; }
   ```
4. **`apps/web/nginx.conf`** — optionally a hostname `server` block. It **must**
   `include /etc/nginx/api_locations.conf;` or `/api/*` 404s on that hostname only, which is a
   genuinely baffling symptom.

Then add it to the launcher (`apps/web/index.html`) and the Admin console's Apps tab
(`APPS` array in `apps/admin-console/app.js`).

### Deliberate choices that look like omissions

- **No `gps_pings` table.** ~864k writes/day of data stale in 10 seconds. Redis only, 1-hour TTL.
  `POST /api/v1/gps` returns **202 Accepted**, not 201, because the write is non-durable.
- **Shipping labels are HTML, not PDF or ZPL.**
  `GET /api/v1/waybills/{awb}/label` renders through
  `services/consignment-service/app/labels.py` and is meant to be opened in a browser and printed.
  It is the one place the backend emits markup, so every interpolated field goes through
  `html.escape()` — keep it that way when adding fields.
- **No FK crosses a schema boundary.** `runsheet_items.awb` and `delivery_attempts.awb` reference
  `consignment.waybills` by value; a real FK would block a future service split.
- **`runsheets` copies the driver's name and vehicle instead of joining `dispatch.drivers`.** Not
  a normalisation slip — a runsheet is a historical record of who drove what that day, so it must
  still read correctly after the driver changes vehicle or leaves. For the same reason drivers are
  marked `INACTIVE`, never deleted.
- **`scan_events` is append-only.** `waybills.current_status` is a denormalised convenience column
  rebuildable from it.
- **Partial failure is reported, not hidden.** `POST /runsheets` returns `assigned[]` *and*
  `failed[]`; `POST /delivery` returns **207 Multi-Status** when the attempt saved but the status
  update failed. Not bugs — without a broker there is no cross-service transaction. The intended fix
  is the outbox pattern in `docs/FleetPulse-Addon-Notification.md`.
- **No authentication anywhere.** Driver "login" is a `localStorage` picker; anyone can be any
  driver, hub or merchant. The admin console can create runsheets and read every parcel. Real auth
  is **backend first** — frontend-only auth is decoration when anyone can `curl` the API.
- **Proof of delivery is captured but never verified.** The driver app has a full POD modal and
  `dispatch.delivery_attempts` has `pod_type` / `pod_receiver` / `pod_data`, so it looks finished.
  It is not: **nothing generates an OTP, nothing sends one, and nothing checks one.** The only
  validation is `/^\d{4,6}$/` in the browser; server-side `pod_data` is an optional string stored
  in plaintext. Any six digits complete a delivery, and so does omitting the field. `SIGNATURE`
  captures real evidence; `PHOTO` is accepted by the API and has **no UI**. Free-text
  `pod_receiver` is correct and not a bug — parcels legitimately go to neighbours and reception.
  The design is `docs/FleetPulse-Addon-OTP.md`.

## Infrastructure

Three deployment targets exist, at three different maturities: **Compose (works), Kubernetes on
Docker Desktop (applied and debugged), EKS via Terraform + GitHub Actions (written, cost-guarded).**

### The k8s manifests are `.yml`, and the `.yaml` ones are dead

`infra/k8s/base/*.yml` is the live set. The `.yaml` files of the same numbers are **staged
deletions** (git shows `D 00-namespace.yaml` … `D 09-hpa.yaml`) and `infra/k8s/base_bkp/` is a copy
of the old set. Do not edit either — and do not "restore" the deletions. Two exceptions still carry
the `.yaml` extension because they were never renamed: `10-migration-job.yaml`, plus an **empty
`11-ingress-r.yml`** (0 bytes, a placeholder).

### Kubernetes and Compose deliberately disagree on the Postgres user

| | Compose | Kubernetes |
|---|---|---|
| `POSTGRES_USER` | `fleetadmin` (`.env`) | `fleetpulse` (`02-secrets.yml`) |
| Ingress host | `*.fleetpulse.localhost` | `fleetpulse.test` (needs a hosts entry) |

Both are internally consistent; **do not reconcile them.** A `psql -U` or `pg_isready -U` copied
from one into the other fails with `role does not exist`, and because `init.sql` runs only on an
empty data directory, the fix is deleting `pvc/data-postgres-0` — not editing the manifest.

`01-configmap.yml` keys become env vars **verbatim and case-sensitively**. A misspelled `REDIS_URL`
or `CONSIGNMENT_URL` makes the app fall back to a built-in default silently rather than crash.

### Terraform: the cost guards are the design

`infra/terraform/environments/dev/` builds an EKS cluster (`fleetpulse-tf`) sized so the **only**
recurring charge is the control plane — ~$0.15/hr, ~$107/month if left up. That holds only because
several module defaults are turned **off**, each marked `COST GUARD` in `main.tf`:
`enable_nat_gateway = false` (~$33/mo), `create_kms_key = false`, `cluster_enabled_log_types = []`
(~$15–45/mo). Public subnets only, because without NAT a private subnet is unroutable and nodes
never join. **Do not remove a cost guard without knowing its price.**

Two consequences worth knowing before debugging them cold:

- **`enable_ebs_csi` defaults to `false`**, so there is no default StorageClass, so the Postgres PVC
  stays `Pending` forever and both services fail readiness behind it. Deliberate — Docker Desktop
  hides this with a built-in hostpath provisioner; EKS ships none. Set it `true` (~$0.16/mo) to make
  Postgres work. The `storage_warning` output says which mode you are in.
- **`node_instance_type` must not be `t3.micro`.** The VPC CNI gives every pod a real VPC IP from
  the node's ENIs, so max-pods is a *networking* limit: t3.micro = 4 pods, and kube-system alone
  needs ~4.

State is in S3 (`versions.tf`), created by hand on purpose — **Terraform must never be able to
delete the record of what it has to delete.** `.terraform.lock.hcl` is deliberately **not**
gitignored; it pins provider checksums so CI and a laptop resolve the same versions.

### CI: the plan is the review artifact

`.github/workflows/terraform.yml` — PRs touching `infra/terraform/**` get their plan posted as a
single updated PR comment; a push to `code2` applies **the saved plan artifact**, never a freshly
computed one. Auth is GitHub OIDC → an IAM role in repo variable `AWS_ROLE_ARN`; no long-lived AWS
keys. A `Cost guard` step greps the plan text for `aws_nat_gateway|aws_kms_key|aws_cloudwatch_log_group|aws_eip|aws_lb`
and fails the build — a module upgrade that flips a default back on breaks CI instead of the budget.

⚠️ **`code2` is both the working branch and the deploy branch**, so a push changes AWS with no gate.
The apply job already targets a GitHub Environment named `dev`; creating that Environment with a
required reviewer is what turns it into an approval gate.

`.github/workflows/terraform-destroy.yml` is manual-only and requires typing `destroy`. It deletes
Ingresses and PVCs **before** `terraform destroy`, because those create real ALBs and EBS volumes
that are **not in Terraform state** — destroy the cluster with them present and they orphan, billing
quietly forever. It then re-lists clusters, load balancers and available volumes to verify.

One-time setup (S3 bucket, OIDC provider, `AWS_ROLE_ARN`) is documented in the workflow header and
**has not necessarily been done** — check before assuming a run will succeed.

## Gotchas that have already cost time

**nginx healthchecks must use `127.0.0.1`, not `localhost`.** In `nginx:alpine`, `localhost`
resolves to `::1` while `listen 80` binds IPv4 only — `wget http://localhost/healthz` returns
*connection refused* from a container serving perfectly. Every app image hit this and reported
`unhealthy` while working, which makes `docker compose ps` lie. The two `python:3.12-slim` service
images keep `localhost:8000` in their `HEALTHCHECK` and are fine — the trap is nginx-specific, so
there is nothing to "fix" there.

**nginx `return 301 /x/` drops the port.** By default nginx builds an absolute `Location` from
`$host`, so `/driver` on `:8080` redirected to `http://localhost/driver/` — a different server.
`apps/web/nginx.conf` sets `absolute_redirect off;` to emit a relative header instead.

**Changing `POSTGRES_PASSWORD` does not change an existing database.** Postgres applies that
variable only when it initialises a *fresh* volume. Editing `.env` against an existing `pgdata`
leaves the old password in place and every service fails to authenticate. Rotate in place instead
of wiping data:

```bash
docker compose exec -T postgres psql -U fleetadmin -d fleetpulse \
  -c "ALTER USER fleetadmin WITH PASSWORD 'new-password';"
docker compose restart consignment-service dispatch-service
```

**Apps must use relative asset paths** (`./app.js`, `./base.css`). Absolute `/app.js` requested from
`localhost/driver/` escapes the gateway prefix and 404s.

**`hidden` loses to any `display` rule — keep `[hidden]` first in `base.css`.** `ui.js`'s `show()`
toggles the `hidden` *attribute*, which hides only via the user agent's `[hidden] { display: none }`.
Author styles outrank the UA sheet, so `.modal-backdrop { display: grid }` silently made `hidden`
a no-op: **the driver app's POD modal rendered on every page load**, floating over the driver
picker, and `closePod()` did nothing. `#switch-driver` (class `btn`) was visible on the login view
for the same reason. Fixed by `[hidden] { display: none !important; }` at the top of
`packages/web-shared/base.css` — it must stay above the component rules it outranks. Every
individual file read correctly; the bug existed only in the cascade between them.

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
  latency. *Neither is set in `docker-compose.yml` today* (no `mem_limit`, no `deploy.resources`);
  this records which one to add when limits arrive. The one memory bound that does exist is Redis's
  own `--maxmemory 64mb --maxmemory-policy allkeys-lru`.
- `services/*/app/db.py` is intentionally duplicated rather than shared; two small helpers do not
  justify a library, and copying keeps each service independently deployable.

## Git

Branch **`code2`**, remote `https://github.com/shashu775/fleetpulse.git`, five commits
(`b0a2f26 terraform` is HEAD). `origin/HEAD` points at `master`, but **`code2` is the branch that
matters** — it is what the CI workflows deploy from. Local branches `code`, `code1`, `master` are
stale.

The container consolidation and Terraform are now committed. What remains uncommitted is the
Kubernetes and CI work:

- `?? .github/` — both Terraform workflows, **untracked**. They cannot have run yet.
- `?? docs/k8s-lessons/` — the five debugging write-ups
- `D infra/k8s/base/*.yaml` + `?? infra/k8s/base/*.yml` — the extension rename, staged as
  delete-plus-add rather than as a rename. `?? infra/k8s/base_bkp/` is the old copy.
- `M .gitignore` — un-ignores `.terraform.lock.hcl` (see Infrastructure)
- `M infra/terraform/environments/dev/{outputs,versions}.tf`, `?? .terraform.lock.hcl`

Do not "restore" those deletions — they are intentional. Commit only when asked.

`.env` is gitignored and now holds a **generated** `POSTGRES_PASSWORD`, not the `.env.example`
default. Rotating it again requires `ALTER USER` (see Gotchas), not just editing the file.

## Environment

Windows 11, PowerShell 5.1 primary; the Bash tool is also available and takes POSIX syntax. In
PowerShell `&&` and `||` are parser errors, and `eval $(minikube docker-env)` must be
`minikube docker-env | Invoke-Expression`.

## Documentation

`docs/` holds twelve design documents from an evolving design. Live ones:

| Document | Role |
|---|---|
| `FleetPulse-Architecture.md` | **How the running system works** — request flows, failure behaviour |
| `FleetPulse-Apps.md` | Front-end structure, full API surface, routing |
| `FleetPulse-Architecture-Review.md` | Why 10 containers became 5; the merge-vs-split analysis |
| `FleetPulse-Simple.md` | The original two-service build plan (Milestones 1–2 done) |
| `FleetPulse-Kubernetes.md` | The k8s + EKS plan `infra/k8s/` and `infra/terraform/` were built from |
| `FleetPulse-Addon-Observability.md` | Optional: metrics, dashboards, tracing |
| `FleetPulse-Addon-Notification.md` | Optional: 3rd service via transactional outbox |
| `FleetPulse-Addon-OTP.md` | Optional: verified delivery codes. **Build Notification first** |

`docs/k8s-lessons/` is a separate, higher-value set: five write-ups of failures that actually
happened bringing `infra/k8s/base/` up. **Read `docs/k8s-lessons/README.md` before debugging a pod**
— it maps the `kubectl get pods` STATUS column to the page that explains it. Its thesis:
*Kubernetes connects things by name and nothing checks that the names match* — seven of nine
problems in one session were a name in file A not matching a name in file B, and none was an
architecture problem.

`FleetPulse-Blueprint.md` and `FleetPulse-Zero-Cost.md` are **superseded** (4 services, RabbitMQ,
K3s); `FleetPulse-EventBridge.md` is off-path; `FleetPulse-Cost-Model.md` is production-scale
pricing reference. Do not reconcile the superseded documents with the code.

## Not built

- **Nothing has been applied to AWS.** The Terraform is written and the workflows exist, but
  `.github/` is untracked, so no run has ever happened, and the one-time setup in the workflow
  header (S3 state bucket, OIDC provider, `AWS_ROLE_ARN`) may not be done. `infra/terraform/modules/`
  holds no FleetPulse modules — everything is inlined in `environments/dev/` against upstream
  registry modules. There is no `prod` environment.
- **`infra/k8s/overlays/` does not exist** — there is no Kustomize layering, just `base/`.
- Two empty stray directories sit at the repo root: **`fleetpulse/`** and **`simulators/`**
  (plural). Neither is used by anything — the real simulator is the singular **`simulator/`**.
- **`infra/helm/fleetpulse/` is an untouched `helm create` scaffold**, not a FleetPulse chart. It
  deploys **one** Deployment of the stock **`nginx`** image (`values.yaml`: `image.repository:
  nginx`, `appVersion: "1.16.0"`) with the default Service, Ingress, HTTPRoute, ServiceAccount, HPA
  and test hook. Nothing in it references the five containers, Postgres, Redis or any FleetPulse
  image. Templating the real stack means rewriting it — start from `docs/FleetPulse-Kubernetes.md`,
  and do not read the existing values as design intent.
- **No Python linter or formatter.** No ruff, black, flake8 or mypy in either `requirements.txt`,
  and no config for them; `pytest` is the only Python quality gate. Do not add one unasked.
  Terraform is the exception — CI runs `terraform fmt -check -recursive` and `terraform validate`,
  so **unformatted HCL fails the build.** Run `terraform fmt` before pushing infra changes.
- **The two service test suites are not run by any workflow.** Only Terraform has CI.
- **Merchant**: bulk CSV upload, barcode label sheets, pickup requests, order list. The AWB is shown
  once after booking and cannot be found again from the UI — `GET /api/v1/waybills` exists and is
  already in the shared client, only the screen is missing.
- **Hub**: bag tagging, transit manifests, camera QR scanning.
