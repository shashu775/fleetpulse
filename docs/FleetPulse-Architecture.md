# How FleetPulse Works

The architecture and runtime behaviour of the whole system: five front-end applications served by a
single nginx container, two backend microservices, PostgreSQL and Redis — **five containers**, one
command.

Companion documents: [FleetPulse-Apps.md](FleetPulse-Apps.md) (front-end structure and the full API
surface) and [FleetPulse-Simple.md](FleetPulse-Simple.md) (the build plan).

---

## 1. What the system does

FleetPulse models the operational spine of a parcel logistics network — the part of Delhivery that
moves a package from a merchant's warehouse to a customer's door.

> A merchant books a shipment. FleetPulse issues an **AWB** (airway bill number) and a printable
> label. The parcel is scanned into the origin facility, travels between sorting hubs on line-haul
> vehicles, and arrives at the facility nearest the customer. There it is assigned to a driver's
> **runsheet**. The driver's vehicle reports GPS while out. Finally the parcel is **delivered**
> (proof captured) or, after failed attempts, sent **RTO** — return to origin.

Five kinds of user touch a parcel, and each gets their own interface. The system's job is to know
where every parcel is, who is responsible for it right now, and what happened to it.

---

## 2. The whole system

```mermaid
flowchart TB
    subgraph USERS[" "]
        M["🏪 Merchant"]
        H["🏭 Hub operator"]
        D["🚚 Driver"]
        C["📦 Customer"]
        O["🎛️ Operations"]
    end

    subgraph GWB["<b>web</b> :80 — one nginx container"]
        GW["host + path routing · API proxy<br/>one origin, so no CORS anywhere"]
        subgraph APPS["Static apps served from this image (no build step)"]
            MP["/merchant/"]
            HA["/hub/"]
            DA["/driver/"]
            CP["/track/"]
            AC["/admin/"]
        end
    end

    M --> GW
    H --> GW
    D --> GW
    C --> GW
    O --> GW

    GW --> MP & HA & DA & CP & AC

    subgraph SVC["Backend microservices (FastAPI)"]
        CS["<b>consignment-service</b> :8000<br/>parcels · scans · labels<br/><b>THE STATE MACHINE</b>"]
        DS["<b>dispatch-service</b> :8000<br/>runsheets · GPS · POD"]
    end

    GW -->|"/api/consignment/*"| CS
    GW -->|"/api/dispatch/*"| DS
    DS -->|"REST — the only<br/>inter-service call"| CS

    PG[("PostgreSQL<br/>schemas: consignment, dispatch")]
    RD[("Redis<br/>tracking cache + live GPS")]

    CS --> PG & RD
    DS --> PG & RD
```

**Five containers.** Every app is reachable two ways — its own hostname
(`driver.fleetpulse.localhost`) or a path under localhost (`/driver/`), both always live. See
[FleetPulse-Apps.md §1](FleetPulse-Apps.md).

> This was ten containers until recently: a gateway plus one nginx per app. They were consolidated
> because static assets have no independent scaling profile, no state and no independent lifecycle
> — and since every app embeds `packages/web-shared`, a change there rebuilt all five images
> anyway. The per-app ports 3001–3005 no longer exist. See
> [FleetPulse-Architecture-Review.md](FleetPulse-Architecture-Review.md).

### 2.1 Component responsibilities

| Component | Owns | Notes |
|---|---|---|
| **web** | Nothing | nginx. Serves all five apps as static files, proxies `/api/*`, hosts the launcher |
| ↳ `/merchant/` | — | Booking. Bulk upload and labels not built |
| ↳ `/hub/` | — | High-speed inbound/outbound scanning |
| ↳ `/driver/` | — | Runsheets, out-for-delivery scan, POD, GPS |
| ↳ `/track/` | — | Public tracking; the AWB is the only credential |
| ↳ `/admin/` | — | Network-wide ops. **The only place a runsheet can be created** |
| **consignment-service** | Waybills, scan events, parcel status | **System of record.** Owns `ALLOWED_TRANSITIONS` |
| **dispatch-service** | Runsheets, stops, delivery attempts, POD | Drives the last three transitions, performs none of them |
| **PostgreSQL** | Durable truth | One database, two schemas |
| **Redis** | Tracking cache + last-known GPS | Two roles with opposite failure semantics (§6.2) |

The web tier holds **no state and no database access**. It is static HTML, CSS and ES modules,
calling HTTP APIs. That is why adding a sixth app, or rewriting one in React, changes nothing
behind it.

**Every port except `80` binds `127.0.0.1`.** Before that was fixed, Postgres, Redis (no password)
and the admin console were reachable from the whole LAN.

### 2.2 Three rules that define the design

**1. Dispatch never writes to the `consignment` schema.** Same database, same credentials, trivially
possible — and forbidden. Consignment enforces legal state transitions; if dispatch wrote directly,
that rule would live in two places and eventually disagree. All cross-service traffic goes through
`services/dispatch-service/app/consignment_client.py`.

**2. Dependencies point one way.** Dispatch → consignment, never the reverse. Circular service
dependencies create startup-ordering problems and cascading timeouts.

**3. Front-ends never talk to a database.** They call HTTP APIs through the web tier. Obvious, but it
is why swapping any app for React, or adding a sixth, changes nothing behind it. The admin
console proved this: it was built entirely from endpoints that already existed.

---

## 3. Data model

```mermaid
erDiagram
    WAYBILLS ||--o{ SCAN_EVENTS : "append-only history"
    RUNSHEETS ||--o{ RUNSHEET_ITEMS : "stops"
    RUNSHEETS ||--o{ DELIVERY_ATTEMPTS : "outcomes"
    WAYBILLS ||..o{ RUNSHEET_ITEMS : "by AWB (no FK)"

    WAYBILLS {
        varchar awb PK
        varchar merchant_name
        varchar consignee_name
        varchar consignee_phone
        varchar consignee_addr
        varchar origin_hub
        varchar destination_hub
        varchar current_status
        varchar payment_mode
        numeric cod_amount
    }
    SCAN_EVENTS {
        bigserial id PK
        varchar awb FK
        varchar status
        varchar hub_id
        timestamptz scanned_at
    }
    RUNSHEETS {
        varchar id PK
        varchar driver_id
        varchar vehicle_id
        varchar hub_id
    }
    RUNSHEET_ITEMS {
        varchar runsheet_id PK
        varchar awb PK
        integer sequence
        varchar status
    }
    DELIVERY_ATTEMPTS {
        bigserial id PK
        varchar awb
        varchar runsheet_id FK
        varchar outcome
        varchar pod_type
        varchar pod_receiver
        text pod_data
    }
```

Four choices worth understanding:

**`scan_events` is append-only.** Never updated or deleted. `waybills.current_status` is a
denormalised convenience column that could be rebuilt from it at any time. This is why the tracking
history a customer sees is trustworthy.

**`runsheet_items` exists because the driver app needed it.** Before it, `runsheets` recorded *who*
was driving and `delivery_attempts` recorded what had *already* been attempted — nothing recorded
what was **on** the runsheet. "What am I delivering today?" was unanswerable.

**No FK crosses a schema boundary.** `runsheet_items.awb` and `delivery_attempts.awb` point at
`consignment.waybills` by value. A real FK would couple the schemas at the database level and make a
future service split impossible. AWBs are validated over HTTP instead.

**There is no `gps_pings` table.** Deliberately — see §4.7.

---

## 4. Request flows

### 4.1 Every request starts at the web tier

```mermaid
sequenceDiagram
    participant B as Browser
    participant G as web (nginx)
    participant A as static files
    participant S as service

    alt By path (works with no setup)
        B->>G: GET /driver/ <br/>Host: localhost
        Note over G: default server, /driver/ location
    else By hostname (name must resolve first)
        B->>G: GET / <br/>Host: driver.fleetpulse.localhost
        Note over G: matches the driver vhost server block
    end

    G->>A: GET /            (prefix stripped either way)
    A-->>B: index.html
    B->>G: GET ./app.js
    Note over B,A: Apps use RELATIVE asset paths, so the same file<br/>works behind /driver/ and on its own hostname.

    B->>G: GET /api/dispatch/v1/runsheets?driver_id=DRV-4417
    G->>S: GET /api/v1/runsheets?driver_id=DRV-4417
    S-->>B: 200 JSON
```

Every `server` block in `apps/web/nginx.conf` includes the same `api_locations.conf`, so `/api/*` resolves
same-origin **from whichever hostname you used**. That is what keeps **CORS out of this project
entirely** — the most common source of frontend/backend friction, designed out rather than
configured around.

Absolute asset paths would break this: `/app.js` requested from `localhost/driver/` escapes the
prefix and 404s. Every app therefore uses `./app.js` and `./base.css`.

### 4.2 Booking — Merchant Portal

```mermaid
sequenceDiagram
    participant M as Merchant Portal
    participant C as consignment
    participant P as Postgres

    M->>C: POST /waybills {merchant, consignee, hubs, weight, COD}
    Note over C: Pydantic validates. Bad input → 422<br/>before any handler code runs.
    C->>C: generate_awb() → "FP4820193756"
    rect rgb(232, 240, 254)
        Note over C,P: ONE transaction
        C->>P: INSERT waybills (status = MANIFESTED)
        C->>P: INSERT scan_events ("Shipment booked")
    end
    C-->>M: 201 {awb, tracking_url, label_url}
```

Both inserts share a transaction, so a parcel can never exist without at least one history row.

### 4.3 Hub scan — where the rules live

```mermaid
sequenceDiagram
    participant H as Hub Scanner
    participant C as consignment
    participant P as Postgres
    participant R as Redis

    H->>C: POST /scans {awb, ARRIVED_AT_FACILITY, hub_id}
    C->>P: SELECT current_status ... FOR UPDATE
    P-->>C: "IN_TRANSIT"

    alt Illegal transition
        C-->>H: 409 "Cannot move from X to Y"
    else Legal
        C->>P: UPDATE waybills SET current_status
        C->>P: INSERT scan_events
        C->>R: DEL awb:FP4820193756
        C-->>H: 201 {previous_status, new_status}
    end
```

`SELECT ... FOR UPDATE` holds the row for the transaction, so two concurrent scans of the same
parcel cannot both read the old status and both conclude their move is legal.

**Cache invalidation is `DEL`, never overwrite.** If you wrote the new value and the transaction
later rolled back, the cache would hold data that was never committed. Deleting is always safe.

### 4.4 Tracking — Customer Portal

```mermaid
sequenceDiagram
    participant U as Customer Portal
    participant C as consignment
    participant R as Redis
    participant P as Postgres

    U->>C: GET /waybills/FP4820193756
    alt Cache HIT (typical)
        C->>R: GET awb:...
        R-->>C: {...}
        C-->>U: 200 {..., "_cache": "HIT"}     ~2 ms
    else Cache MISS
        C->>R: (nil)
        C->>P: SELECT * FROM waybills
        C->>R: SETEX awb:... 300
        C-->>U: 200 {..., "_cache": "MISS"}    ~20 ms
    end
```

The most-called endpoint in any logistics system — customers refresh tracking pages obsessively.
The portal fetches the parcel and its history in parallel.

### 4.5 Runsheet creation — Admin Console

The hand-off from the middle mile to the last mile, and the only write in the system that touches
both services' data in one user action.

```mermaid
sequenceDiagram
    participant A as Admin Console
    participant CS as consignment
    participant DS as dispatch
    participant PD as Postgres

    Note over A,CS: Populate the picker
    A->>CS: GET /waybills?status=ARRIVED_AT_FACILITY
    CS-->>A: parcels (filtered client-side by destination hub)
    Note over A: Only this status can legally go OUT_FOR_DELIVERY,<br/>so the UI cannot build a request the state machine rejects.

    Note over A,DS: Create
    A->>DS: POST /runsheets {driver, vehicle, hub, awbs[]}

    loop STEP 1 — validate every AWB first
        DS->>CS: GET /waybills/{awb}
    end
    Note over DS: One bad AWB rejects the WHOLE request.<br/>Nothing written yet.

    DS->>PD: INSERT runsheets
    DS->>PD: INSERT runsheet_items (sequence 1..N, PENDING)

    loop STEP 3 — move each parcel
        DS->>CS: PATCH /waybills/{awb}/status {OUT_FOR_DELIVERY}
    end

    DS-->>A: 201 {assigned[], failed[]}
```

**Validate-then-write** means a single bad AWB rejects the request rather than half-creating a
runsheet. Step 3 can still partially fail — no transaction spans two services over HTTP — so the
response carries both lists and the console renders the failures inline rather than hiding them.

`runsheet_items` is what makes the driver app possible; before it existed, "what am I delivering
today?" had no answer (§3).

### 4.6 Driver workflow — the cross-service call

```mermaid
sequenceDiagram
    participant D as Driver App
    participant DS as dispatch
    participant CS as consignment
    participant PD as Postgres

    Note over D,CS: Open a runsheet
    D->>DS: GET /runsheets/{id}
    DS->>PD: SELECT runsheet_items ORDER BY sequence
    loop per stop
        DS->>CS: GET /waybills/{awb}
        Note over DS,CS: Enrich with consignee name, address, phone, COD.<br/>Dispatch does not own that data, so it asks.
        CS-->>DS: 200 (or error → stop still renders with AWB)
    end
    DS-->>D: 200 {stops: [...]}

    Note over D,CS: Scan out for delivery
    D->>CS: PATCH /waybills/{awb}/status {OUT_FOR_DELIVERY}
    CS-->>D: 200

    Note over D,CS: Capture proof of delivery
    D->>DS: POST /delivery {outcome, pod_type, pod_data, pod_receiver}
    DS->>PD: INSERT delivery_attempts (with POD)
    DS->>PD: UPDATE runsheet_items SET status
    DS->>CS: PATCH /waybills/{awb}/status {DELIVERED}
    alt consignment reachable
        CS-->>DS: 200
        DS-->>D: 201
    else consignment down
        DS-->>D: 207 Multi-Status
        Note over D: The driver's work IS saved.<br/>App shows "will reconcile shortly", not an error.
    end
```

**The enrichment loop is N+1 by design.** Runsheets cap at 50 stops, and the alternative — dispatch
reading `consignment.waybills` directly — breaks rule 1 in §2.2. If it ever became hot, the fix is a
batch endpoint on consignment, not a cross-schema `SELECT`.

**The 207 is deliberate.** A `500` would imply nothing happened; a `201` would claim everything
worked. Neither is true. This is the real cost of synchronous REST without a broker, and the
transactional outbox in [the notification add-on](FleetPulse-Addon-Notification.md) is how you close
it.

### 4.7 GPS — the path that avoids the database

```mermaid
sequenceDiagram
    participant D as Driver App
    participant DS as dispatch
    participant R as Redis
    participant P as Postgres

    loop every 15 s while a runsheet is open
        D->>DS: POST /gps {vehicle_id, lat, lon, speed}
        DS->>R: SETEX vehicle:{id}:location 3600
        DS-->>D: 202 Accepted
    end
    Note over P: Postgres is never written to.
```

100 vehicles pinging every 10 seconds is ~864,000 writes/day of data whose value expires in seconds.
Redis holds one key per vehicle with a 1-hour TTL, so a vehicle that stops reporting expires on its
own with no cleanup job.

**`202 Accepted`, not `201 Created`** — the API is being honest that the write is non-durable.

---

## 5. Parcel lifecycle

```mermaid
stateDiagram-v2
    [*] --> MANIFESTED: Merchant Portal
    MANIFESTED --> IN_TRANSIT: Hub Scanner (outbound)
    IN_TRANSIT --> ARRIVED_AT_FACILITY: Hub Scanner (inbound)
    ARRIVED_AT_FACILITY --> IN_TRANSIT: Hub Scanner (onward hub)
    ARRIVED_AT_FACILITY --> OUT_FOR_DELIVERY: Admin Console / Driver App
    OUT_FOR_DELIVERY --> DELIVERED: Driver App (POD)
    OUT_FOR_DELIVERY --> RTO: Driver App (NDR)
    DELIVERED --> [*]
    RTO --> [*]
```

| Transition | Triggered from | Endpoint |
|---|---|---|
| → `MANIFESTED` | Merchant Portal | `POST /waybills` |
| → `IN_TRANSIT` | Hub Scanner | `POST /scans` |
| → `ARRIVED_AT_FACILITY` | Hub Scanner | `POST /scans` |
| → `OUT_FOR_DELIVERY` | **Admin Console** (bulk, on runsheet creation) or **Driver App** (per stop) | `POST /runsheets` or `PATCH /waybills/{awb}/status` |
| → `DELIVERED` / `RTO` | Driver App | `POST /delivery` → `PATCH …/status` |

Two apps can drive `OUT_FOR_DELIVERY`, and both go through the same `PATCH` on consignment —
the admin console indirectly, via `POST /runsheets`. Neither writes parcel status itself.

Every arrow is validated against `ALLOWED_TRANSITIONS` in consignment. The driver app additionally
*hides* illegal actions — "Complete delivery" is disabled until the parcel is `OUT_FOR_DELIVERY` —
but the backend enforces it regardless. UI restrictions are a courtesy; the 409 is the guarantee.

---

## 6. When things break

### 6.1 Failure matrix

| Failure | Effect | Why |
|---|---|---|
| **web container down** | Everything unreachable on :80. Backends still up on 127.0.0.1:8001/8002 | Single entry point is a single point of failure |

| **Redis down** | Tracking works, slower. GPS endpoints 503 | Two roles, two policies — §6.2 |
| **consignment down** | Booking, tracking, scans fail. Admin console's parcel list and picker fail. Driver can still record POD → 207 | It is the system of record |
| **dispatch down** | Merchant, hub and customer apps unaffected. Driver app and admin runsheet tab dead | Nothing depends on dispatch |
| **Postgres down** | All writes fail; cached tracking reads still succeed | Correct: reject rather than accept unpersistable writes |
| **consignment slow** | Runsheet detail slows, bounded at 5 s per call | `httpx.Timeout(5.0, connect=2.0)` |
| **Partial runsheet assign** | Some parcels assigned; `failed[]` lists the rest, rendered inline by the console | No transaction spans two services |

**consignment-service is the single point of failure**, and that is a property of the design, not an
oversight. Making the system of record optional would mean giving up the guarantee that parcel
status has exactly one authority.

### 6.2 The two Redis roles

| | consignment `cache.py` | dispatch `cache.py` |
|---|---|---|
| Role | **Cache** — Postgres is the truth | **Store** — nothing else holds GPS |
| On failure | Logs, returns `None`, behaves as a miss | Propagates; endpoints return **503** |
| Rationale | A cache that can down the service is worse than no cache | Silently discarding writes is worse than an error |

Do not harmonise these. Knowing which kind of Redis you have is the whole distinction.

```bash
docker compose stop redis
curl -s localhost/api/consignment/v1/waybills/$AWB        # 200 — still works
curl -s -o /dev/null -w '%{http_code}\n' \
     localhost/api/dispatch/v1/vehicles/KA01AB1234/location   # 503 — honest
docker compose start redis
```

---

## 7. Cross-cutting concerns

**Service discovery.** `http://consignment-service:8000` is the same URL in Docker Compose and in
Kubernetes — both provide DNS by service name. That is why no application code changes when moving
to a cluster.

**Configuration.** `.env.example` is the contract; `docker-compose.yml` carries defaults so the
stack starts without a `.env`. Missing config fails loudly with a message naming the variable, not
a bare `KeyError`.

**No authentication.** The driver "login" is a picker in `localStorage`; anyone can be any driver,
hub, or merchant. The customer portal is genuinely public — the AWB *is* the credential, matching
every real courier. See [FleetPulse-Apps.md §5](FleetPulse-Apps.md) for what real auth would take;
the short version is **backend first**, because auth added only in the frontend is decoration when
anyone can `curl` the API.

> ⚠️ The **admin console raises the stakes here.** It can create runsheets and read every parcel in
> the network. It must not be reachable beyond localhost until real auth exists — and when it is
> added, this is the app that needs a role check, not just a login.

**No build step.** Apps are HTML + CSS + ES modules served by nginx. `packages/web-shared` is copied
into each image at build time, so no app depends on another being up. The cost is that changing the
shared package requires rebuilding all six images.

**Container healthchecks bind IPv4.** In `nginx:alpine`, `localhost` resolves to `::1` first while
`listen 80` binds IPv4 only — so `wget http://localhost/healthz` returns *connection refused* from a
container that is serving perfectly. All six app images use `127.0.0.1` for this reason. A
healthcheck that always fails is worse than none: it makes `docker compose ps` lie.

---

## 8. Deployment topology

The application code never changes between environments — only configuration.

| | Local Compose | minikube | AWS EC2 | EKS |
|---|---|---|---|---|
| Postgres | container | container | **RDS** | RDS or in-cluster |
| Redis | container | pod | container | pod |
| Images | built locally | loaded into minikube | **ECR** | ECR |
| Routing | web nginx | **Ingress** | web nginx | **ALB Ingress** |
| App hostnames | hosts file | `/etc/hosts` + `minikube ip` | hosts file or DNS | **Route 53** |
| Service discovery | Compose DNS | K8s DNS | Compose DNS | K8s DNS |
| Config | `.env` | ConfigMap + Secret | `.env` | ConfigMap + Secret |

The web tier's nginx is deliberately Ingress-shaped — **host- and path-based routing to several backends from
one entry point** — which is exactly the Ingress resource model. Each `server { server_name … }`
block becomes an Ingress `rule.host`; each `location /x/` becomes a `path`. Moving to Kubernetes is
a translation of `apps/web/nginx.conf`, not a redesign. See
[FleetPulse-Kubernetes.md](FleetPulse-Kubernetes.md).

---

## 9. Deliberate omissions

| Missing | Why it is fine here | When to add it |
|---|---|---|
| Authentication | No real data; nothing is publicly exposed | Before anything is reachable from the internet |
| Message broker | Two services, one call between them | When a third consumer needs the same events |
| Database per service | Separate *schemas* already mark the boundary | When teams own services independently |
| Retries / circuit breakers | Failures are visible and reported honestly | When cross-service calls become frequent |
| Distributed tracing | Two services, one hop — logs suffice | [Observability add-on](FleetPulse-Addon-Observability.md) |
| Bulk operations | Single booking proves the model | Merchant portal's next milestone |
| Rate limiting | No public exposure | Before public exposure |

Being able to say *"I left that out on purpose, and here is when I would add it"* is stronger than
having built everything.

---

## 10. Extending it

**[→ FleetPulse-Apps.md](FleetPulse-Apps.md)** — front-end structure, the complete API surface
(built and specified), and the authentication design.

**[→ Observability Add-On](FleetPulse-Addon-Observability.md)** — structured logging, domain
metrics, Prometheus/Grafana, alerts, tracing. Worth more now that there are nine containers.

**[→ Notification Add-On](FleetPulse-Addon-Notification.md)** — a third service using a
transactional outbox, which also removes the 207 from §4.5.

**[→ Kubernetes](FleetPulse-Kubernetes.md)** — minikube and EKS.

Recommended order: **observability → notifications → Kubernetes.** Add observability first so that
when something misbehaves, you can already see it.
