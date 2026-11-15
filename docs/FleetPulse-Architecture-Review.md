# FleetPulse Architecture Review

A critique of the current 10-container runtime, with a concrete simplification path to 5 and a
Kubernetes translation.

---

## 0. Verdict up front

| Question | Answer |
|---|---|
| Is 10 containers too many? | **Yes — but only 6 of them are the problem.** The five static apps plus the gateway should be one container |
| Should the 5 frontends be consolidated? | **Yes, to one.** Static assets have no runtime, no state, and no independent scaling profile |
| Should the 2 backends be merged? | **On the merits, yes. For your stated purpose, no.** §3 has the honest test |
| Three entry points per app? | **Keep the gateway, keep the ports, but bind them to loopback.** §1 |
| Realistic target | **10 → 5 containers**, no route changes, no schema changes |

There is also one **live security issue** that is not theoretical (§1.1). Fix that today regardless
of everything else.

---

## 1. Ingress & Routing Critique

### 1.1 🔴 Every service binds `0.0.0.0` — including Postgres and Redis

Verified on the running stack:

```
fp-postgres   0.0.0.0:5432->5432/tcp     ← database, LAN-reachable
fp-redis      0.0.0.0:6379->6379/tcp     ← no password at all
fp-admin      0.0.0.0:3005->80/tcp       ← can create runsheets, read every parcel
fp-consignment 0.0.0.0:8001->8000/tcp    ← full API, no auth
```

This machine has LAN addresses `192.168.152.1` and `192.168.78.1`. On any shared network — an
office, a café, a hotel — every one of those is reachable by anyone on the same subnet. Concretely:

- **Redis** requires no credentials. `redis-cli -h <your-ip>` gives full read/write, including
  wiping every driver's live position.
- **Postgres** accepts `fleetadmin` / `localdevpassword` — the default in `.env.example`, which is
  almost certainly still in use locally.
- **The admin console** needs no login and can create runsheets and enumerate every parcel,
  consignee name, phone number and address in the system.

This is not a design flaw in the architecture; it is a Compose default (`ports:` binds all
interfaces). The fix is one prefix per line:

```yaml
ports:
  - "127.0.0.1:3001:80"      # loopback only
```

**Apply this to all nine published ports.** Keep `80` on `0.0.0.0` only if you actually want to
reach the gateway from your phone; otherwise bind that too.

### 1.2 Direct ports bypass every control the gateway will ever have

Today the gateway enforces nothing, so `localhost:3002` and `localhost/driver/` are equivalent. That
changes the moment you add anything at the gateway — auth, rate limiting, security headers, TLS,
request logging, a WAF. Each of those becomes **five holes**, because `:3001`–`:3005` reach the app
containers directly and the gateway never sees the request.

This is the classic "debug affordance becomes production bypass" pattern. The affordance is
genuinely useful — it isolates whether a fault is in the app or the routing — so don't delete it.
Instead:

- Bind the direct ports to `127.0.0.1` (§1.1), making them a developer-only door
- Declare the gateway the **only supported entry point** in docs and in `CLAUDE.md`
- When auth arrives, put it in the app or a shared middleware, **not** only at the gateway — never
  rely on a network path being the only way in

### 1.3 Three URLs per app has a cookie problem waiting

Not an issue yet, because there is no authentication. It becomes one immediately when there is.

Cookies scope to origin. `localhost/driver/` and `driver.fleetpulse.localhost` are **different
origins**, so a session cookie set on one is invisible to the other. Worse, all five path-based apps
share the `localhost` origin — a cookie set by the merchant portal is readable by the admin console
and vice versa. With per-hostname apps you get clean isolation; with path-based you do not.

If you keep both entry styles past the point of adding auth, pick **one canonical origin** for
session cookies and redirect the others to it.

### 1.4 The documentation cost is real and has already bitten

Three URL styles produced a wrong table in two documents, hostnames that never resolved, and a
`.local` suffix that would have failed intermittently even once configured. That is the maintenance
cost of three entry points, paid in confusion rather than CPU.

**Recommendation:** demote the direct ports from "documented feature" to "debug escape hatch,
loopback-only". Lead with paths, offer hostnames as optional.

---

## 2. Should the five static frontends be one container?

### 2.1 What you are actually paying for

Each app container is nginx serving three to five files. Measured:

| | Per app | × 5 |
|---|---|---|
| Memory | ~10 MB | ~50 MB |
| Image (shared `nginx:alpine` base) | ~74 MB, mostly shared layers | negligible incremental |

**Resource cost is not the argument.** The cost is structural:

- 5 Dockerfiles and 5 `nginx.conf` files that are byte-identical apart from a path
- 5 Compose entries, 5 healthchecks, 5 things to forget to update
- **Any change to `packages/web-shared` rebuilds all five images** — so the "independent
  deployability" benefit is already void
- In Kubernetes: 5 Deployments + 5 Services + 5 sets of probes and resource limits

### 2.2 The test for whether a component deserves its own container

Ask three questions. A separate container is justified if you answer yes to any:

| Test | Static apps |
|---|---|
| Does it have an independent **scaling profile**? | ❌ Static files. nginx serves thousands/sec from one replica |
| Does it have independent **state or lifecycle**? | ❌ No state. All five ship together |
| Can it **fail independently** in a way that matters? | ❌ If nginx dies, all five die anyway — same process class |

Three no's. **Five containers here is over-decomposition** — microservice reflexes applied to static
assets.

### 2.3 Recommendation: one `web` container, folding in the gateway

The gateway is also nginx. Serving the apps *and* proxying the APIs from a single nginx is not a
compromise — it is what nginx is for.

**6 containers → 1.**

> ⚠️ **One exception, and it is yours.** You are learning Kubernetes on this app. Deploying two or
> three separate app Services makes the Ingress fan-out lesson concrete. Keep them split *for that
> exercise*, then consolidate — or note that Ingress fan-out is equally well demonstrated by routing
> `/api/consignment/*` and `/api/dispatch/*` to two different backend Services, which you need
> anyway. The learning goal and the architecture goal genuinely differ here; don't let the learning
> setup become the permanent design.

---

## 3. Merge the two backend services?

The most interesting question here, and the one where the honest answer is uncomfortable.

### 3.1 Apply the same test

| Test | consignment vs dispatch |
|---|---|
| Independent **data store**? | ❌ Same PostgreSQL instance, same Redis. Two schemas is a *logical* boundary, not a physical one |
| Independent **deploy cadence**? | ❌ Same repo, same `docker compose up`, same person |
| Independent **scaling profile**? | ⚠️ Partially — GPS ingest is high-frequency, booking is not. But that is **one endpoint**, not a service |
| Independent **failure domain**? | ⚠️ Dispatch surviving a consignment outage is real, but it degrades to `207` — barely useful |
| Independent **team ownership**? | ❌ One person |

**Four no's and two partials.** By the standard test, these two services do not justify being
separate processes.

### 3.2 The tell: costs you pay *only* because they are split

These exist purely as a consequence of the split, and vanish if merged:

| Cost | Merged equivalent |
|---|---|
| `207 Multi-Status` on `POST /delivery` | One transaction. Either both writes land or neither does |
| `assigned[]` / `failed[]` on `POST /runsheets` | One transaction. Full success or full rollback |
| N+1 HTTP calls enriching runsheet stops | A single `JOIN` across two schemas |
| `consignment_client.py`, timeouts, `ConsignmentError` | A function call |
| Duplicated `db.py` in both services | One module |

That is a meaningful amount of complexity — including the *hardest* correctness reasoning in the
codebase — bought with a single HTTP hop that crosses no organisational or infrastructural boundary.

### 3.3 What you would lose by merging

Be equally honest in the other direction:

- **The cross-service call is the most interesting thing in this project to talk about.** Real
  timeout handling, real partial-failure reporting, a real `207`. In an interview that is worth more
  than a clean monolith.
- **The GPS scaling profile is genuinely different.** If ingest ever becomes real, you would want to
  scale that path independently.
- **The schema boundary is currently enforced by the network.** Merged, nothing stops a lazy `JOIN`
  across schemas six months from now, and the boundary quietly dies.

### 3.4 Recommendation: modular monolith with an enforced boundary

The production-correct target is **one FastAPI app, two routers, two schema-owning modules, and a
test that fails if the boundary is crossed**:

```
services/api/
├── app/
│   ├── main.py                 # mounts both routers
│   ├── consignment/
│   │   ├── router.py           # /api/v1/waybills, /scans
│   │   ├── domain.py           # ALLOWED_TRANSITIONS lives here, still one place
│   │   └── store.py            # ONLY module allowed to touch schema `consignment`
│   ├── dispatch/
│   │   ├── router.py           # /api/v1/runsheets, /gps, /delivery
│   │   └── store.py            # ONLY module allowed to touch schema `dispatch`
│   └── shared/{db,cache,config}.py
└── tests/
    └── test_boundaries.py      # ← the important one
```

```python
# tests/test_boundaries.py
"""The network no longer enforces the schema boundary, so a test must.

This is what stops the modular monolith decaying into a big ball of mud:
dispatch code may not reference consignment's tables, and vice versa.
"""
import pathlib, re

SRC = pathlib.Path(__file__).parent.parent / "app"

def test_dispatch_never_touches_consignment_schema():
    for f in (SRC / "dispatch").rglob("*.py"):
        text = f.read_text()
        assert not re.search(r"\bconsignment\.\w+", text), (
            f"{f.name} references the consignment schema directly. "
            "Cross-schema reads must go through consignment's service layer."
        )

def test_consignment_never_touches_dispatch_schema():
    for f in (SRC / "consignment").rglob("*.py"):
        assert not re.search(r"\bdispatch\.\w+", f.read_text())
```

You keep the discipline and lose the network. `create_runsheet` becomes one transaction, and the
`207` disappears.

**But:** given your project is a DevOps learning portfolio, I would **keep the split for now** —
and be able to explain, in one paragraph, the test above and why you would merge in production.
Knowing *when not to* use microservices is a stronger signal than having used them.

---

## 4. Refactoring Plan: 10 → 5 containers

No route changes. No schema changes. No backend changes.

```
BEFORE (10)                          AFTER (5)
─────────────────────────            ─────────────────────
gateway          ─┐
merchant-portal   │
hub-app           ├─────────────►    web            (nginx: 5 apps + API proxy)
driver-app        │
customer-portal   │
admin-console    ─┘
consignment-service  ───────────►    consignment-service
dispatch-service     ───────────►    dispatch-service
postgres             ───────────►    postgres
redis                ───────────►    redis
```

### Step 1 — Create `apps/web/`

```
apps/web/
├── Dockerfile
├── nginx.conf
└── index.html          # the launcher (moved from apps/gateway/)
```

The five app folders stay exactly where they are. Only the Dockerfile changes — it now copies all
five into subdirectories of one image.

### Step 2 — `apps/web/Dockerfile`

```dockerfile
# One nginx serving all five apps plus proxying both APIs.
# Build from the REPO ROOT:  docker build -f apps/web/Dockerfile -t fp-web .
FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf

# Launcher at the root.
COPY apps/web/index.html          /usr/share/nginx/html/index.html
COPY packages/web-shared/base.css /usr/share/nginx/html/base.css

# Each app into its own subdirectory, with the shared package copied in
# ALONGSIDE it. That is what keeps the apps' relative paths (./api.js,
# ./base.css) working unchanged -- they resolve inside the subdirectory.
COPY packages/web-shared/         /usr/share/nginx/html/merchant/
COPY apps/merchant-portal/        /usr/share/nginx/html/merchant/
COPY packages/web-shared/         /usr/share/nginx/html/hub/
COPY apps/hub-app/                /usr/share/nginx/html/hub/
COPY packages/web-shared/         /usr/share/nginx/html/driver/
COPY apps/driver-app/             /usr/share/nginx/html/driver/
COPY packages/web-shared/         /usr/share/nginx/html/track/
COPY apps/customer-portal/        /usr/share/nginx/html/track/
COPY packages/web-shared/         /usr/share/nginx/html/admin/
COPY apps/admin-console/          /usr/share/nginx/html/admin/

# Build-context leftovers that must not be served.
RUN find /usr/share/nginx/html -name 'Dockerfile' -o -name 'nginx.conf' | xargs -r rm -f

EXPOSE 80

# 127.0.0.1, NOT localhost: localhost resolves to ::1 in this image while
# `listen 80` binds IPv4 only, so a localhost check always fails.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null 2>&1 || exit 1
```

### Step 3 — `apps/web/nginx.conf`

Every current URL still resolves. Path routing becomes plain static serving — **no proxying between
containers at all**, which is strictly faster and removes a whole class of failure.

```nginx
# FleetPulse web tier: five static apps + API proxy, one container.
#
# Path routing is now direct file serving rather than a proxy hop, because the
# apps live in this image. Only /api/* is still proxied.

# Docker's embedded DNS -- without it nginx resolves the API upstreams once at
# boot and keeps a stale IP after any backend restart.
resolver 127.0.0.11 valid=10s ipv6=off;

# ---- Shared API + health routes, included by every server block -----------
# Keeping these in a snippet is what makes /api/* same-origin from EVERY
# hostname, which is why this project has no CORS configuration anywhere.
# (Save as /etc/nginx/api_locations.conf)

# ---------------------------------------------------------------------------
# Per-app hostnames. Each points `root` at that app's subdirectory, so the app
# sees itself at "/" exactly as it does today.
# ---------------------------------------------------------------------------
server {
    listen 80;
    server_name merchant.fleetpulse.localhost merchant.fleetpulse.local;
    root /usr/share/nginx/html/merchant;
    location / { try_files $uri $uri/ /index.html; }
    include /etc/nginx/api_locations.conf;
}

server {
    listen 80;
    server_name hub.fleetpulse.localhost hub.fleetpulse.local;
    root /usr/share/nginx/html/hub;
    location / { try_files $uri $uri/ /index.html; }
    include /etc/nginx/api_locations.conf;
}

server {
    listen 80;
    server_name driver.fleetpulse.localhost driver.fleetpulse.local;
    root /usr/share/nginx/html/driver;
    location / { try_files $uri $uri/ /index.html; }
    include /etc/nginx/api_locations.conf;
}

server {
    listen 80;
    server_name track.fleetpulse.localhost track.fleetpulse.local;
    root /usr/share/nginx/html/track;
    location / { try_files $uri $uri/ /index.html; }
    include /etc/nginx/api_locations.conf;
}

server {
    listen 80;
    server_name admin.fleetpulse.localhost admin.fleetpulse.local;
    root /usr/share/nginx/html/admin;
    location / { try_files $uri $uri/ /index.html; }
    include /etc/nginx/api_locations.conf;
}

# ---------------------------------------------------------------------------
# Default: localhost and anything else. Launcher + path routing.
# ---------------------------------------------------------------------------
server {
    listen 80 default_server;
    server_name _;
    root /usr/share/nginx/html;

    location = /          { add_header Cache-Control "no-store"; try_files /index.html =404; }
    location = /index.html { add_header Cache-Control "no-store"; }

    # Path routes are now plain file serving. The trailing-slash redirects
    # keep /driver (no slash) working.
    location /merchant/ { try_files $uri $uri/ /merchant/index.html; }
    location /hub/      { try_files $uri $uri/ /hub/index.html; }
    location /driver/   { try_files $uri $uri/ /driver/index.html; }
    location /track/    { try_files $uri $uri/ /track/index.html; }
    location /admin/    { try_files $uri $uri/ /admin/index.html; }

    location = /merchant { return 301 /merchant/; }
    location = /hub      { return 301 /hub/; }
    location = /driver   { return 301 /driver/; }
    location = /track    { return 301 /track/; }
    location = /admin    { return 301 /admin/; }

    include /etc/nginx/api_locations.conf;

    gzip on;
    gzip_types text/css application/javascript application/json text/plain;
    gzip_min_length 512;
}
```

```nginx
# /etc/nginx/api_locations.conf  (apps/web/api_locations.conf)
location /api/consignment/ {
    proxy_pass http://consignment-service:8000/api/;
    include /etc/nginx/app_proxy.conf;
}
location /api/dispatch/ {
    proxy_pass http://dispatch-service:8000/api/;
    include /etc/nginx/app_proxy.conf;
}
location = /health/consignment { proxy_pass http://consignment-service:8000/health; }
location = /health/dispatch    { proxy_pass http://dispatch-service:8000/health; }
location = /healthz {
    access_log off;
    default_type text/plain;
    return 200 "ok\n";
}
```

### Step 4 — `docker-compose.yml`

```yaml
name: fleetpulse

services:
  # ==========================================================
  # WEB TIER -- 5 apps + API proxy in one container (was 6)
  # ==========================================================
  web:
    build:
      context: .                       # repo root: needs packages/ AND apps/
      dockerfile: apps/web/Dockerfile
    container_name: fp-web
    ports:
      - "80:80"                        # the only intentionally public port
    depends_on:
      consignment-service: { condition: service_started }
      dispatch-service:    { condition: service_started }
    restart: unless-stopped

  # ==========================================================
  # BACKEND
  # ==========================================================
  consignment-service:
    build: ./services/consignment-service
    container_name: fp-consignment
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-fleetadmin}:${POSTGRES_PASSWORD:-localdevpassword}@postgres:5432/${POSTGRES_DB:-fleetpulse}
      REDIS_URL: redis://redis:6379/0
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    ports:
      - "127.0.0.1:8001:8000"          # loopback: debug + Swagger only
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    restart: unless-stopped

  dispatch-service:
    build: ./services/dispatch-service
    container_name: fp-dispatch
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-fleetadmin}:${POSTGRES_PASSWORD:-localdevpassword}@postgres:5432/${POSTGRES_DB:-fleetpulse}
      REDIS_URL: redis://redis:6379/0
      CONSIGNMENT_URL: http://consignment-service:8000
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    ports:
      - "127.0.0.1:8002:8000"
    depends_on:
      postgres:            { condition: service_healthy }
      redis:               { condition: service_healthy }
      consignment-service: { condition: service_started }
    restart: unless-stopped

  # ==========================================================
  # DATA -- loopback ONLY. These must never be LAN-reachable.
  # ==========================================================
  postgres:
    image: postgres:16-alpine
    container_name: fp-postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-fleetadmin}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-localdevpassword}
      POSTGRES_DB: ${POSTGRES_DB:-fleetpulse}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-fleetadmin} -d ${POSTGRES_DB:-fleetpulse}"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    container_name: fp-redis
    command: ["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]
    ports:
      - "127.0.0.1:6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10
    restart: unless-stopped

  # ==========================================================
  # SIMULATOR -- profile, does not start with `up`
  # ==========================================================
  simulator:
    profiles: ["sim"]
    build: ./simulator
    environment:
      CONSIGNMENT_URL: http://consignment-service:8000
      DISPATCH_URL: http://dispatch-service:8000

volumes:
  pgdata:
```

### Step 5 — Migration order

Do it in this sequence so you can stop at any point with a working system:

1. **Bind ports to `127.0.0.1`** in the current compose file. Ship this alone, today. One line per
   service, zero risk, closes the live exposure.
2. Create `apps/web/` with the Dockerfile and nginx config above. **Do not delete anything yet.**
3. Add `web` to Compose on port **8080** alongside the existing gateway on 80.
4. Verify all 11 URLs against `:8080` — 6 paths, 5 hostnames via `Host:` header.
5. Swap `web` to port 80, remove `gateway` and the five app services.
6. Delete `apps/gateway/`. Keep the five app source folders — they are still the sources.

### Step 6 — What you gain

| | Before | After |
|---|---|---|
| Containers | 10 | **5** |
| Images to build on a `web-shared` change | 5 | **1** |
| Dockerfiles | 8 | **3** |
| nginx configs | 7 | **1** (+2 snippets) |
| Proxy hops for a static asset | 2 (gateway → app) | **1** |
| LAN-exposed ports | 9 | **1** |

---

## 5. Kubernetes Translation

### 5.1 What each Compose object becomes

| Compose | Kubernetes | Notes |
|---|---|---|
| `web` | Deployment + Service + **Ingress** | The nginx *config* becomes the Ingress; the container keeps serving static files |
| `consignment-service` | Deployment + Service (ClusterIP) | |
| `dispatch-service` | Deployment + Service (ClusterIP) | |
| `postgres` | **StatefulSet** + headless Service + PVC | Or drop it entirely for managed RDS |
| `redis` | Deployment + Service | It is a cache with a 1h TTL — no StatefulSet needed |
| `db/init.sql` | **Job** (or `initContainer`) | Idempotent, so safe to re-run |
| `ports:` | Service + Ingress | You almost never publish a nodePort |
| `environment:` | ConfigMap + Secret | Split by sensitivity |
| `depends_on: healthy` | **Nothing** | Pods crash-loop until dependencies are ready. That is the design |
| `healthcheck:` | `livenessProbe` **and** `readinessProbe` | One becomes two, and the difference matters |

### 5.2 The routing layer collapses

This is the most important translation. **Your reverse proxy stops being a container you run.** The
Ingress controller is the reverse proxy; your nginx config becomes an Ingress *resource*:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: fleetpulse
  namespace: fleetpulse
  annotations:
    nginx.ingress.kubernetes.io/use-regex: "true"
spec:
  ingressClassName: nginx
  rules:
    # ---- Host-based: one rule per app hostname ----
    - host: driver.fleetpulse.localhost
      http:
        paths:
          - { path: /, pathType: Prefix, backend: { service: { name: web, port: { number: 80 } } } }
    - host: admin.fleetpulse.localhost
      http:
        paths:
          - { path: /, pathType: Prefix, backend: { service: { name: web, port: { number: 80 } } } }

    # ---- Path-based, plus the API split ----
    - host: fleetpulse.localhost
      http:
        paths:
          # Order matters: the API prefixes must precede the catch-all.
          - path: /api/consignment(/|$)(.*)
            pathType: ImplementationSpecific
            backend: { service: { name: consignment-service, port: { number: 8000 } } }
          - path: /api/dispatch(/|$)(.*)
            pathType: ImplementationSpecific
            backend: { service: { name: dispatch-service, port: { number: 8000 } } }
          - path: /
            pathType: Prefix
            backend: { service: { name: web, port: { number: 80 } } }
```

Two things to notice:

- **The `/api/*` routes now bypass `web` entirely.** In Compose, nginx proxied them; in Kubernetes,
  the Ingress controller routes straight to the backend Services. One fewer hop.
- The `web` Service still fronts a container serving static files, but the nginx `server_name`
  blocks are now redundant — the Ingress does host matching. You can simplify `web`'s config down to
  plain static serving.

### 5.3 Pod grouping: do **not** use sidecars here

A frequent mistake when moving from Compose is to put related containers in one Pod. Containers in a
Pod share a network namespace and lifecycle — appropriate for a log shipper, a service-mesh proxy,
or a config reloader. **Nothing in FleetPulse qualifies.**

| Tempting | Verdict |
|---|---|
| nginx + FastAPI in one Pod | ❌ They scale differently and Ingress already fronts them |
| consignment + dispatch in one Pod | ❌ If you want them co-located, merge the code (§3), don't co-locate processes |
| Postgres + a backup sidecar | ✅ Legitimate — shared volume, shared lifecycle |

**One process class per Pod.** Five Deployments, five Services, one Ingress.

### 5.4 Recommended object set

```
namespace/fleetpulse
├── ConfigMap    fleetpulse-config      LOG_LEVEL, REDIS_URL, CONSIGNMENT_URL
├── Secret       fleetpulse-secrets     DATABASE_URL
├── Deployment   web                    2 replicas (static, cheap)
│   └── Service  web            ClusterIP :80
├── Deployment   consignment-service    2 replicas + HPA
│   └── Service  consignment-service    ClusterIP :8000
├── Deployment   dispatch-service       2 replicas
│   └── Service  dispatch-service       ClusterIP :8000
├── Deployment   redis                  1 replica
│   └── Service  redis          ClusterIP :6379
├── StatefulSet  postgres               1 replica + PVC 5Gi
│   └── Service  postgres       headless
├── Job          db-migrate             runs db/init.sql, PreSync
└── Ingress      fleetpulse             host + path rules
```

### 5.5 Three things that will surprise you

**Connection-pool arithmetic.** `max_size=5` per replica × 2 services × N replicas must stay under
Postgres's limit. Two services at 3 replicas each is 30 connections — fine. Add an HPA with
`maxReplicas: 10` and you are at 100, which exhausts a small Postgres. **Autoscaling can cause an
outage by succeeding.** Cap the pool or add PgBouncer before you enable the HPA.

**`depends_on` is gone and that is correct.** Your services will start before Postgres is ready and
crash. Kubernetes restarts them. The 30-second connect timeout and clear `RuntimeError` you already
have in `db.py` are exactly the right behaviour — no `initContainer` needed.

**Readiness must not check Postgres; liveness definitely must not.** Your `/health` returns a static
200 and deliberately does not touch the database. Keep it that way for **liveness** — a health check
that hits the DB turns a brief blip into every pod restarting at once. If you want a
dependency-aware check, add a *separate* `/readyz` and wire it only to `readinessProbe`.

---

## 6. Priority order

| # | Action | Effort | Value |
|---|---|---|---|
| 1 | **Bind all ports to `127.0.0.1`** | 10 min | 🔴 Closes live LAN exposure of Postgres, Redis, admin |
| 2 | Set a real `POSTGRES_PASSWORD` in `.env` | 2 min | 🔴 Default credential |
| 3 | Consolidate 6 web containers → 1 | 2–3 h | 🟠 10 → 5 containers, halves the config surface |
| 4 | Keep the backends split, document the merge test | 30 min | 🟡 Judgement is the deliverable, not the merge |
| 5 | Kubernetes translation (§5) | learning track | 🟡 |
| 6 | Add auth — **backend first** | later | 🟠 Everything above assumes localhost-only |

Do 1 and 2 today. They are ten minutes combined and they are the only items on this list that
matter if someone else is on your network.
