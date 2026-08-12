# Add-On: Notification Service

A third service that pushes parcel status updates to merchants — **without introducing a message
broker.**

Prerequisite: [FleetPulse-Simple.md](FleetPulse-Simple.md) working, and
[FleetPulse-Architecture.md](FleetPulse-Architecture.md) read. Doing the
[Observability add-on](FleetPulse-Addon-Observability.md) first is recommended — a background worker
you cannot see is a background worker you cannot debug.

---

## 0. The interesting problem

You want merchants notified when their parcel changes status. The obvious answer is a message broker,
and you have deliberately excluded one. **Good** — solving this without a broker teaches you more
than adding RabbitMQ would.

### 0.1 Four options, honestly compared

| # | Approach | Decoupled? | Survives outage? | Retryable? | New infra |
|---|---|---|---|---|---|
| 1 | Consignment calls Notification directly over HTTP | ❌ | ❌ | ❌ | None |
| 2 | Notification polls `scan_events` for new rows | ✅ | ✅ | ⚠️ | None |
| 3 | **Outbox table + polling worker** | ✅ | ✅ | ✅ | None |
| 4 | Postgres `LISTEN` / `NOTIFY` | ✅ | ❌ | ❌ | None |

**Option 1 is what most people build, and it is the wrong answer.** If Notification is down or slow,
the hub scan fails — you have made notifications a hard dependency of your core business operation.
A courier scanning a parcel should never fail because an email service is having a bad day.

**Option 4 is elegant but lossy.** `LISTEN`/`NOTIFY` is real Postgres pub/sub with no extra
infrastructure, but notifications are fire-and-forget: if no listener is connected at that instant,
the message is gone forever. Fine as an optimisation, unusable as the mechanism.

**Option 3 is the answer.** The transactional outbox pattern.

### 0.2 What the outbox pattern is

When Consignment changes a parcel's status, it writes **two rows in one transaction**: the status
change, and a row in an `outbox` table describing the event. A separate worker reads unprocessed
outbox rows and delivers them.

```mermaid
sequenceDiagram
    participant H as Hub scanner
    participant C as Consignment
    participant P as Postgres
    participant W as Notification worker
    participant M as Merchant webhook

    H->>C: POST /api/v1/scans
    rect rgb(230, 240, 255)
        Note over C,P: ONE transaction
        C->>P: UPDATE waybills SET status
        C->>P: INSERT scan_events
        C->>P: INSERT outbox (event)
    end
    C-->>H: 201 (fast — no webhook call in the request path)

    loop every 2 seconds
        W->>P: SELECT ... WHERE processed_at IS NULL<br/>FOR UPDATE SKIP LOCKED
        P-->>W: pending events
        W->>M: POST webhook (HMAC-signed)
        alt success
            M-->>W: 200
            W->>P: UPDATE outbox SET processed_at = now()
        else failure
            M-->>W: 500 / timeout
            W->>P: UPDATE outbox SET attempts = attempts + 1,<br/>next_attempt_at = now() + backoff
        end
    end
```

**Why this is genuinely correct**, not a workaround:

- **Atomic.** The status change and the event are in one transaction. There is no window where a
  parcel is delivered but no event exists — the classic dual-write bug.
- **Durable.** Events survive a crash of every service. They are rows in Postgres.
- **Retryable.** A failed delivery increments a counter and gets picked up again.
- **Decoupled.** Notification can be down for an hour; events queue up and drain on recovery.
- **Debuggable.** You can `SELECT * FROM outbox WHERE processed_at IS NULL` and *see* your queue.
  Try that with RabbitMQ.

This is the same pattern the [production blueprint](FleetPulse-Blueprint.md) uses with a real broker.
You are learning the important half — the half that stays the same regardless of broker.

### 0.3 It also fixes a bug you already have

[Architecture §4.6](FleetPulse-Architecture.md) describes Dispatch returning `207 Multi-Status`
because it recorded a delivery attempt but could not reach Consignment. With an outbox, Dispatch
writes to its own outbox in the same transaction as the attempt, and the worker retries the status
update until it succeeds. **The 207 disappears.**

---

## 1. Architecture with the third service

```mermaid
flowchart TB
    CS["<b>Consignment &amp; Hub</b><br/>writes outbox rows"]
    DS["<b>Fleet &amp; Dispatch</b><br/>writes outbox rows"]
    NS["<b>Notification Service</b><br/>─────────────<br/>API: merchant subscriptions<br/><b>Worker: polls outbox</b>"]
    PG[("PostgreSQL<br/>consignment · dispatch<br/><b>notification</b>")]
    RD[("Redis")]
    MW["Merchant webhook<br/><i>(webhook-sink locally)</i>"]

    CS -->|"INSERT outbox<br/>(same txn)"| PG
    DS -->|"INSERT outbox<br/>(same txn)"| PG
    NS -->|"poll + mark processed"| PG
    NS -->|"POST signed payload"| MW
    NS --> RD
    DS -->|"REST (unchanged)"| CS
```

**No new arrows into Consignment or Dispatch.** They gained one `INSERT` each; they do not know
Notification exists. That is the decoupling — you could delete the notification service tomorrow and
the core would not notice.

Memory cost: **+70 MB** (~490 MB total on the `t3.micro`, still comfortable).

---

## 2. Schema

```sql
-- db/init.sql — append
CREATE SCHEMA IF NOT EXISTS notification;

-- ==========================================================
-- THE OUTBOX. Written by consignment and dispatch, read by
-- the notification worker. This table IS the message queue.
-- ==========================================================
CREATE TABLE IF NOT EXISTS notification.outbox (
    id              BIGSERIAL PRIMARY KEY,
    -- Stable idempotency key. UNIQUE means a retried API call cannot
    -- enqueue the same notification twice.
    event_key       VARCHAR(120) NOT NULL UNIQUE,
    awb             VARCHAR(20)  NOT NULL,
    merchant_name   VARCHAR(120) NOT NULL,
    event_type      VARCHAR(40)  NOT NULL,   -- STATUS_CHANGED | DELIVERED | RTO
    payload         JSONB        NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,             -- NULL = still pending
    attempts        INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_error      TEXT
);

-- PARTIAL index: only unprocessed rows are indexed, so the worker's query
-- stays fast forever even as the table grows to millions of processed rows.
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON notification.outbox (next_attempt_at)
    WHERE processed_at IS NULL;

-- Who wants notifications, and where.
CREATE TABLE IF NOT EXISTS notification.subscriptions (
    id             BIGSERIAL PRIMARY KEY,
    merchant_name  VARCHAR(120) NOT NULL,
    webhook_url    TEXT         NOT NULL,
    secret         VARCHAR(120) NOT NULL,    -- for HMAC signing
    events         TEXT[]       NOT NULL DEFAULT ARRAY['*'],
    active         BOOLEAN      NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (merchant_name, webhook_url)
);

-- Audit trail: every delivery attempt, successful or not.
CREATE TABLE IF NOT EXISTS notification.delivery_log (
    id           BIGSERIAL PRIMARY KEY,
    outbox_id    BIGINT      NOT NULL,
    webhook_url  TEXT        NOT NULL,
    status_code  INTEGER,
    duration_ms  INTEGER,
    error        TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_log_outbox
    ON notification.delivery_log (outbox_id);
```

---

## 3. Producers — the small change to existing services

```python
# services/consignment-service/app/outbox.py
"""Write an event to the outbox. MUST be called inside the caller's
transaction — that is the entire point of the pattern."""
import json


def enqueue(cur, *, event_key: str, awb: str, merchant_name: str,
            event_type: str, payload: dict) -> None:
    cur.execute(
        """
        INSERT INTO notification.outbox
            (event_key, awb, merchant_name, event_type, payload)
        VALUES (%s, %s, %s, %s, %s)
        -- A retried request produces the same event_key; ignore the duplicate
        -- rather than notifying the merchant twice.
        ON CONFLICT (event_key) DO NOTHING
        """,
        (event_key, awb, merchant_name, event_type, json.dumps(payload)),
    )
```

Wire it into the existing `record_scan` handler — three added lines, same cursor, same transaction:

```python
# services/consignment-service/app/main.py — inside record_scan()
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

        # ---- NEW: same cursor, same transaction ----
        outbox.enqueue(
            cur,
            # Deterministic key: same parcel + same status = same event.
            event_key=f"{req.awb}:{req.status}",
            awb=req.awb,
            merchant_name=merchant_name,      # from the SELECT above
            event_type="STATUS_CHANGED",
            payload={
                "awb": req.awb,
                "previous_status": current,
                "status": req.status,
                "hub_id": req.hub_id,
                "remarks": req.remarks,
            },
        )

        conn.commit()   # all three rows land together, or none do
```

> **If `conn.commit()` fails, nothing happened** — no status change, no scan event, no notification.
> If it succeeds, all three exist. There is no in-between. That property is the whole pattern, and
> it is why the `INSERT` must share the cursor rather than opening its own connection.

---

## 4. The Notification Service

### 4.1 Structure

```
services/notification-service/
├── app/
│   ├── main.py          # FastAPI: subscriptions + health + metrics
│   ├── worker.py        # the polling loop  ← the interesting file
│   ├── delivery.py      # signed webhook POST with retries
│   ├── db.py
│   └── models.py
├── tests/
├── requirements.txt
└── Dockerfile
```

### 4.2 The worker loop

```python
# services/notification-service/app/worker.py
"""Polls the outbox and delivers notifications.

Runs as a background task inside the same process as the API. At this scale
that is simpler than a separate deployment, and you can split it later if
the workloads ever need to scale independently.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from .db import get_conn
from .delivery import deliver_webhook, DeliveryError
from . import metrics

log = logging.getLogger("worker")

POLL_INTERVAL_SECONDS = 2
BATCH_SIZE = 20
MAX_ATTEMPTS = 5
# Exponential backoff by attempt number: 10s, 60s, 5m, 30m, 2h
BACKOFF_SECONDS = [10, 60, 300, 1800, 7200]


async def run_worker(stop: asyncio.Event) -> None:
    log.info("outbox worker started")
    while not stop.is_set():
        try:
            processed = await asyncio.to_thread(process_batch)
            # Only sleep when there was nothing to do — drain fast under load.
            if processed == 0:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass                              # normal: the poll interval elapsed
        except Exception:
            log.exception("worker iteration failed; continuing")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    log.info("outbox worker stopped")


def process_batch() -> int:
    """Claim and deliver one batch. Returns how many rows were handled."""
    with get_conn() as conn, conn.cursor() as cur:
        # FOR UPDATE SKIP LOCKED lets multiple worker replicas run safely:
        # each locks a different set of rows instead of fighting over the
        # same ones. This is what makes the outbox horizontally scalable.
        cur.execute(
            """
            SELECT id, event_key, awb, merchant_name, event_type, payload, attempts
            FROM notification.outbox
            WHERE processed_at IS NULL
              AND next_attempt_at <= now()
              AND attempts < %s
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (MAX_ATTEMPTS, BATCH_SIZE),
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        for (oid, event_key, awb, merchant, event_type, payload, attempts) in rows:
            _deliver_one(cur, oid, awb, merchant, event_type, payload, attempts)

        conn.commit()
        return len(rows)


def _deliver_one(cur, oid, awb, merchant, event_type, payload, attempts) -> None:
    cur.execute(
        """SELECT webhook_url, secret, events FROM notification.subscriptions
           WHERE merchant_name = %s AND active = true""",
        (merchant,),
    )
    subs = cur.fetchall()

    if not subs:
        # Nobody subscribed. This is SUCCESS, not failure — mark it done or
        # the row is retried forever.
        cur.execute(
            "UPDATE notification.outbox SET processed_at = now() WHERE id = %s", (oid,)
        )
        metrics.notifications_skipped_total.inc()
        return

    all_ok = True
    for (url, secret, events) in subs:
        if "*" not in events and event_type not in events:
            continue

        body = {"event": event_type, "awb": awb,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "data": payload}
        try:
            status, duration_ms = deliver_webhook(url, secret, body)
            cur.execute(
                """INSERT INTO notification.delivery_log
                       (outbox_id, webhook_url, status_code, duration_ms)
                   VALUES (%s, %s, %s, %s)""",
                (oid, url, status, duration_ms),
            )
            metrics.webhook_deliveries_total.labels(result="success").inc()
        except DeliveryError as e:
            all_ok = False
            cur.execute(
                """INSERT INTO notification.delivery_log
                       (outbox_id, webhook_url, status_code, duration_ms, error)
                   VALUES (%s, %s, %s, %s, %s)""",
                (oid, url, e.status_code, e.duration_ms, str(e)),
            )
            metrics.webhook_deliveries_total.labels(result="failure").inc()
            log.warning("delivery failed awb=%s url=%s: %s", awb, url, e)

    if all_ok:
        cur.execute(
            "UPDATE notification.outbox SET processed_at = now() WHERE id = %s", (oid,)
        )
    else:
        nxt = attempts + 1
        backoff = BACKOFF_SECONDS[min(nxt, len(BACKOFF_SECONDS)) - 1]
        cur.execute(
            """UPDATE notification.outbox
               SET attempts = %s,
                   next_attempt_at = now() + %s::interval,
                   last_error = %s
               WHERE id = %s""",
            (nxt, f"{backoff} seconds", "webhook delivery failed", oid),
        )
        if nxt >= MAX_ATTEMPTS:
            # Exhausted. Stays unprocessed with attempts = MAX — this is your
            # dead-letter queue, and it is just a WHERE clause.
            metrics.notifications_dead_lettered_total.inc()
            log.error("giving up on outbox id=%s awb=%s after %s attempts", oid, awb, nxt)
```

### 4.3 Signed webhook delivery

```python
# services/notification-service/app/delivery.py
"""HMAC-signed webhook delivery.

Signing lets merchants verify the request genuinely came from you, which is
the standard practice for every real webhook provider.
"""
import hashlib
import hmac
import json
import time

import httpx

TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class DeliveryError(Exception):
    def __init__(self, message, status_code=None, duration_ms=None):
        super().__init__(message)
        self.status_code = status_code
        self.duration_ms = duration_ms


def sign(secret: str, body: bytes, timestamp: str) -> str:
    """Sign timestamp + body. Including the timestamp prevents an attacker
    from replaying an old captured request."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return mac.hexdigest()


def deliver_webhook(url: str, secret: str, payload: dict) -> tuple[int, int]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))

    headers = {
        "Content-Type": "application/json",
        "X-FleetPulse-Timestamp": ts,
        "X-FleetPulse-Signature": f"sha256={sign(secret, body, ts)}",
        "X-FleetPulse-Event": payload["event"],
        "User-Agent": "FleetPulse-Webhooks/1.0",
    }

    start = time.perf_counter()
    try:
        r = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT)
    except httpx.RequestError as e:
        raise DeliveryError(f"unreachable: {e}",
                            duration_ms=int((time.perf_counter() - start) * 1000)) from e

    duration_ms = int((time.perf_counter() - start) * 1000)

    # 4xx (except 429) means the merchant's endpoint rejected it — retrying
    # will not help. Treat as permanent so we stop wasting attempts.
    if 400 <= r.status_code < 500 and r.status_code != 429:
        raise DeliveryError(f"permanent rejection {r.status_code}",
                            r.status_code, duration_ms)
    if r.status_code >= 500 or r.status_code == 429:
        raise DeliveryError(f"transient {r.status_code}", r.status_code, duration_ms)

    return r.status_code, duration_ms
```

### 4.4 API and worker startup

```python
# services/notification-service/app/main.py
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from .db import get_conn, init_pool
from .worker import run_worker
from .models import SubscriptionRequest

log = logging.getLogger("notification")
_stop = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    task = asyncio.create_task(run_worker(_stop))
    yield
    # Graceful shutdown: let the current batch finish before exiting, so
    # in-flight webhooks are not abandoned mid-delivery.
    _stop.set()
    await asyncio.wait_for(task, timeout=15)


app = FastAPI(
    title="FleetPulse — Notification Service",
    description="Merchant webhook subscriptions and outbox-driven delivery.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "notification"}


@app.post("/api/v1/subscriptions", status_code=201)
def create_subscription(req: SubscriptionRequest) -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO notification.subscriptions
                   (merchant_name, webhook_url, secret, events)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (merchant_name, webhook_url)
               DO UPDATE SET secret = EXCLUDED.secret,
                             events = EXCLUDED.events,
                             active = true
               RETURNING id""",
            (req.merchant_name, req.webhook_url, req.secret, req.events),
        )
        sub_id = cur.fetchone()[0]
        conn.commit()
    return {"id": sub_id, "merchant_name": req.merchant_name}


@app.get("/api/v1/outbox/stats")
def outbox_stats() -> dict:
    """Operational visibility — the endpoint you will actually use."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE processed_at IS NULL)                     AS pending,
              count(*) FILTER (WHERE processed_at IS NOT NULL)                 AS processed,
              count(*) FILTER (WHERE processed_at IS NULL AND attempts >= 5)   AS dead_lettered,
              coalesce(extract(epoch FROM now() - min(created_at))
                       FILTER (WHERE processed_at IS NULL), 0)::int            AS oldest_pending_seconds
            FROM notification.outbox
        """)
        pending, processed, dead, oldest = cur.fetchone()
    return {"pending": pending, "processed": processed,
            "dead_lettered": dead, "oldest_pending_seconds": oldest}
```

`oldest_pending_seconds` is the health metric that matters. A growing value means the worker is
stuck while every HTTP endpoint still returns 200 — the failure with no error signal.

---

## 5. Wiring it up

```yaml
# docker-compose.yml — add
  notification-service:
    build: ./services/notification-service
    container_name: fp-notification
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      REDIS_URL: redis://redis:6379/0
      LOG_LEVEL: INFO
      POLL_INTERVAL_SECONDS: "2"
    ports:
      - "8003:8000"
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    restart: unless-stopped

  # Pretend merchant endpoint. Reflects requests back so you can inspect
  # exactly what you sent, including the signature headers.
  webhook-sink:
    image: mendhak/http-https-echo:34
    container_name: fp-webhook-sink
    environment: { HTTP_PORT: 8080 }
    ports: ["8090:8080"]
```

```bash
docker compose up -d

# Subscribe a merchant to its own webhook endpoint
curl -X POST localhost:8003/api/v1/subscriptions -H 'Content-Type: application/json' -d '{
  "merchant_name": "Nykaa",
  "webhook_url": "http://webhook-sink:8080/hook",
  "secret": "test-secret-123",
  "events": ["*"]
}'

python simulator/simulate_delhivery_scans.py --parcels 5

docker compose logs webhook-sink | grep -i x-fleetpulse-signature
curl -s localhost:8003/api/v1/outbox/stats | jq
```

### 5.1 Kubernetes

The worker runs inside the API process, so this is an ordinary Deployment.

```yaml
# k8s/base/10-notification.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: notification-service
  namespace: fleetpulse
spec:
  # Keep at 1 while learning. FOR UPDATE SKIP LOCKED makes >1 safe, but
  # a single replica keeps the log output comprehensible.
  replicas: 1
  selector:
    matchLabels: { app: notification-service }
  template:
    metadata:
      labels: { app: notification-service }
    spec:
      # Let the current batch drain instead of killing mid-webhook.
      terminationGracePeriodSeconds: 30
      containers:
        - name: notification
          image: fleetpulse/notification-service:latest
          ports: [{ containerPort: 8000, name: http }]
          envFrom:
            - configMapRef: { name: fleetpulse-config }
            - secretRef:    { name: fleetpulse-secrets }
          resources:
            requests: { memory: "96Mi", cpu: "50m" }
            limits:   { memory: "256Mi" }
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 15
            periodSeconds: 20
```

---

## 6. Testing the failure paths

The outbox only earns its complexity under failure. Prove each one.

### Test 1 — Merchant endpoint is down

```bash
docker compose stop webhook-sink
python simulator/simulate_delhivery_scans.py --parcels 3
# Scans still succeed (fast!). Events queue up:
curl -s localhost:8003/api/v1/outbox/stats | jq   # pending > 0

docker compose start webhook-sink
sleep 15
curl -s localhost:8003/api/v1/outbox/stats | jq   # pending back to 0
```

✅ Core operations were never blocked by a failing downstream, and nothing was lost.

### Test 2 — Notification service is down

```bash
docker compose stop notification-service
python simulator/simulate_delhivery_scans.py --parcels 5   # unaffected
docker compose start notification-service                  # drains automatically
```

✅ The producers do not know or care that the consumer exists.

### Test 3 — Permanent rejection

```bash
curl -X POST localhost:8003/api/v1/subscriptions -d '{
  "merchant_name": "BadMerchant",
  "webhook_url": "http://webhook-sink:8080/hook?status=404",
  "secret": "x", "events": ["*"]
}' -H 'Content-Type: application/json'
```

✅ A 404 is treated as permanent — no retries wasted. A 500 would back off and retry.

### Test 4 — Dead lettering

```sql
-- Your DLQ is a WHERE clause.
SELECT id, awb, event_type, attempts, last_error
FROM notification.outbox
WHERE processed_at IS NULL AND attempts >= 5;
```

### Test 5 — Idempotency

```bash
# Send the same scan twice. ON CONFLICT (event_key) DO NOTHING means the
# merchant is notified exactly once.
```

---

## 7. Metrics to add

If you did the [Observability add-on](FleetPulse-Addon-Observability.md):

```python
outbox_pending = Gauge("fleetpulse_outbox_pending", "Unprocessed outbox rows")
outbox_oldest_seconds = Gauge("fleetpulse_outbox_oldest_seconds",
                              "Age of the oldest unprocessed row")
webhook_deliveries_total = Counter("fleetpulse_webhook_deliveries_total",
                                   "Webhook attempts", ["result"])
notifications_dead_lettered_total = Counter(
    "fleetpulse_notifications_dead_lettered_total", "Gave up after max attempts")
notifications_skipped_total = Counter(
    "fleetpulse_notifications_skipped_total", "No active subscription")
```

```yaml
- alert: OutboxBacklogGrowing
  # THE alert for this service. Nothing 500s, no pod restarts — notifications
  # just silently stop. Only outbox age catches it.
  expr: fleetpulse_outbox_oldest_seconds > 300
  for: 5m
  labels: { severity: critical }
  annotations:
    summary: "Outbox worker stalled — oldest event is {{ $value }}s old"

- alert: WebhooksDeadLettering
  expr: increase(fleetpulse_notifications_dead_lettered_total[1h]) > 0
  for: 5m
  labels: { severity: warning }
```

---

## 8. Milestone checklist

### Milestone N1 — Outbox plumbing (Days 1–3)
- [ ] Add the `notification` schema and three tables to `init.sql`
- [ ] `outbox.py` in consignment-service; call `enqueue()` inside the existing transaction
- [ ] Verify: run a scan, then `SELECT * FROM notification.outbox` — the row is there

> ✅ Events accumulate in Postgres. No consumer yet, and that is fine.

### Milestone N2 — The worker (Days 4–7)
- [ ] Scaffold notification-service (copy the shape of consignment-service)
- [ ] `POST /api/v1/subscriptions`
- [ ] `delivery.py` with HMAC signing
- [ ] `worker.py` with `FOR UPDATE SKIP LOCKED` and backoff
- [ ] `webhook-sink` in Compose
- [ ] `GET /api/v1/outbox/stats`

> ✅ Run the simulator; webhooks arrive at the sink with valid signatures.

### Milestone N3 — Failure handling (Days 8–10)
- [ ] Run all five tests in §6
- [ ] Permanent vs transient failure distinction
- [ ] Dead-letter query
- [ ] Graceful shutdown drains the batch

> ✅ You can stop any component, restart it, and lose nothing.

### Milestone N4 — Production shape (Days 11–14)
- [ ] Dispatch also writes to the outbox — **the 207 from Architecture §4.6 disappears**
- [ ] Metrics + the `OutboxBacklogGrowing` alert
- [ ] Kubernetes manifests
- [ ] Update the architecture diagram in your README

---

## 9. Interview answers

**"How do services communicate asynchronously without a broker?"**
> "The transactional outbox pattern. When consignment changes a parcel's status it writes the status
> change and an outbox row in the same database transaction, so they're atomic — there's no window
> where the parcel is delivered but no event exists. A worker polls the outbox with `FOR UPDATE SKIP
> LOCKED`, delivers the webhook, and marks the row processed. It gives you durability, retries with
> backoff, and dead-lettering with no extra infrastructure. The trade-off is polling latency —
> couple of seconds instead of milliseconds — which is irrelevant for merchant notifications."

**"Why not just call the notification service directly from consignment?"**
> "That makes notifications a hard dependency of a core business operation. If the notification
> service is down or slow, hub scans start failing — a courier can't scan a parcel because an email
> service is having a bad day. The outbox means consignment's only obligation is one INSERT in a
> transaction it was already running."

**"How would this change with a real message broker?"**
> "Less than you'd think. You'd keep the outbox — the dual-write problem doesn't go away just
> because you added RabbitMQ — and change the worker from 'poll and POST a webhook' to 'poll and
> publish to the broker.' The consumer side gains fan-out to multiple subscribers. The atomicity
> guarantee, idempotency keys, and retry logic are identical. That's why I built it this way first:
> the hard part is the pattern, not the broker."

**"What's the failure mode you're most worried about?"**
> "The worker dying silently. Every HTTP endpoint still returns 200, no pod restarts, no error rate
> moves — notifications just stop. That's why I expose `oldest_pending_seconds` and alert on it
> rather than on error counts. An absence-of-progress alert is the only thing that catches it."
