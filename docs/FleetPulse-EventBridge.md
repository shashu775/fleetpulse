# FleetPulse on EventBridge

Replaces RabbitMQ with **Amazon EventBridge** as the primary asynchronous event bus, with
**LocalStack** for a zero-charge local sandbox.

This supersedes [Blueprint §0.1 and §1.5](FleetPulse-Blueprint.md) (the RabbitMQ topology) and the
NATS deviation in [Zero-Cost §1.2](FleetPulse-Zero-Cost.md). Everything else in the blueprint —
the outbox pattern, idempotent consumers, the parcel state machine, trace propagation — carries
over unchanged, and §0.3 below explains why.

---

## 0. Architect's assessment

### 0.1 This is a good change, and it fixes a problem I flagged earlier

Three concrete wins beyond "no broker to operate":

**It eliminates the broker from the 1 GB box entirely.** The [zero-cost track](FleetPulse-Zero-Cost.md)
had to swap RabbitMQ (150 MB) for NATS (30 MB) just to fit on a `t3.micro`. With EventBridge the
broker moves off-instance completely — **~30–150 MB returned to your workload**, which is 15% of the
node. That single fact makes the free-tier architecture materially more comfortable.

**It gives you archive and replay natively.** In [Blueprint §0.1](FleetPulse-Blueprint.md) I said
event replay — reprocessing months of parcel history into a new service — was Kafka's strength and
RabbitMQ's weakness, and that wanting it was the one legitimate reason to reconsider the broker.
EventBridge Archive + Replay does it as a managed feature, with no Kafka. That argument is now
resolved in EventBridge's favour.

**Content-based routing beats topic exchanges.** RabbitMQ routes on a routing-key string.
EventBridge rules filter on *any field in the payload* — `detail.to_state`, `detail.hub_id`,
numeric ranges, prefix matches, `anything-but`. Routing "only high-value parcels in Bengaluru" is a
rule change, not a code change.

### 0.2 Three things you genuinely lose — read before committing

| Loss | Severity | Mitigation |
|---|---|---|
| **No ordering guarantee** | **High — correctness** | Causal guard in the consumer (§0.4). Non-negotiable |
| Higher latency (~0.5–2s vs sub-ms) | Low | Blueprint SLO is p99 < 5s; comfortably inside |
| Vendor lock-in | Medium | LocalStack for local dev; keep the publisher behind an interface |

**Ordering is the one that matters.** RabbitMQ gives you per-queue FIFO. **EventBridge guarantees
none.** A `ParcelDelivered` can arrive before the `ParcelOutForDelivery` that preceded it, and for a
state machine that is a correctness bug, not a nuisance.

And you cannot fix it with an SQS FIFO target. EventBridge *can* target a FIFO queue, but
`message_group_id` is a **static value in the rule** — you cannot set it per-event from
`detail.waybill`. One static group means one group for every parcel, which serialises your entire
pipeline. **Per-parcel FIFO ordering is not achievable through EventBridge.** Handle it in the
consumer instead (§0.4).

### 0.3 What does NOT change — and why that matters

The outbox pattern from [Blueprint §1.6](FleetPulse-Blueprint.md) is **still required**. It solved
the dual-write problem: writing to Postgres and then publishing can fail between the two steps.
Changing the publish target from AMQP to `PutEvents` does not change that at all. Only the relay's
final call differs; the table, the shared transaction, and `FOR UPDATE SKIP LOCKED` are identical.

Likewise: idempotent consumers keyed on `event_id`, the ULID envelope, and `traceparent` propagation
all carry over. **If you were tempted to think a managed bus removes the need for the outbox, it does
not.** At-least-once delivery plus idempotent consumers remains the only correct answer.

### 0.4 Handling out-of-order events — the causal guard

Every consumer that mutates parcel state must reject events that are stale or illegal, using state
already in its own database:

```go
// Reject if (a) already processed, (b) older than the last event we applied,
// or (c) not a legal transition from where the parcel actually is.
func (c *Consumer) applyTransition(ctx context.Context, ev Envelope) error {
	cur, err := c.q.GetParcelForUpdate(ctx, ev.Subject.Waybill)
	if err != nil { return err }

	if cur.LastEventID == ev.EventID {
		return nil                                  // exact duplicate — ack, do nothing
	}
	if ev.OccurredAt.Before(cur.LastEventAt) {
		metrics.StaleEvents.Inc()
		return nil                                  // arrived late; a newer event already won
	}
	if !domain.CanTransition(cur.State, ev.Detail.ToState) {
		metrics.IllegalTransitions.WithLabelValues(cur.State, ev.Detail.ToState).Inc()
		return ErrIllegalTransition                 // → DLQ for inspection, do NOT retry
	}
	return c.q.ApplyTransition(ctx, ev.Subject.Waybill, ev.Detail.ToState, ev.EventID, ev.OccurredAt)
}
```

`last_event_at` and `last_event_id` become columns on the parcel row. This is last-writer-wins with a
legality check — the standard answer for unordered delivery, and worth being able to explain.

### 0.5 Cost — and the one routing decision that breaks it

**EventBridge custom buses have no free tier.** The AWS Free Tier covers AWS-service events on the
*default* bus; your custom-source events bill at **$1.00 per million published**. SQS is genuinely
free-forever at 1M requests/month.

| Traffic through the bus | Events/month | Cost/month |
|---|---|---|
| Lifecycle only — 1,000 parcels/day (~8 events each) | 240k | **$0.24** |
| Lifecycle only — 10,000 parcels/day | 2.4M | **$2.40** |
| **+ GPS pings at 300/min** | **13.0M** | **$12.96** |
| **+ GPS pings at 1,000/min** | **43.2M** | **$43.20** |

> ### ⚠️ Do not route GPS telemetry through EventBridge.
> [Blueprint §0.2](FleetPulse-Blueprint.md) split lifecycle events from GPS telemetry on
> *engineering* grounds — opposite durability, ordering, and volume requirements. EventBridge adds a
> **financial** reason: at 300 pings/min, telemetry is **98% of your event volume and 98% of the
> bill**, for data whose value expires in ten seconds.
>
> **GPS writes go straight to Redis (last-known position) with a periodic batch flush to Postgres.**
> No bus, no per-event charge. `GPSLocationUpdated` stays defined in the schema registry — but you
> publish it **only on a meaningful change** (geofence crossing, hub arrival/departure, a
> stopped-too-long alert), which is a few events per parcel rather than thousands.

That reduces GPS from ~13M events/month to a few thousand, and keeps your total under **$0.50/month**
at student scale.

---

## 1. Architecture & Schema Design

### 1.1 Topology

```mermaid
flowchart LR
    subgraph PROD["Producers"]
        CS["consignment-service<br/>outbox relay"]
        FS["facility-service<br/>outbox relay"]
        DS["dispatch-service<br/>outbox relay"]
    end

    CS -->|"PutEvents<br/>fleetpulse.consignment"| BUS{{"fleetpulse-event-bus"}}
    FS -->|"PutEvents<br/>fleetpulse.facility"| BUS
    DS -->|"PutEvents<br/>fleetpulse.dispatch"| BUS

    BUS --> R1["Rule: notification-all-parcel-events<br/>detail-type: ParcelRegistered,<br/>ParcelHubScanned, ParcelOutForDelivery,<br/>ParcelDelivered"]
    BUS --> R2["Rule: facility-inbound-expected<br/>detail-type: ParcelRegistered"]
    BUS --> R3["Rule: dispatch-assignable<br/>detail-type: ParcelHubScanned<br/>+ detail.scan_type = INBOUND"]

    R1 --> Q1[["SQS notification-events"]]
    R2 --> Q2[["SQS facility-inbound"]]
    R3 --> Q3[["SQS dispatch-assignable"]]

    Q1 --> NS["notification-service"]
    Q2 --> FS2["facility-service"]
    Q3 --> DS2["dispatch-service"]

    Q1 -.->|maxReceiveCount 5| D1[("notification-events-dlq")]
    BUS -.-> ARC[("Archive<br/>30-day retention<br/>replayable")]

    DS3["dispatch-service<br/>GPS ingest"] -->|"direct write — NOT via bus"| RD[("Redis<br/>last position")]
    RD -.->|"batch flush 5s"| PG[("dispatch_db")]
```

Note the GPS path bypasses the bus entirely, per §0.5.

### 1.2 Event catalogue

| Source | DetailType | Producer | Consumers |
|---|---|---|---|
| `fleetpulse.consignment` | `ParcelRegistered` | consignment | notification, facility |
| `fleetpulse.consignment` | `ParcelCancelled` | consignment | notification, facility |
| `fleetpulse.facility` | `ParcelHubScanned` | facility | notification, dispatch |
| `fleetpulse.facility` | `TransitManifestCreated` | facility | dispatch |
| `fleetpulse.dispatch` | `ParcelOutForDelivery` | dispatch | notification |
| `fleetpulse.dispatch` | `ParcelDelivered` | dispatch | notification, consignment |
| `fleetpulse.dispatch` | `ParcelDeliveryFailed` | dispatch | notification, consignment |
| `fleetpulse.dispatch` | `GPSLocationUpdated` | dispatch | *(significant changes only — see §0.5)* |

### 1.3 Event envelope

EventBridge wraps your payload in its own structure. Your envelope lives inside `detail`:

```json
{
  "version": "0",
  "id": "7bf73129-1428-4cd3-a780-95db273d1602",
  "detail-type": "ParcelOutForDelivery",
  "source": "fleetpulse.dispatch",
  "account": "123456789012",
  "time": "2026-08-13T09:41:22Z",
  "region": "us-east-1",
  "resources": [],
  "detail": {
    "event_id": "01JQ8F2X9N4K7M3PQRSTVWXYZ0",
    "event_version": 1,
    "occurred_at": "2026-08-13T09:41:22.113Z",
    "producer": "dispatch-service",
    "idempotency_key": "WB1234567890:OUT_FOR_DELIVERY:attempt-1",
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "subject": { "waybill": "WB1234567890", "merchant_id": "MER-9931" },
    "data": {
      "from_state": "AT_HUB",
      "to_state": "OUT_FOR_DELIVERY",
      "hub_id": "HUB-BLR-01",
      "driver_id": "DRV-4417",
      "runsheet_id": "RS-20260813-BLR-07"
    }
  }
}
```

Three deliberate choices:

- **Keep your own `event_id`, not EventBridge's `id`.** EventBridge assigns a fresh `id` on redelivery;
  yours is stable and is what consumers dedupe on.
- **`traceparent` lives in `detail`, not a header.** EventBridge has no custom-header concept — this
  is the only place it can go, and it is what keeps [Blueprint §5.3](FleetPulse-Blueprint.md) tracing
  intact across the bus.
- **`occurred_at` is yours, distinct from EventBridge's `time`.** The causal guard in §0.4 depends on
  producer-side time, not bus-ingestion time.

### 1.4 Routing rules

```json
// Rule: dispatch-assignable — content-based filtering on a nested field.
// RabbitMQ topic exchanges cannot express this without a new routing key.
{
  "source": ["fleetpulse.facility"],
  "detail-type": ["ParcelHubScanned"],
  "detail": {
    "data": {
      "scan_type": ["INBOUND"],
      "hub_id": [{ "prefix": "HUB-BLR" }]
    }
  }
}
```

---

## 2. Local Sandbox with LocalStack

### 2.1 `infra/docker/docker-compose.yml`

```yaml
name: fleetpulse

x-svc: &svc
  restart: unless-stopped
  environment: &env
    # Point the AWS SDK at LocalStack. In AWS these are simply unset.
    AWS_ENDPOINT_URL: http://localstack:4566
    AWS_REGION: us-east-1
    AWS_ACCESS_KEY_ID: test
    AWS_SECRET_ACCESS_KEY: test
    EVENT_BUS_NAME: fleetpulse-event-bus
    LOG_LEVEL: debug
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  depends_on:
    postgres:   { condition: service_healthy }
    localstack: { condition: service_healthy }
    redis:      { condition: service_healthy }

services:
  localstack:
    image: localstack/localstack:3
    ports:
      - "4566:4566"
    environment:
      SERVICES: events,sqs,sts,iam,scheduler
      DEBUG: ${LOCALSTACK_DEBUG:-0}
      AWS_DEFAULT_REGION: us-east-1
      EAGER_SERVICE_LOADING: 1
      PERSISTENCE: 0                     # fresh bus on every boot; init script rebuilds it
    volumes:
      # Anything in ready.d runs once LocalStack reports healthy.
      - ./localstack/init-aws-local.sh:/etc/localstack/init/ready.d/init-aws-local.sh:ro
      - "/var/run/docker.sock:/var/run/docker.sock"
    healthcheck:
      test: ["CMD-SHELL", "awslocal sqs list-queues >/dev/null 2>&1 || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 20s

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports: ["5432:5432"]
    volumes:
      # Strip the UTF-8 BOM from this file or psql fails on line 1.
      - ./postgres-init:/docker-entrypoint-initdb.d:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s

  consignment-service:
    <<: *svc
    build: ../../services/consignment-service
    environment:
      <<: *env
      OTEL_SERVICE_NAME: consignment-service
      EVENT_SOURCE: fleetpulse.consignment
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/consignment_db?sslmode=disable
    ports: ["8081:8080"]

  facility-service:
    <<: *svc
    build: ../../services/facility-service
    environment:
      <<: *env
      OTEL_SERVICE_NAME: facility-service
      EVENT_SOURCE: fleetpulse.facility
      SQS_QUEUE_URL: http://localstack:4566/000000000000/fleetpulse-facility-inbound
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/facility_db?sslmode=disable
    ports: ["8082:8080"]

  dispatch-service:
    <<: *svc
    build: ../../services/dispatch-service
    environment:
      <<: *env
      OTEL_SERVICE_NAME: dispatch-service
      EVENT_SOURCE: fleetpulse.dispatch
      SQS_QUEUE_URL: http://localstack:4566/000000000000/fleetpulse-dispatch-assignable
      REDIS_ADDR: redis:6379
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/dispatch_db?sslmode=disable
    ports: ["8083:8080"]

  notification-service:
    <<: *svc
    build: ../../services/notification-service
    environment:
      <<: *env
      OTEL_SERVICE_NAME: notification-service
      SQS_QUEUE_URL: http://localstack:4566/000000000000/fleetpulse-notification-events
      REDIS_ADDR: redis:6379
      WEBHOOK_SINK_URL: http://webhook-sink:8080/hook
    ports: ["8084:8080"]

  webhook-sink:
    image: mendhak/http-https-echo:34
    environment: { HTTP_PORT: 8080 }
    ports: ["8090:8080"]

volumes: { pgdata: {} }
```

### 2.2 `infra/docker/localstack/init-aws-local.sh`

```bash
#!/bin/bash
# infra/docker/localstack/init-aws-local.sh
#
# Runs automatically inside the LocalStack container once it reports healthy
# (mounted into /etc/localstack/init/ready.d/). Creates the event bus, the SQS
# queues + DLQs, the queue policies, and the routing rules.
#
# Mirror any change here into infra/terraform/modules/eventbus/ so local and
# AWS stay in step — divergence between the two is the main risk of this setup.
set -euo pipefail

BUS="fleetpulse-event-bus"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT="000000000000"        # LocalStack's fixed account id

echo "==> Creating event bus: ${BUS}"
awslocal events create-event-bus --name "${BUS}" >/dev/null

# ---------------------------------------------------------------------------
# create_queue <name>  ->  main queue + DLQ, redrive after 5 failures
# ---------------------------------------------------------------------------
create_queue() {
  local name="$1"
  local dlq="${name}-dlq"

  echo "==> Creating queue: ${name} (+ ${dlq})"
  awslocal sqs create-queue --queue-name "${dlq}" >/dev/null

  local dlq_arn
  dlq_arn=$(awslocal sqs get-queue-attributes \
    --queue-url "http://localhost:4566/${ACCOUNT}/${dlq}" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

  awslocal sqs create-queue --queue-name "${name}" --attributes "$(cat <<JSON
{
  "VisibilityTimeout": "60",
  "MessageRetentionPeriod": "345600",
  "ReceiveMessageWaitTimeSeconds": "20",
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"${dlq_arn}\",\"maxReceiveCount\":\"5\"}"
}
JSON
)" >/dev/null

  # EventBridge cannot deliver to SQS without an explicit resource policy.
  # This is the #1 reason "the rule matches but nothing arrives".
  local q_arn
  q_arn=$(awslocal sqs get-queue-attributes \
    --queue-url "http://localhost:4566/${ACCOUNT}/${name}" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

  awslocal sqs set-queue-attributes \
    --queue-url "http://localhost:4566/${ACCOUNT}/${name}" \
    --attributes "$(cat <<JSON
{
  "Policy": "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"events.amazonaws.com\"},
      \"Action\":\"sqs:SendMessage\",
      \"Resource\":\"${q_arn}\"
    }]
  }"
}
JSON
)" >/dev/null
}

create_queue "fleetpulse-notification-events"
create_queue "fleetpulse-facility-inbound"
create_queue "fleetpulse-dispatch-assignable"

# ---------------------------------------------------------------------------
# add_rule <rule-name> <pattern-json> <target-queue>
# ---------------------------------------------------------------------------
add_rule() {
  local rule="$1" pattern="$2" queue="$3"

  echo "==> Rule: ${rule} -> ${queue}"
  awslocal events put-rule \
    --name "${rule}" \
    --event-bus-name "${BUS}" \
    --event-pattern "${pattern}" \
    --state ENABLED >/dev/null

  local q_arn
  q_arn=$(awslocal sqs get-queue-attributes \
    --queue-url "http://localhost:4566/${ACCOUNT}/${queue}" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

  awslocal events put-targets \
    --rule "${rule}" \
    --event-bus-name "${BUS}" \
    --targets "Id=1,Arn=${q_arn}" >/dev/null
}

add_rule "notification-all-parcel-events" '{
  "source": ["fleetpulse.consignment", "fleetpulse.facility", "fleetpulse.dispatch"],
  "detail-type": [
    "ParcelRegistered", "ParcelCancelled", "ParcelHubScanned",
    "ParcelOutForDelivery", "ParcelDelivered", "ParcelDeliveryFailed"
  ]
}' "fleetpulse-notification-events"

add_rule "facility-inbound-expected" '{
  "source": ["fleetpulse.consignment"],
  "detail-type": ["ParcelRegistered"]
}' "fleetpulse-facility-inbound"

add_rule "dispatch-assignable" '{
  "source": ["fleetpulse.facility"],
  "detail-type": ["ParcelHubScanned"],
  "detail": { "data": { "scan_type": ["INBOUND"] } }
}' "fleetpulse-dispatch-assignable"

echo "==> LocalStack ready."
awslocal events list-rules --event-bus-name "${BUS}" \
  --query 'Rules[].Name' --output table
```

Make it executable — a non-executable init script fails silently, which is a frustrating half-hour:

```bash
chmod +x infra/docker/localstack/init-aws-local.sh
git update-index --chmod=+x infra/docker/localstack/init-aws-local.sh   # Windows/WSL
```

### 2.3 Verifying locally

```bash
docker compose up -d
docker compose logs -f localstack | grep "LocalStack ready"

alias awslocal='aws --endpoint-url=http://localhost:4566 --region us-east-1'

awslocal events list-rules --event-bus-name fleetpulse-event-bus

# Publish a test event by hand
awslocal events put-events --entries '[{
  "EventBusName": "fleetpulse-event-bus",
  "Source": "fleetpulse.consignment",
  "DetailType": "ParcelRegistered",
  "Detail": "{\"event_id\":\"TEST-1\",\"subject\":{\"waybill\":\"WB-TEST\"},\"data\":{\"to_state\":\"BOOKED\"}}"
}'

# It should land in the notification queue
awslocal sqs receive-message \
  --queue-url http://localhost:4566/000000000000/fleetpulse-notification-events \
  --wait-time-seconds 5
```

> **Debugging tip:** `FailedEntryCount: 0` from `put-events` means only that EventBridge *accepted*
> the event — **not** that any rule matched it. An event matching zero rules is silently discarded
> and looks identical to success. If nothing arrives, check the rule pattern first, then the SQS
> resource policy.

---

## 2A. Practising EventBridge locally

LocalStack gives you an unlimited, free EventBridge to make mistakes in. This section is a **lab
curriculum** — eight exercises, in order, each isolating one concept. Work through them before
touching real AWS and you will arrive already knowing the failure modes.

Everything below runs against `http://localhost:4566` and costs nothing.

```bash
# Put this in your shell profile — every command in this section assumes it.
alias awslocal='aws --endpoint-url=http://localhost:4566 --region us-east-1'
export BUS=fleetpulse-event-bus
export Q=http://localhost:4566/000000000000/fleetpulse-notification-events
```

### 2A.0 First, build the two tools you will use constantly

**A debug catch-all queue.** The hardest thing about EventBridge is that unmatched events vanish
silently. Fix that by adding one rule that matches *everything* from your app and dumps it to a
queue you can inspect. Add this to `init-aws-local.sh`:

```bash
# ---- DEBUG: catch-all so you can always see what was actually published ----
# Matches any fleetpulse.* source regardless of detail-type.
create_queue "fleetpulse-debug-all"
add_rule "debug-catch-all" '{
  "source": [{ "prefix": "fleetpulse." }]
}' "fleetpulse-debug-all"
```

Now "did my event reach the bus at all?" and "did my rule match?" become two separate, answerable
questions. If it is in `fleetpulse-debug-all` but not in your target queue, the bus is fine and your
**pattern** is wrong. That one distinction will save you hours.

**A queue-watching helper.** Save as `scripts/watch-queue.sh`:

```bash
#!/usr/bin/env bash
# Usage: ./scripts/watch-queue.sh fleetpulse-notification-events
set -euo pipefail
QUEUE_URL="http://localhost:4566/000000000000/${1:?queue name required}"
echo "Watching ${1} — Ctrl+C to stop"
while true; do
  MSGS=$(aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sqs receive-message --queue-url "$QUEUE_URL" \
    --max-number-of-messages 10 --wait-time-seconds 20 \
    --query 'Messages[].Body' --output json 2>/dev/null || echo '[]')

  if [ "$MSGS" != "[]" ] && [ -n "$MSGS" ]; then
    # Print just detail-type + waybill so the output stays readable.
    echo "$MSGS" | jq -r '.[] | fromjson
      | "\(.["detail-type"])  \(.detail.subject.waybill // "-")"'
  fi
done
```

### 2A.1 Exercise 1 — Prove the round trip

Goal: one event, published by hand, arriving in a queue.

```bash
docker compose up -d localstack
docker compose logs localstack | grep "LocalStack ready"

awslocal events list-rules --event-bus-name $BUS --output table

awslocal events put-events --entries '[{
  "EventBusName": "fleetpulse-event-bus",
  "Source": "fleetpulse.consignment",
  "DetailType": "ParcelRegistered",
  "Detail": "{\"event_id\":\"EX1\",\"subject\":{\"waybill\":\"WB-EX1\"},\"data\":{\"to_state\":\"BOOKED\"}}"
}'

awslocal sqs receive-message --queue-url $Q --wait-time-seconds 5
```

✅ **You have learned it when:** you can explain what each of `EventBusName`, `Source`,
`DetailType`, and `Detail` is for, and why `Detail` is a *string* containing JSON rather than a JSON
object.

### 2A.2 Exercise 2 — Event patterns (spend the most time here)

Pattern matching is where almost all EventBridge bugs live. There is a dedicated API for testing
patterns **without publishing anything**, and it is the most useful EventBridge command you will
learn:

```bash
awslocal events test-event-pattern \
  --event-pattern '{"source":["fleetpulse.facility"],"detail":{"data":{"scan_type":["INBOUND"]}}}' \
  --event '{
    "id":"1","version":"0","account":"000000000000",
    "time":"2026-08-13T09:00:00Z","region":"us-east-1","resources":[],
    "source":"fleetpulse.facility",
    "detail-type":"ParcelHubScanned",
    "detail":{"data":{"scan_type":"INBOUND","hub_id":"HUB-BLR-01"}}
  }'
# → { "Result": true }
```

Work through these until each result is obvious *before* you run it:

| # | Pattern | Concept |
|---|---|---|
| 1 | `{"source":["fleetpulse.facility"]}` | Exact match |
| 2 | `{"detail-type":["ParcelDelivered","ParcelDeliveryFailed"]}` | A list is **OR** |
| 3 | `{"source":["fleetpulse.facility"],"detail-type":["ParcelHubScanned"]}` | Separate keys are **AND** |
| 4 | `{"detail":{"data":{"hub_id":[{"prefix":"HUB-BLR"}]}}}` | Prefix match |
| 5 | `{"detail":{"data":{"to_state":[{"anything-but":["DELIVERED"]}]}}}` | Negation |
| 6 | `{"detail":{"data":{"weight_grams":[{"numeric":[">",5000]}]}}}` | Numeric comparison |
| 7 | `{"detail":{"data":{"driver_id":[{"exists":true}]}}}` | Field presence |
| 8 | `{"detail":{"data":{"hub_id":[{"prefix":"HUB-BLR"},{"prefix":"HUB-MUM"}]}}}` | OR of matchers |

**The three rules that catch everyone:**

1. **Values are always arrays.** `{"source":"fleetpulse.facility"}` is invalid — it must be
   `{"source":["fleetpulse.facility"]}`. Even for one value.
2. **Nesting must mirror the event exactly.** Your envelope puts business fields under
   `detail.data.*`, so the pattern needs `{"detail":{"data":{...}}}`. Writing `{"detail":{"scan_type":...}}`
   silently matches nothing.
3. **Omitted fields are wildcards.** A pattern that does not mention `detail-type` matches *every*
   detail-type. This is how you accidentally fan an event out to consumers you did not intend.

✅ **You have learned it when:** you can predict `Result` for all eight patterns before running them,
and you reach for `test-event-pattern` automatically when a rule misbehaves.

### 2A.3 Exercise 3 — Fan-out

Add a second rule targeting a different queue with an overlapping pattern, publish once, and confirm
the event lands in **both** queues.

```bash
awslocal events put-rule --name "analytics-all-deliveries" --event-bus-name $BUS \
  --event-pattern '{"detail-type":["ParcelDelivered"]}'
# ...create a queue, set its policy, put-targets...

# Publish ONE ParcelDelivered → arrives in notification-events AND analytics
```

This is the property that makes an event bus different from an HTTP call: **the publisher does not
know or care how many consumers exist.** Adding the analytics consumer required zero changes to
`dispatch-service`.

✅ **You have learned it when:** you can articulate why this is impossible with the direct REST calls
in [FleetPulse-Architecture.md §4.4](FleetPulse-Architecture.md).

### 2A.4 Exercise 4 — Make the DLQ fire

Deliberately break a consumer and watch redrive work.

```bash
# 1. Stop the notification service so nothing consumes
docker compose stop notification-service

# 2. Publish an event
awslocal events put-events --entries '[{ ... "DetailType":"ParcelDelivered" ... }]'

# 3. Receive it 5 times WITHOUT deleting — simulating a handler that keeps failing.
#    maxReceiveCount is 5, so the 6th receive sends it to the DLQ.
for i in 1 2 3 4 5; do
  awslocal sqs receive-message --queue-url $Q --visibility-timeout 0 >/dev/null
  echo "receive attempt $i"
done

# 4. It should now be in the DLQ
awslocal sqs receive-message \
  --queue-url http://localhost:4566/000000000000/fleetpulse-notification-events-dlq
```

✅ **You have learned it when:** you can explain the difference between the **SQS redrive DLQ** (the
consumer could not *process* it) and the **EventBridge target DLQ** (the bus could not *deliver* it),
and why the Terraform in §4.1 configures both.

### 2A.5 Exercise 5 — Input transformers

Reshape an event before it reaches the target, so consumers do not need to know the envelope:

```bash
awslocal events put-targets --rule notification-all-parcel-events --event-bus-name $BUS \
  --targets '[{
    "Id": "1",
    "Arn": "arn:aws:sqs:us-east-1:000000000000:fleetpulse-notification-events",
    "InputTransformer": {
      "InputPathsMap": {
        "awb": "$.detail.subject.waybill",
        "state": "$.detail.data.to_state",
        "when": "$.time"
      },
      "InputTemplate": "{\"waybill\":\"<awb>\",\"status\":\"<state>\",\"at\":\"<when>\"}"
    }
  }]'
```

Useful to know exists — but **do not use it for FleetPulse.** Your consumers need `event_id` for
idempotency and `traceparent` for tracing, and a transformer that drops them would break both. Worth
one exercise, then revert.

### 2A.6 Exercise 6 — Ordering, the hard one

Prove to yourself that §0.2's ordering warning is real:

```bash
# Publish 10 events for the SAME waybill in strict sequence
for i in $(seq 1 10); do
  awslocal events put-events --entries "[{
    \"EventBusName\":\"$BUS\",\"Source\":\"fleetpulse.dispatch\",
    \"DetailType\":\"ParcelHubScanned\",
    \"Detail\":\"{\\\"event_id\\\":\\\"SEQ-$i\\\",\\\"seq\\\":$i,\\\"subject\\\":{\\\"waybill\\\":\\\"WB-ORDER\\\"}}\"
  }]"
done

# Drain the queue and print the sequence numbers in ARRIVAL order
awslocal sqs receive-message --queue-url $Q --max-number-of-messages 10 \
  --wait-time-seconds 5 --query 'Messages[].Body' --output json \
  | jq -r '.[] | fromjson | .detail.seq'
```

Run it several times. You will eventually see them out of order — and even when local ordering looks
fine, **real AWS makes no guarantee at all.** LocalStack being well-behaved here is misleading, which
is itself worth knowing.

✅ **You have learned it when:** you have implemented the causal guard from §0.4 and can explain why
an SQS FIFO target does not solve this.

### 2A.7 Exercise 7 — Run the real thing

```bash
docker compose up -d
./scripts/watch-queue.sh fleetpulse-debug-all &     # see everything
python -m fleetsim --parcels-per-min 20 --duration 2m
```

Then check the counts add up:

```bash
awslocal sqs get-queue-attributes --queue-url $Q \
  --attribute-names ApproximateNumberOfMessagesVisible \
                    ApproximateNumberOfMessagesNotVisible

# DLQ should be EMPTY. Anything here is a bug.
awslocal sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/fleetpulse-notification-events-dlq \
  --attribute-names ApproximateNumberOfMessagesVisible
```

✅ **You have learned it when:** 20 parcels/min for 2 minutes produces roughly `parcels × 8` events
in the debug queue, zero in any DLQ, and every parcel reaches a terminal state.

### 2A.8 Exercise 8 — The debugging drill

Break it on purpose, four ways, and practise diagnosing each. This is the exercise that pays off.

| Break | Symptom | How to diagnose |
|---|---|---|
| Typo the `Source` in the publisher | Publish succeeds, nothing arrives | Event **is** in `debug-all` (prefix match) but not the target → pattern problem. Confirm with `test-event-pattern` |
| Delete the SQS queue policy | Rule shows as matching, queue stays empty | `awslocal sqs get-queue-attributes --attribute-names Policy` → missing `events.amazonaws.com` |
| Change a rule to a non-matching pattern | Silent, total loss | `debug-all` has it, target does not. Nothing errors anywhere — this is the failure mode with **no signal** |
| Publish a `Detail` that is not valid JSON | `FailedEntryCount: 1` | Inspect `Entries[].ErrorCode` in the `put-events` response |

**The diagnostic order that works:**

```
1. Did it reach the bus?        → is it in fleetpulse-debug-all?
2. Did the pattern match?       → awslocal events test-event-pattern
3. Could the bus deliver?       → check the SQS queue policy
4. Did the consumer process it? → check the DLQ, then app logs
```

Nine times out of ten the answer is step 2.

### 2A.9 LocalStack limits you should verify, not assume

LocalStack **Community** covers the core EventBridge and SQS surface used above — buses, rules,
targets, patterns, queues, DLQs. Some higher-tier features (**Archive & Replay**, the **Schema
Registry** and discoverer) may require LocalStack Pro depending on your version.

Check rather than guess:

```bash
awslocal events create-archive --archive-name test-archive \
  --event-source-arn "arn:aws:events:us-east-1:000000000000:event-bus/$BUS"
# Works → practise replay locally.
# "not supported" / "Pro feature" → skip it locally and do that one exercise
# on real AWS, where a handful of archived events costs well under $0.01.
```

The Terraform in §4.1 provisions the archive and registry regardless — they are correct for AWS, and
Terraform is not the thing you are testing locally.

### 2A.10 Suggested schedule

| Session | Exercises | Time |
|---|---|---|
| 1 | Setup, catch-all queue, watch script, Ex. 1 | 1 hr |
| 2 | **Ex. 2 — patterns.** Do not rush this one | 2 hr |
| 3 | Ex. 3–4 (fan-out, DLQ) | 1.5 hr |
| 4 | Ex. 5–6 (transformers, ordering) + causal guard | 2 hr |
| 5 | Ex. 7–8 (full run, debugging drill) | 1.5 hr |

About 8 hours to genuine competence, at zero cost. Then your first real-AWS deploy is a
configuration change, not a learning exercise.

---

## 3. Service Integration

### 3.1 `pkg/events/eventbridge.go` — publisher

```go
// pkg/events/eventbridge.go
package events

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/eventbridge"
	"github.com/aws/aws-sdk-go-v2/service/eventbridge/types"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

const maxEntriesPerCall = 10 // hard EventBridge limit

type Publisher struct {
	client *eventbridge.Client
	bus    string
	source string
}

func NewPublisher(ctx context.Context, bus, source string) (*Publisher, error) {
	// AWS_ENDPOINT_URL is honoured natively by SDK v2 (>= 2023). Setting it to
	// LocalStack requires no code branch; in AWS the variable is simply unset.
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		return nil, fmt.Errorf("load aws config: %w", err)
	}
	return &Publisher{client: eventbridge.NewFromConfig(cfg), bus: bus, source: source}, nil
}

// Publish sends up to len(evs) events, batching at the 10-entry API limit.
// Returns the events that FAILED so the outbox relay can leave them unpublished.
func (p *Publisher) Publish(ctx context.Context, evs []Envelope) ([]Envelope, error) {
	var failed []Envelope

	for start := 0; start < len(evs); start += maxEntriesPerCall {
		end := min(start+maxEntriesPerCall, len(evs))
		chunk := evs[start:end]

		entries := make([]types.PutEventsRequestEntry, 0, len(chunk))
		for _, ev := range chunk {
			// Inject W3C trace context into the payload. EventBridge has no
			// custom headers, so `detail` is the only carrier available.
			ev.TraceParent = traceParentFrom(ctx)

			detail, err := json.Marshal(ev)
			if err != nil {
				return nil, fmt.Errorf("marshal %s: %w", ev.EventID, err)
			}
			// 256 KB per entry. Oversized events must be claim-checked to S3.
			if len(detail) > 256*1024 {
				return nil, fmt.Errorf("event %s exceeds 256KB", ev.EventID)
			}

			entries = append(entries, types.PutEventsRequestEntry{
				EventBusName: aws.String(p.bus),
				Source:       aws.String(p.source),
				DetailType:   aws.String(ev.DetailType),
				Detail:       aws.String(string(detail)),
				Time:         aws.Time(ev.OccurredAt),
			})
		}

		out, err := p.client.PutEvents(ctx, &eventbridge.PutEventsInput{Entries: entries})
		if err != nil {
			failed = append(failed, chunk...) // whole chunk unpublished
			continue
		}

		// ⚠️ PutEvents returns HTTP 200 on PARTIAL failure. Ignoring
		// FailedEntryCount silently drops events — the classic EventBridge bug.
		if out.FailedEntryCount != nil && *out.FailedEntryCount > 0 {
			for i, res := range out.Entries {
				if res.ErrorCode != nil {
					failed = append(failed, chunk[i])
				}
			}
		}
	}
	return failed, nil
}

func traceParentFrom(ctx context.Context) string {
	sc := trace.SpanContextFromContext(ctx)
	if !sc.IsValid() {
		return ""
	}
	flags := "00"
	if sc.IsSampled() {
		flags = "01"
	}
	return fmt.Sprintf("00-%s-%s-%s", sc.TraceID(), sc.SpanID(), flags)
}
```

### 3.2 Outbox relay — the only change from the RabbitMQ version

```go
// services/consignment-service/internal/events/relay.go
func (r *Relay) tick(ctx context.Context) error {
	tx, err := r.db.Begin(ctx)
	if err != nil { return err }
	defer tx.Rollback(ctx)

	rows, err := tx.Query(ctx, `
		SELECT id, payload FROM outbox
		WHERE published_at IS NULL
		ORDER BY id
		LIMIT 100
		FOR UPDATE SKIP LOCKED`)
	if err != nil { return err }

	batch := collect(rows)
	if len(batch) == 0 { return tx.Commit(ctx) }

	// vvv the ONLY line that differs from the AMQP relay vvv
	failed, err := r.pub.Publish(ctx, envelopes(batch))
	if err != nil { return err }

	// Mark only what actually published. Failures stay NULL and retry next tick.
	published := diff(batch, failed)
	if _, err := tx.Exec(ctx,
		`UPDATE outbox SET published_at = now() WHERE id = ANY($1)`, ids(published)); err != nil {
		return err
	}
	metrics.OutboxPending.Set(float64(len(failed)))
	return tx.Commit(ctx)
}
```

Everything from [Blueprint §1.6](FleetPulse-Blueprint.md) — the shared transaction, the partial
index, `FOR UPDATE SKIP LOCKED` — is untouched.

### 3.3 `services/notification-service/internal/consumer/sqs.go` — Go consumer

```go
// services/notification-service/internal/consumer/sqs.go
package consumer

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/sqs/types"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// EventBridge wraps our envelope; SQS delivers that wrapper as the message body.
type eventBridgeMessage struct {
	ID         string          `json:"id"`
	DetailType string          `json:"detail-type"`
	Source     string          `json:"source"`
	Time       time.Time       `json:"time"`
	Detail     json.RawMessage `json:"detail"`
}

type Consumer struct {
	sqs      *sqs.Client
	queueURL string
	handler  Handler
	dedupe   Deduper // Redis SETNX
}

func (c *Consumer) Run(ctx context.Context) error {
	for {
		select {
		case <-ctx.Done():
			slog.Info("consumer draining")
			return nil
		default:
		}

		out, err := c.sqs.ReceiveMessage(ctx, &sqs.ReceiveMessageInput{
			QueueUrl:            aws.String(c.queueURL),
			MaxNumberOfMessages: 10,
			// 20s long polling: essential. Short polling burns your 1M/month
			// SQS free-tier request allowance in days.
			WaitTimeSeconds:   20,
			VisibilityTimeout: 60,
		})
		if err != nil {
			if errors.Is(err, context.Canceled) { return nil }
			slog.Error("receive failed", "err", err)
			time.Sleep(2 * time.Second)
			continue
		}

		for _, m := range out.Messages {
			c.processOne(ctx, m)
		}
	}
}

func (c *Consumer) processOne(ctx context.Context, m types.Message) {
	var ebm eventBridgeMessage
	if err := json.Unmarshal([]byte(*m.Body), &ebm); err != nil {
		// Unparseable: never retryable. Let redrive move it to the DLQ.
		slog.Error("malformed message", "err", err, "id", *m.MessageId)
		return
	}

	var env Envelope
	if err := json.Unmarshal(ebm.Detail, &env); err != nil {
		slog.Error("malformed detail", "err", err)
		return
	}

	// Re-attach the producer's trace so this span joins the parcel's journey
	// instead of starting a disconnected root.
	carrier := propagation.MapCarrier{"traceparent": env.TraceParent}
	ctx = propagation.TraceContext{}.Extract(ctx, carrier)

	ctx, span := otel.Tracer("notification").Start(ctx,
		"process "+ebm.DetailType, trace.WithSpanKind(trace.SpanKindConsumer))
	defer span.End()
	span.SetAttributes(
		attribute.String("fleetpulse.waybill", env.Subject.Waybill),
		attribute.String("fleetpulse.event_id", env.EventID),
		attribute.String("messaging.system", "aws_sqs"),
	)

	// Idempotency: EventBridge is at-least-once and WILL redeliver.
	fresh, err := c.dedupe.FirstSight(ctx, env.EventID, 24*time.Hour)
	if err != nil {
		span.RecordError(err)
		return // leave on queue; visibility timeout returns it
	}
	if !fresh {
		c.delete(ctx, m) // already handled — ack and move on
		return
	}

	if err := c.handler(ctx, env); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		// Do NOT delete: SQS redelivers, and after maxReceiveCount=5 it
		// lands in the DLQ automatically.
		return
	}

	c.delete(ctx, m)
}

func (c *Consumer) delete(ctx context.Context, m types.Message) {
	if _, err := c.sqs.DeleteMessage(ctx, &sqs.DeleteMessageInput{
		QueueUrl:      aws.String(c.queueURL),
		ReceiptHandle: m.ReceiptHandle,
	}); err != nil {
		slog.Error("delete failed", "err", err)
	}
}
```

### 3.4 Node.js consumer (same contract)

```javascript
// services/notification-service/src/consumer.js
import {
  SQSClient, ReceiveMessageCommand,
  DeleteMessageCommand, DeleteMessageBatchCommand,
} from "@aws-sdk/client-sqs";

// AWS_ENDPOINT_URL is honoured by SDK v3 — no LocalStack-specific branch.
const sqs = new SQSClient({});
const QUEUE_URL = process.env.SQS_QUEUE_URL;

export async function run({ handler, dedupe, signal }) {
  while (!signal.aborted) {
    let res;
    try {
      res = await sqs.send(new ReceiveMessageCommand({
        QueueUrl: QUEUE_URL,
        MaxNumberOfMessages: 10,
        WaitTimeSeconds: 20,       // long polling — protects the SQS free tier
        VisibilityTimeout: 60,
      }));
    } catch (err) {
      if (signal.aborted) break;
      console.error({ msg: "receive failed", err: err.message });
      await new Promise((r) => setTimeout(r, 2000));
      continue;
    }

    const toDelete = [];

    for (const m of res.Messages ?? []) {
      let env;
      try {
        env = JSON.parse(m.Body).detail;   // unwrap the EventBridge envelope
      } catch {
        console.error({ msg: "malformed body", id: m.MessageId });
        continue;                          // let redrive send it to the DLQ
      }

      try {
        if (!(await dedupe.firstSight(env.event_id, 86400))) {
          toDelete.push(m);                // duplicate — ack without reprocessing
          continue;
        }
        await handler(env);                // e.g. signed webhook POST
        toDelete.push(m);
      } catch (err) {
        // Leave it on the queue: SQS redelivers, DLQ catches it after 5 tries.
        console.error({ msg: "handler failed", waybill: env?.subject?.waybill, err: err.message });
      }
    }

    // Batch deletes: 10 messages = 1 billable request instead of 10.
    if (toDelete.length) {
      await sqs.send(new DeleteMessageBatchCommand({
        QueueUrl: QUEUE_URL,
        Entries: toDelete.map((m, i) => ({ Id: String(i), ReceiptHandle: m.ReceiptHandle })),
      }));
    }
  }
}
```

---

## 4. Terraform

### 4.1 `infra/terraform/modules/eventbus/main.tf`

```hcl
# infra/terraform/modules/eventbus/main.tf

resource "aws_cloudwatch_event_bus" "this" {
  name = var.bus_name          # "fleetpulse-event-bus"
  tags = var.tags
}

# ---------------------------------------------------------------------------
# Schema registry — discoverer auto-derives OpenAPI schemas from live traffic,
# and `aws schemas get-code-binding` then generates typed Go/TS structs.
# Registry + discoverer are free; you pay only for discovered schemas beyond
# the free allowance, which this project will not reach.
# ---------------------------------------------------------------------------
resource "aws_schemas_registry" "this" {
  name = "fleetpulse-schemas"
  tags = var.tags
}

resource "aws_schemas_discoverer" "this" {
  source_arn  = aws_cloudwatch_event_bus.this.arn
  description = "Auto-discover FleetPulse event schemas"
  tags        = var.tags
}

# ---------------------------------------------------------------------------
# Archive — the native replay capability. 30 days of retention on this volume
# is a few MB; storage is $0.10/GB-month.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_archive" "this" {
  name             = "${var.bus_name}-archive"
  event_source_arn = aws_cloudwatch_event_bus.this.arn
  retention_days   = var.archive_retention_days   # 30
  description      = "Replayable archive of all FleetPulse events"
}

# ---------------------------------------------------------------------------
# One SQS queue + DLQ per consumer, from var.consumers
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "dlq" {
  for_each                  = var.consumers
  name                      = "${var.bus_name}-${each.key}-dlq"
  message_retention_seconds = 1209600            # 14 days, the maximum
  tags                      = var.tags
}

resource "aws_sqs_queue" "main" {
  for_each                   = var.consumers
  name                       = "${var.bus_name}-${each.key}"
  visibility_timeout_seconds = 60                # >= your handler's worst case
  message_retention_seconds  = 345600            # 4 days
  receive_wait_time_seconds  = 20                # long polling, server side

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Resource policy — WITHOUT THIS, RULES MATCH BUT NOTHING IS DELIVERED.
# The SourceArn condition stops any other account's rule targeting your queue.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "queue" {
  for_each = var.consumers

  statement {
    sid     = "AllowEventBridgeDelivery"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main[each.key].arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.this[each.key].arn]
    }
  }
}

resource "aws_sqs_queue_policy" "main" {
  for_each  = var.consumers
  queue_url = aws_sqs_queue.main[each.key].id
  policy    = data.aws_iam_policy_document.queue[each.key].json
}

# ---------------------------------------------------------------------------
# Rules and targets
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "this" {
  for_each = var.consumers

  name           = "${var.bus_name}-${each.key}"
  event_bus_name = aws_cloudwatch_event_bus.this.name
  description    = each.value.description
  event_pattern  = jsonencode(each.value.event_pattern)
  state          = "ENABLED"
  tags           = var.tags
}

resource "aws_cloudwatch_event_target" "this" {
  for_each = var.consumers

  rule           = aws_cloudwatch_event_rule.this[each.key].name
  event_bus_name = aws_cloudwatch_event_bus.this.name
  target_id      = "${each.key}-sqs"
  arn            = aws_sqs_queue.main[each.key].arn

  # Target-level DLQ: catches events EventBridge could not DELIVER at all
  # (throttling, policy errors). Distinct from the SQS redrive DLQ, which
  # catches messages the CONSUMER could not process. You want both.
  dead_letter_config {
    arn = aws_sqs_queue.dlq[each.key].arn
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}

# ---------------------------------------------------------------------------
# Least-privilege publish policy for the services
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "publish" {
  statement {
    sid       = "PutEventsToFleetPulseBus"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.this.arn]   # this bus only, not "*"
  }
}

resource "aws_iam_policy" "publish" {
  name   = "${var.bus_name}-publish"
  policy = data.aws_iam_policy_document.publish.json
}
```

### 4.2 `infra/terraform/modules/eventbus/variables.tf`

```hcl
variable "bus_name" {
  type    = string
  default = "fleetpulse-event-bus"
}

variable "archive_retention_days" {
  type    = number
  default = 30
}

variable "consumers" {
  description = "Consumer name => rule description + EventBridge event pattern"
  type = map(object({
    description   = string
    event_pattern = any
  }))
}

variable "tags" {
  type    = map(string)
  default = {}
}
```

### 4.3 Wiring it up

```hcl
# infra/terraform/environments/free/main.tf
module "eventbus" {
  source   = "../../modules/eventbus"
  bus_name = "fleetpulse-event-bus"
  tags     = { Project = "fleetpulse", Env = "free" }

  consumers = {
    # Keep these patterns byte-identical to init-aws-local.sh (§2.2).
    notification-events = {
      description = "All parcel lifecycle events -> notification-service"
      event_pattern = {
        source = ["fleetpulse.consignment", "fleetpulse.facility", "fleetpulse.dispatch"]
        "detail-type" = [
          "ParcelRegistered", "ParcelCancelled", "ParcelHubScanned",
          "ParcelOutForDelivery", "ParcelDelivered", "ParcelDeliveryFailed",
        ]
      }
    }

    facility-inbound = {
      description = "New bookings -> facility-service expects inbound"
      event_pattern = {
        source        = ["fleetpulse.consignment"]
        "detail-type" = ["ParcelRegistered"]
      }
    }

    dispatch-assignable = {
      description = "Inbound hub scans -> dispatch-service can assign"
      event_pattern = {
        source        = ["fleetpulse.facility"]
        "detail-type" = ["ParcelHubScanned"]
        detail        = { data = { scan_type = ["INBOUND"] } }
      }
    }
  }
}
```

### 4.4 Free-tier posture

| Resource | Charge | At FleetPulse volume |
|---|---|---|
| `aws_cloudwatch_event_bus` | Free to exist | $0.00 |
| `PutEvents` (custom source) | **$1.00/M — no free tier** | ~$0.24/mo (§0.5) |
| Rules, targets, pattern matching | Free | $0.00 |
| SQS requests | 1M/month free **forever** | $0.00 with long polling |
| Archive storage | $0.10/GB-month | < $0.01 |
| Replay | $1.00/M events replayed | Only when you run one |
| Schema registry + discoverer | Free | $0.00 |

**Expected total: under $0.50/month** — provided GPS telemetry stays off the bus.

### 4.5 Replaying events

```bash
# Reprocess a window of history — e.g. after fixing a notification bug.
aws events start-replay \
  --replay-name "notification-backfill-$(date +%s)" \
  --event-source-arn "$(terraform output -raw archive_arn)" \
  --event-start-time  2026-08-01T00:00:00Z \
  --event-end-time    2026-08-13T00:00:00Z \
  --destination '{
    "Arn": "'"$(terraform output -raw bus_arn)"'",
    "FilterArns": ["'"$(terraform output -raw notification_rule_arn)"'"]
  }'
```

`FilterArns` restricts the replay to one rule — without it you re-fire *every* rule and every
consumer reprocesses everything. Your consumers are idempotent (§3.3), so this is survivable rather
than catastrophic, but it is still an expensive mistake.

---

## 5. Simulator Update

### 5.1 `simulators/fleetsim/publisher.py`

```python
# simulators/fleetsim/publisher.py
"""EventBridge publisher for the FleetPulse traffic simulator.

Targets LocalStack by default; set AWS_ENDPOINT_URL="" to hit real AWS.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config

MAX_ENTRIES = 10           # EventBridge hard limit per PutEvents call
MAX_DETAIL_BYTES = 256_000

ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.getenv("AWS_REGION", "us-east-1")
BUS = os.getenv("EVENT_BUS_NAME", "fleetpulse-event-bus")


@dataclass
class Event:
    source: str            # "fleetpulse.consignment"
    detail_type: str       # "ParcelRegistered"
    waybill: str
    merchant_id: str
    data: dict


class EventBridgePublisher:
    def __init__(self) -> None:
        self.client = boto3.client(
            "events",
            endpoint_url=ENDPOINT or None,
            region_name=REGION,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )
        self.sent = 0
        self.failed = 0

    def _entry(self, ev: Event) -> dict:
        detail = {
            "event_id": str(uuid.uuid4()),
            "event_version": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "producer": f"fleetsim/{ev.source}",
            "idempotency_key": f"{ev.waybill}:{ev.detail_type}",
            "traceparent": "",
            "subject": {"waybill": ev.waybill, "merchant_id": ev.merchant_id},
            "data": ev.data,
        }
        blob = json.dumps(detail, separators=(",", ":"))
        if len(blob.encode()) > MAX_DETAIL_BYTES:
            raise ValueError(f"detail too large for {ev.waybill}")

        return {
            "EventBusName": BUS,
            "Source": ev.source,
            "DetailType": ev.detail_type,
            "Detail": blob,
        }

    def publish(self, events: list[Event]) -> None:
        """Publish in batches of 10, checking per-entry failures."""
        for i in range(0, len(events), MAX_ENTRIES):
            chunk = events[i : i + MAX_ENTRIES]
            resp = self.client.put_events(Entries=[self._entry(e) for e in chunk])

            # ⚠️ put_events returns HTTP 200 even when individual entries fail.
            # Checking only the status code silently loses events.
            failed = resp.get("FailedEntryCount", 0)
            if failed:
                self.failed += failed
                for entry, result in zip(chunk, resp["Entries"]):
                    if "ErrorCode" in result:
                        print(
                            f"  ✗ {entry.detail_type} {entry.waybill}: "
                            f"{result['ErrorCode']} {result.get('ErrorMessage', '')}"
                        )
            self.sent += len(chunk) - failed
```

### 5.2 `simulators/fleetsim/scenarios.py` — lifecycle walker

```python
# simulators/fleetsim/scenarios.py (excerpt)
import asyncio, random
from .publisher import Event, EventBridgePublisher

async def run_parcel(pub: EventBridgePublisher, cfg, route: list[str]) -> None:
    """One parcel's full lifecycle, emitted onto EventBridge."""
    wb = f"WB{random.randint(10**9, 10**10 - 1)}"
    mer = f"MER-{random.randint(1000, 9999)}"

    pub.publish([Event("fleetpulse.consignment", "ParcelRegistered", wb, mer, {
        "from_state": None, "to_state": "BOOKED",
        "origin_hub": route[0], "destination_hub": route[-1],
        "service_type": random.choice(["EXPRESS", "SURFACE"]),
    })])

    await nap(random.uniform(1800, 7200), cfg)
    pub.publish([Event("fleetpulse.facility", "ParcelHubScanned", wb, mer, {
        "from_state": "BOOKED", "to_state": "PICKED_UP",
        "hub_id": route[0], "scan_type": "PICKUP",
    })])

    for i, hub in enumerate(route[1:], start=1):
        await nap(random.uniform(14400, 43200), cfg)
        pub.publish([Event("fleetpulse.facility", "ParcelHubScanned", wb, mer, {
            "from_state": "IN_TRANSIT", "to_state": "AT_HUB",
            "hub_id": hub, "scan_type": "INBOUND",   # matches the dispatch rule
        })])

    for attempt in range(1, 4):
        pub.publish([Event("fleetpulse.dispatch", "ParcelOutForDelivery", wb, mer, {
            "from_state": "AT_HUB", "to_state": "OUT_FOR_DELIVERY",
            "hub_id": route[-1], "driver_id": f"DRV-{random.randint(1000,9999)}",
            "attempt": attempt,
        })])

        # GPS goes to Redis, NOT the bus — see §0.5. At 300 pings/min the bus
        # would cost ~$13/mo for data that is stale in ten seconds.
        asyncio.create_task(emit_gps_to_redis(wb, route[-1], cfg))

        await nap(random.uniform(3600, 10800), cfg)
        if random.random() > cfg.ndr_rate:
            pub.publish([Event("fleetpulse.dispatch", "ParcelDelivered", wb, mer, {
                "from_state": "OUT_FOR_DELIVERY", "to_state": "DELIVERED",
                "pod_type": random.choice(["OTP", "SIGNATURE"]),
            })])
            return

        pub.publish([Event("fleetpulse.dispatch", "ParcelDeliveryFailed", wb, mer, {
            "from_state": "OUT_FOR_DELIVERY", "to_state": "DELIVERY_FAILED",
            "ndr_reason": random.choice(["CUSTOMER_UNAVAILABLE", "ADDRESS_INCORRECT"]),
            "attempt": attempt,
        })])


async def nap(sim_seconds: float, cfg) -> None:
    await asyncio.sleep(sim_seconds / cfg.time_compression)
```

### 5.3 `simulators/fleetsim/__main__.py`

```python
# simulators/fleetsim/__main__.py (excerpt)
import argparse, asyncio
from .publisher import EventBridgePublisher
from .scenarios import run_parcel, SimConfig
from .routes import random_route

async def main() -> None:
    ap = argparse.ArgumentParser(prog="fleetsim")
    ap.add_argument("--parcels-per-min", type=int, default=100)
    ap.add_argument("--duration", default="10m")
    ap.add_argument("--profile", default="steady",
                    choices=["steady", "spike", "hub-outage", "flaky-merchant"])
    args = ap.parse_args()

    cfg = SimConfig(parcels_per_min=args.parcels_per_min)
    pub = EventBridgePublisher()

    # Cost guard: at $1.00/M events, warn before an accidental big run.
    est = args.parcels_per_min * 8 * parse_minutes(args.duration)
    print(f"≈ {est:,} events  →  ~${est / 1_000_000:.4f} on real AWS "
          f"($0.00 on LocalStack)")

    tasks = [asyncio.create_task(run_parcel(pub, cfg, random_route()))
             for _ in range(cfg.parcels_per_min)]
    await asyncio.gather(*tasks)

    print(f"sent={pub.sent} failed={pub.failed}")

if __name__ == "__main__":
    asyncio.run(main())
```

```bash
# Local (LocalStack) — free
python -m fleetsim --parcels-per-min 100 --duration 10m

# Real AWS — costs money, prints the estimate first
AWS_ENDPOINT_URL="" AWS_REGION=us-east-1 python -m fleetsim --parcels-per-min 10 --duration 2m
```

---

## 6. Migration checklist

| # | Step | Verify |
|---|---|---|
| 1 | Add LocalStack + `init-aws-local.sh`; `chmod +x` it | `awslocal events list-rules` shows 3 rules |
| 2 | Add `pkg/events/eventbridge.go`; keep the `Publisher` interface | Unit test asserts `FailedEntryCount` handling |
| 3 | Point the outbox relay at `PutEvents` — one line (§3.2) | Stop LocalStack, book 10 parcels, restart → all 10 drain |
| 4 | Replace AMQP consumers with SQS pollers | Webhook fires end to end |
| 5 | **Add the causal guard (§0.4)** and `last_event_at` / `last_event_id` columns | Deliver events out of order; state stays correct |
| 6 | Move GPS off the bus to Redis + batch flush | `PutEvents` count stays ~8 per parcel |
| 7 | Delete RabbitMQ from Compose, Helm, and `.env.example` | No `RABBITMQ_*` left in the repo |
| 8 | `terraform apply` the eventbus module | Rules visible in the console; test event lands in SQS |
| 9 | Swap `rabbitmq_queue_messages_ready` for SQS metrics in dashboards/KEDA | KEDA scales on `ApproximateNumberOfMessagesVisible` |
| 10 | Update `CLAUDE.md` and Blueprint §0.1 | No stale "RabbitMQ is settled" text |

### Configuration changes

```ini
# .env.example — remove
-RABBITMQ_USER=fleetuser
-RABBITMQ_PASS=fleetpass

# .env.example — add
+AWS_ENDPOINT_URL=http://localstack:4566   # unset in real AWS
+AWS_REGION=us-east-1
+AWS_ACCESS_KEY_ID=test                     # LocalStack dummy creds
+AWS_SECRET_ACCESS_KEY=test
+EVENT_BUS_NAME=fleetpulse-event-bus
+EVENT_SOURCE=fleetpulse.consignment        # per service
+SQS_QUEUE_URL=
+SQS_MAX_MESSAGES=10
+SQS_WAIT_TIME_SECONDS=20
+SQS_VISIBILITY_TIMEOUT=60
```

### Observability changes

| Was (RabbitMQ) | Now (EventBridge + SQS) |
|---|---|
| `rabbitmq_queue_messages_ready` | `ApproximateNumberOfMessagesVisible` (CloudWatch) |
| `.dlq` depth alert | `ApproximateNumberOfMessagesVisible` on the DLQ **plus** the EventBridge `FailedInvocations` metric |
| KEDA `rabbitmq` trigger | KEDA `aws-sqs-queue` trigger |
| — *(new)* | EventBridge `TriggeredRules` — **flatlining at zero means a pattern stopped matching**, which is otherwise invisible |

That last one deserves an alert. A rule that stops matching produces no errors anywhere: publishers
succeed, no queue backs up, no DLQ fills. Events simply vanish. `TriggeredRules == 0` during active
hours is the only signal you get.
