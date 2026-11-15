# FleetPulse App Ecosystem

Five front-end applications over two shared backend microservices, mirroring the Delhivery
multi-app model: merchants book, hubs sort, drivers deliver, customers track.

Companion to [FleetPulse-Architecture.md](FleetPulse-Architecture.md) (how the backend works) and
[FleetPulse-Simple.md](FleetPulse-Simple.md) (the build plan).

---

## 0. Tech stack decision — and why not React

You offered React / Next.js / HTML+Tailwind. **I chose plain HTML + CSS + ES modules.**

| | Vanilla (chosen) | 4 × Next.js |
|---|---|---|
| Disk | ~60 KB of source | ~1.5 GB of `node_modules` |
| Cold `docker compose up --build` | ~40 s | 5–10 min |
| Edit → see change | instant (hard refresh) | rebuild per app |
| Toolchains to maintain | 0 | 4 |
| Image size per app | ~12 MB (nginx:alpine) | ~180 MB+ |

This project's purpose is **DevOps practice**, not frontend framework practice. Four Node
toolchains would add four things that break, slow every rebuild, and teach nothing about
containers, networking, or deployment. The apps use ES modules and `<script type="module">`, so
there is genuinely no build step.

**When to revisit:** if you want React on your CV, port *one* app — the merchant portal, which has
the most complex state. Keep the other three vanilla. That gives you the React talking point
without paying the cost four times.

The trade-off is honest: no component reuse across apps beyond shared JS modules, no type safety,
and manual DOM updates. At this scale that is cheap.

---

## 1. The five apps

| App | Users | Path *(works now)* | Hostname *(needs §1.2)* | Status |
|---|---|---|---|---|
| **Launcher** | — | `/` | `fleetpulse.localhost` | App switcher |
| **Merchant Portal** | Sellers, brands | `/merchant/` | `merchant.fleetpulse.localhost` | Booking works; bulk/labels/pickups pending |
| **Hub Scanner** | Facility operators | `/hub/` | `hub.fleetpulse.localhost` | Scanning works; bags/manifests pending |
| **Driver App** | Field executives | `/driver/` | `driver.fleetpulse.localhost` | **Complete** |
| **Customer Portal** | Recipients | `/track/` | `track.fleetpulse.localhost` | **Complete** |
| **Admin Console** | Operations | `/admin/` | `admin.fleetpulse.localhost` | **Complete** |

> **All five apps now ship in one container**, `web`. They were previously five separate nginx
> containers behind a sixth gateway container — **the per-app ports 3001–3005 no longer exist.**
> See [FleetPulse-Architecture-Review.md](FleetPulse-Architecture-Review.md) §2 for the reasoning.

### Two ways to reach every app

1. **Path under localhost** — `http://localhost/driver/` · **zero setup, always works. Start here.**
2. **Own hostname** — `http://driver.fleetpulse.localhost/` · production-shaped, but the name must
   resolve first (§1.2)

> ### ⚠️ `.localhost`, not `.local`
> An earlier version of this document used `.local`. **That was a bad choice.** RFC 6762 reserves
> `.local` for mDNS/Bonjour, so on Windows and macOS those lookups can be routed to multicast DNS
> and bypass or race the hosts file — the names then fail intermittently even when configured
> correctly.
>
> RFC 6761 reserves `.localhost` for loopback, and Chrome, Edge and Firefox resolve `*.localhost`
> **internally, without consulting the OS resolver** — so in a browser these often work with no
> setup at all. Command-line tools like `curl` still go through the OS and need §1.2.
>
> `apps/web/nginx.conf` accepts **both** spellings, so old links keep working.

```
   merchant.fleetpulse.localhost ─┐
   hub.fleetpulse.localhost ──────┤        ┌──────────────────────────────┐
   driver.fleetpulse.localhost ───┼───────►│  web (nginx) :80             │
   track.fleetpulse.localhost ────┤        │  host- AND path-based        │
   admin.fleetpulse.localhost ────┤        │                              │
   localhost/<app>/ ──────────────┘        │  /merchant/ /hub/ /driver/   │
                                           │  /track/ /admin/  ← STATIC,  │
                                           │     served from this image   │
                                           └──────┬────────────┬──────────┘
                                    /api/consignment/*   /api/dispatch/*
                                                  ▼            ▼
                                    consignment-service   dispatch-service
```

Only `/api/*` is proxied. The five apps are files inside the `web` image, so a path route is a
direct file lookup rather than a hop to another container.

### 1.1 Why one web container rather than five

Every server block in `apps/web/nginx.conf` includes `api_locations.conf`, so `/api/*` is same-origin from
*whichever* hostname you use. That means **no CORS anywhere in this project** — the single most
common source of frontend/backend friction, designed out rather than configured around.

It is also deliberately the same shape as a Kubernetes **Ingress**: host- and path-based routing to
several backends from one entry point. Moving to k8s later is a translation of
`apps/web/nginx.conf`, not a redesign.

### 1.2 Enabling the hostnames

**Try the browser first.** Chrome, Edge and Firefox resolve `*.localhost` themselves, so
`http://driver.fleetpulse.localhost/` may already work with nothing configured.

If it does not — or you want `curl` to work too — add the names to the hosts file. Run once in an
**Administrator** PowerShell:

```powershell
Add-Content -Path $env:WINDIR\System32\drivers\etc\hosts -Value @"
127.0.0.1 fleetpulse.localhost
127.0.0.1 merchant.fleetpulse.localhost
127.0.0.1 hub.fleetpulse.localhost
127.0.0.1 driver.fleetpulse.localhost
127.0.0.1 track.fleetpulse.localhost
127.0.0.1 admin.fleetpulse.localhost
"@
```

Verify:

```powershell
Resolve-DnsName driver.fleetpulse.localhost      # -> 127.0.0.1
curl.exe -s -o NUL -w "%{http_code}`n" http://driver.fleetpulse.localhost/
```

The Admin console's **Apps** tab shows this command with a copy button. Skipping it costs nothing —
the path URLs keep working regardless.

### 1.3 The Admin Console

The operations view, and the answer to a gap the other apps left: **creating a runsheet** previously
required the simulator or a raw `curl`, which was the one break in an otherwise clickable lifecycle.

| Tab | What it does |
|---|---|
| **Overview** | Network stats, a pipeline bar chart by status, and a "needs attention" panel |
| **Parcels** | Every parcel, searchable by AWB/merchant/consignee, filterable by status |
| **Runsheets** | **Create one**: pick a facility → pick ready parcels → pick or add a driver. Plus a list of all runsheets with live stop counts |
| **Fleet** | Live vehicle positions with map links |
| **Apps** | URL registry for all five apps, and the hosts-file command |

Two details worth noting. The parcel picker only offers parcels at `ARRIVED_AT_FACILITY` — the only
state from which `OUT_FOR_DELIVERY` is legal — so the UI cannot construct a request the state
machine would reject with a 409. And the pipeline bars scale to the largest bucket rather than the
total: with 90% delivered, scaling to the total would make every other bar a sliver.

**It introduced no new endpoints.** Everything it does was already served by the two services, which
is a good sign the API surface was right.

---

## 2. Monorepo layout

```
fleetpulse/
├── docker-compose.yml              5 containers, one command
│
├── apps/                           ── FRONTEND ──────────────────────
│   ├── web/                        THE ONLY front-end container (:80)
│   │   ├── index.html              launcher
│   │   ├── nginx.conf              host + path routing, static serving
│   │   ├── api_locations.conf      /api/* — included by EVERY server block
│   │   ├── app_proxy.conf          shared proxy headers
│   │   └── Dockerfile              bundles all five apps below
│   │
│   │   ── sources; no Dockerfile or nginx.conf of their own ──
│   ├── merchant-portal/            index.html
│   ├── hub-app/                    index.html
│   ├── driver-app/                 index.html · app.js · app.css
│   ├── customer-portal/            index.html · app.js · app.css
│   └── admin-console/              index.html · app.js · app.css
│
├── packages/                       ── SHARED ────────────────────────
│   └── web-shared/
│       ├── api.js                  typed-ish API client for both services
│       ├── ui.js                   badges, dates, toasts, signature pad
│       └── base.css                design system (light + dark)
│
├── services/                       ── BACKEND ───────────────────────
│   ├── consignment-service/        parcels, scans, labels, STATE MACHINE
│   └── dispatch-service/           runsheets, GPS, delivery, POD
│
├── db/init.sql                     2 schemas, 5 tables
├── simulator/                      traffic generator
└── docs/
```

### How sharing works

The single `web` image is built **from the repo root** so it can copy the shared package:

```yaml
build:
  context: .                              # repo root, not the app folder
  dockerfile: apps/web/Dockerfile
```

```dockerfile
COPY packages/web-shared/ /usr/share/nginx/html/   # shared first
COPY apps/driver-app/     /usr/share/nginx/html/   # app wins on collision
```

Copied at **build time**, not fetched at runtime — so no app depends on another being up. The cost
is that changing `packages/web-shared` requires rebuilding the `web` image — one build, not five.


---

## 3. API surface

Legend: **✅ built** · ⬜ specified, not built

### Consignment & Hub Service — `/api/consignment/v1`

| | Endpoint | Used by | Purpose |
|---|---|---|---|
| ✅ | `POST /waybills` | Merchant | Book a shipment, issue an AWB |
| ✅ | `GET /waybills/{awb}` | Customer, Driver | Track (Redis-cached) |
| ✅ | `GET /waybills/{awb}/history` | Customer | Full scan history |
| ✅ | `GET /waybills/{awb}/label` | Merchant | Printable HTML label |
| ✅ | `GET /waybills` | Merchant | List, filter by status, paginate |
| ✅ | `GET /stats` | Merchant | Dashboard aggregates |
| ✅ | `POST /scans` | Hub | `IN_TRANSIT` / `ARRIVED_AT_FACILITY` |
| ✅ | `PATCH /waybills/{awb}/status` | Driver, Dispatch | `OUT_FOR_DELIVERY` / `DELIVERED` / `RTO` |
| ⬜ | `POST /waybills/bulk` | Merchant | CSV → many AWBs, partial-success report |
| ⬜ | `GET /waybills/{awb}/barcode` | Merchant, Hub | Code128 SVG |
| ⬜ | `POST /labels/sheet` | Merchant | N-up label sheet for a printer |
| ⬜ | `POST /pickups` | Merchant | Schedule a collection |
| ⬜ | `GET /merchants/{id}/shipments` | Merchant | Scoped list (needs auth) |

### Fleet & Dispatch Service — `/api/dispatch/v1`

| | Endpoint | Used by | Purpose |
|---|---|---|---|
| ✅ | `GET /drivers` | Driver | Identity picker |
| ✅ | `GET /runsheets?driver_id=` | Driver | My runsheets + stop counts |
| ✅ | `GET /runsheets/{id}` | Driver | Stops, **enriched with consignee details** |
| ✅ | `POST /runsheets` | Hub | Assign parcels to a driver |
| ✅ | `POST /gps` | Driver | Position ping → Redis (**202**) |
| ✅ | `GET /vehicles` | Ops | All reporting vehicles (Redis `SCAN`) |
| ✅ | `GET /vehicles/{id}/location` | Ops | Last known position |
| ✅ | `POST /delivery` | Driver | Outcome **+ POD** (OTP / signature) |
| ⬜ | `POST /bags` | Hub | Group parcels into a bag |
| ⬜ | `POST /manifests` | Hub | Seal a bag to a vehicle |
| ⬜ | `GET /runsheets/{id}/route` | Driver | Optimised stop order |

### Two endpoints worth explaining

**`GET /runsheets/{id}` calls consignment N times.** A driver needs an address and phone number per
stop, and dispatch does not own that data. So it asks — one HTTP call per stop, inside the handler.

That is N+1 calls, and it is the right choice here: runsheets cap at 50 stops, and the alternative
(dispatch reading `consignment.waybills` directly) would break the ownership rule that makes this
microservices. If it ever became hot, the fix is a **batch endpoint on consignment**, not a
cross-schema `SELECT`. Enrichment failures degrade gracefully — the stop still renders with its AWB.

**`POST /delivery` writes locally first, then calls consignment.** If consignment is unreachable it
returns **207 Multi-Status**: the attempt is saved, the parcel status is not. The driver app treats
207 as success-with-a-note rather than an error, because the driver's work *was* recorded.

---

## 4. Data model additions

The driver app forced a real schema gap into the open.

```sql
-- Before: dispatch.runsheets recorded WHO was driving, and
-- dispatch.delivery_attempts recorded what had ALREADY been attempted.
-- Nothing recorded what was ON the runsheet -- so "what am I delivering
-- today?" was unanswerable, which is the driver app's entire home screen.

CREATE TABLE dispatch.runsheet_items (
    runsheet_id  VARCHAR(40) NOT NULL REFERENCES dispatch.runsheets(id),
    awb          VARCHAR(20) NOT NULL,     -- no FK: cross-schema boundary
    sequence     INTEGER     NOT NULL,     -- delivery order
    status       VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (runsheet_id, awb)
);

CREATE INDEX idx_items_runsheet_status
    ON dispatch.runsheet_items (runsheet_id, status, sequence);
```

Proof-of-delivery columns on `delivery_attempts`:

```sql
pod_type     VARCHAR(20),    -- OTP | SIGNATURE | PHOTO
pod_receiver VARCHAR(120),   -- who actually took it
pod_data     TEXT            -- OTP code, or a signature data: URL
```

`pod_data` is capped at 200 KB by the Pydantic model. An unbounded `TEXT` field accepting base64
images is a denial-of-service waiting to happen. A production system would put the image in S3 and
store only the key.

> **Applying these:** Postgres runs `db/init.sql` only on a fresh volume.
> `docker compose down -v && docker compose up --build -d`.

---

## 5. Authentication — what exists and what does not

**There is none.** Be clear-eyed about this:

- The driver "login" is a picker that writes to `localStorage`. Anyone can be any driver.
- The hub app lets you scan as any hub.
- The merchant portal lets you book as any merchant.
- The customer portal is genuinely public — the AWB *is* the credential, which matches how every
  real courier tracking page works.

`packages/web-shared/api.js` exposes a `session` helper that stores the selected identity. It is
labelled in the source as **not authentication**.

### What real auth would take

1. **Backend first.** A `users` table, `POST /api/v1/auth/login` issuing a short-lived JWT with a
   `role` claim (`merchant` / `hub_operator` / `driver` / `admin`), and a FastAPI dependency that
   validates it.
2. **Scope the data.** `GET /waybills` must return only the calling merchant's shipments;
   `GET /runsheets` only the calling driver's.
3. **Then the frontend.** A real login screen, the token in `Authorization: Bearer`, and a 401
   interceptor in the shared client.

Do it in that order. Auth added only in the frontend is decoration — anyone can `curl` the API.

---

## 6. Running it

```bash
docker compose up --build -d      # 9 containers
docker compose ps
```

| | |
|---|---|
| **Launcher** | **http://localhost** |
| Merchant Portal | http://localhost/merchant/ |
| Hub Scanner | http://localhost/hub/ |
| Driver App | http://localhost/driver/ |
| Customer Tracking | http://localhost/track/ |
| Consignment API docs | http://localhost:8001/docs |
| Dispatch API docs | http://localhost:8002/docs |

Seed data — the driver app needs runsheets to exist:

```bash
docker compose --profile sim run --rm simulator --parcels 20
```

### Walking the full lifecycle in a browser

1. **Merchant** → book a shipment → copy the AWB
2. **Hub** → select `HUB-BLR-01`, scan type *Outbound*, scan the AWB → `IN_TRANSIT`
3. **Hub** → switch to `HUB-DEL-03`, *Inbound*, scan again → `ARRIVED_AT_FACILITY`
4. Create a runsheet (simulator, or `POST /api/dispatch/v1/runsheets`)
5. **Driver** → pick the driver → open the runsheet → *Scan out for delivery* → *Complete delivery*
   with OTP or a signature
6. **Customer** → `/track/?awb=FP…` → the whole journey with timestamps

That is the entire parcel lifecycle, clickable, no terminal.

---

## 7. Notable implementation details

**Relative asset paths everywhere.** Apps are served both at `/driver/` (path routing) and
at `/` (own hostname). `<link href="./base.css">` works in both; `/base.css` would break behind
the prefix.

**`resolver 127.0.0.11` in the web container.** Docker's embedded DNS. Without it nginx resolves upstream
service names once at boot and keeps a stale IP after any container restart.

**The signature pad scales for device pixel ratio.** Without matching the canvas backing store to
`devicePixelRatio`, strokes render blurry and land offset from the finger on phones. It uses pointer
events so mouse, touch and stylus share one code path.

**The hub scanner keeps focus on the input.** A barcode gun is a keyboard: it types the AWB and
presses Enter. Lost focus means scans silently go nowhere, so a document-level click handler
restores it. The field also clears immediately on submit rather than awaiting the response —
throughput matters more than sequencing.

**The driver app only shows legal actions.** *Complete delivery* is disabled until the parcel is
`OUT_FOR_DELIVERY`. The backend enforces this with a 409 regardless, but offering an impossible
button and then erroring is a poor experience.

**Container healthchecks must use `127.0.0.1`, not `localhost`.** In `nginx:alpine`, `localhost`
resolves to `::1` first, but `listen 80` binds IPv4 only — so `wget http://localhost/healthz` gets
*connection refused* while the container serves traffic perfectly. All six app images had this bug;
every one reported `unhealthy` while working fine, which is worse than having no healthcheck at all
because it makes `docker compose ps` lie.

**The customer rail is simpler than the state machine.** Multiple hub hops collapse into one
"In transit" step, because a customer does not care how many sort centres their parcel passed
through. `RTO` marks the delivery step red rather than pretending the parcel is still progressing.

---

## 8. Next steps, in order of value

1. **Bulk booking + barcode labels** (merchant) — the biggest gap, and CSV-with-partial-success is a
   genuinely instructive API design problem
2. **Bags and manifests** (hub) — completes the middle-mile model
3. **Authentication** — backend first, per §5
4. **One app in React** — if you want the CV line, port the merchant portal only
5. **Observability** — [the add-on](FleetPulse-Addon-Observability.md); now that there are 9
   containers, `TriggeredRules`-style "nothing is happening" alerts start to matter
