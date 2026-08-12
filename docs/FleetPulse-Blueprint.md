# FleetPulse Blueprint

A production-shaped logistics platform built as a DevOps, cloud infrastructure, and observability
sandbox. The parcel domain is the vehicle; **operability is the product**.

This document is the design contract and the execution roadmap. It assumes the repository state
described in [CLAUDE.md](../CLAUDE.md): four scaffolded services, one PostgreSQL instance with a
database per service, RabbitMQ as the broker, and `infra/` split three ways.

---

## 0. Decisions that differ from the brief

Four places where I am deliberately not doing what the brief literally asked. Each is a decision
you can overturn, but overturn it knowingly.

### 0.1 Amazon MQ for RabbitMQ — not MSK

The brief lists "AWS MSK/ElastiCache" for the cloud queue tier. **MSK is Kafka.** The repo has
already settled on RabbitMQ (`RABBITMQ_USER`/`RABBITMQ_PASS` in `.env.example`), and swapping the
broker between local and production would destroy the single most valuable property of this
sandbox: that what you debug locally is what runs in the cloud. AMQP's push/ack/dead-letter model
and Kafka's pull/offset/log model are not interchangeable — the consumer code, the retry strategy,
the autoscaling signal, and the failure modes all differ.

**Decision:** RabbitMQ end-to-end. Local = the `rabbitmq:3-management` container. AWS = **Amazon MQ
for RabbitMQ** (`mq.t3.micro`, single-instance in dev; cluster deployment in prod).

**When to revisit:** if you want event *replay* — reprocessing three months of parcel history into a
new service — that is Kafka's native strength and RabbitMQ's weakness. Adding Kafka as a *second*
bus for the analytics/replay path is legitimate. Replacing RabbitMQ wholesale is not, unless you
are doing it specifically to learn Kafka.

> **⚠️ Superseded — see [FleetPulse-EventBridge.md](FleetPulse-EventBridge.md).** This section
> argued against switching brokers because local/prod parity is worth more than any broker's feature
> set. **LocalStack resolves that objection**: EventBridge runs locally at zero cost, so parity is
> preserved. EventBridge also provides native archive and replay — the one capability this section
> named as a legitimate reason to reconsider — without adopting Kafka.
>
> The EventBridge track is the current recommendation. It keeps the outbox pattern, idempotent
> consumers, and trace propagation unchanged, and it removes the broker from the 1 GB box in the
> [zero-cost track](FleetPulse-Zero-Cost.md) entirely. The costs are no ordering guarantee (which
> needs a causal guard in every consumer) and AWS lock-in. §0.2's telemetry/lifecycle split becomes
> *more* important there, not less — routing GPS through EventBridge would cost ~$13/month.

### 0.2 GPS telemetry does not go through the lifecycle bus

The brief groups "GPS pinging" with parcel state changes. They have opposite engineering
requirements:

| | Lifecycle events | GPS pings |
|---|---|---|
| Volume | ~1 per parcel per state (~8 total) | hundreds/min, continuous |
| Value of one message | High — a lost "Delivered" is a customer incident | Near zero — the next ping is 10s away |
| Durability need | Must not lose | Lossy is fine |
| Ordering need | Per-parcel ordering matters | Last-write-wins |

Putting them on one exchange means telemetry floods can delay or drop lifecycle events. **Decision:**
two exchanges — `fleetpulse.events` (durable, persistent, DLQ'd) and `fleetpulse.telemetry`
(non-persistent, lazy queue, bounded length with drop-head overflow). Last-known-position lands in
Redis; track history batch-flushes to Postgres every N seconds.

This split is also what makes the Phase 4 backpressure experiments interesting — you can saturate
telemetry and *prove* lifecycle delivery is unaffected.

### 0.3 Notification Service stays stateless — at a named cost

CLAUDE.md commits notification-service to statelessness with no database. That is achievable, but
webhook delivery genuinely needs three pieces of state, so be explicit about where each lives:

| State | Where it lives | Consequence |
|---|---|---|
| Merchant webhook config (URL, secret, retry policy) | Redis cache, source of truth in `consignment_db`, fetched over the API | Cold cache = one extra API hop |
| Retry scheduling | RabbitMQ delayed-retry queues (TTL + DLX ladder) | Retry state is in the broker, not queryable by merchant |
| Delivery dedupe | Redis `SETNX` on `event_id`, 24h TTL | Redis eviction under memory pressure = possible duplicate webhook |

**The cost:** there is no durable, queryable delivery audit trail. You cannot answer "show me every
webhook attempt for merchant X last Tuesday" from a database — only from logs and metrics.

**The trigger to revisit:** the first time you want per-merchant delivery SLA reporting, add
`notification_db` with a `delivery_attempts` table and a `CREATE DATABASE` line in
`01-init-databases.sql`. That is a deliberate upgrade, not a mistake.

### 0.4 SSM Session Manager instead of a bastion host

The brief asks for "Bastion/VPN access." A bastion EC2 instance means an open port 22, an SSH
keypair to manage and rotate, a host to patch, and an audit gap. **Decision:** AWS Systems Manager
Session Manager — no inbound ports, no keys, IAM-authorized, fully logged to CloudWatch/S3, and the
EC2 instance cost disappears. Port-forward to RDS through it:

```bash
aws ssm start-session --target $INSTANCE_ID \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["fleetpulse-dev.xxxx.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["5432"]}'
```

Learn this pattern. Bastion hosts are legacy in new AWS builds.

---

## 1. Logistics Microservices Architecture

### 1.1 Service boundaries

Boundaries follow **who owns the write**, not who reads. Every row has exactly one service allowed
to mutate it.

| Service | Owns (writes) | Database | Reads from others via |
|---|---|---|---|
| `consignment-service` | Waybills, merchants, addresses, labels, SLA promises | `consignment_db` | — (origin of truth for parcels) |
| `facility-service` | Hubs, scan records, bags, transit manifests | `facility_db` | Waybill details via consignment API (cached) |
| `dispatch-service` | Drivers, vehicles, runsheets, delivery attempts, GPS tracks | `dispatch_db` | Manifests via facility events |
| `notification-service` | Nothing durable | — (stateless) | Merchant config via consignment API + Redis |

The hard rule from CLAUDE.md: **no cross-database reads.** `facility-service` connecting to
`consignment_db` is the single easiest way to turn this into a distributed monolith. Cross-service
data moves over the API (synchronous, for reads you need *now*) or the event bus (asynchronous, for
everything else).

### 1.2 Tech stack

**Go 1.23+ for all four services.** Not polyglot — a single engineer running four services plus an
entire infrastructure stack does not need four toolchains. Reasons Go specifically:

- Static binary → `FROM scratch`/distroless images at 15–30 MB. Pod cold start under a second,
  which is what makes HPA and Karpenter scale-out *visible* rather than theoretical.
- 20–40 MB idle RSS → you can run the full stack on a kind cluster on a laptop, and on cheap
  `t4g.small` spot nodes in EKS.
- Best-in-class `prometheus/client_golang` and mature OpenTelemetry SDK.
- Goroutines make a realistic GPS ingest path (thousands of concurrent connections) trivial.

The `.gitignore` already anticipates both Go (`bin/`, `*.exe`) and Node (`node_modules/`, `dist/`).
Going Go means the Node entries are aspirational — leave them, the simulator and any future
dashboard tooling may use them.

| Concern | Choice | Why not the alternative |
|---|---|---|
| HTTP router | `chi` | Stdlib-compatible `http.Handler`; Gin's custom context leaks into your domain code |
| Postgres driver | `pgx/v5` | Native protocol, better perf and types than `lib/pq` |
| Query layer | `sqlc` | Generates typed Go from SQL. No ORM — you *want* to see the queries you're optimizing |
| Migrations | `goose` | Single binary, embeddable, runs as a Helm pre-install hook |
| AMQP | `amqp091-go` | Official successor to `streadway/amqp` |
| Redis | `go-redis/v9` | — |
| Config | `caarlos0/env` + struct tags | Keeps `.env.example` as the literal contract |
| Logging | `log/slog` (stdlib), JSON handler | Structured from day one; trace IDs as attributes |
| Testing | stdlib `testing` + `testcontainers-go` | Real Postgres and RabbitMQ in integration tests, no mocks-of-the-database |
| Lint | `golangci-lint` | — |

**Data stores by role:**

- **PostgreSQL 16** — one instance, three databases. Source of truth. Local: container. AWS: RDS.
- **Redis 7 / Valkey** — last-known GPS position (`HSET vehicle:{id}`), waybill status hot cache,
  webhook dedupe keys, per-merchant rate limiting. Local: container. AWS: ElastiCache.
- **RabbitMQ 3.13** — two exchanges, per-consumer queues, DLX ladder. Local: container. AWS: Amazon MQ.

### 1.3 The parcel state machine

Every lifecycle event is a transition in this machine. Illegal transitions are rejected at the
service boundary and counted as a metric — `fleetpulse_illegal_transition_total` is an excellent
early bug detector.

```mermaid
stateDiagram-v2
    [*] --> BOOKED: merchant creates waybill
    BOOKED --> PICKED_UP: pickup scan
    BOOKED --> CANCELLED: merchant cancels
    PICKED_UP --> IN_TRANSIT: outbound hub scan
    IN_TRANSIT --> AT_HUB: inbound hub scan
    AT_HUB --> IN_TRANSIT: onward manifest
    AT_HUB --> OUT_FOR_DELIVERY: assigned to runsheet
    OUT_FOR_DELIVERY --> DELIVERED: POD captured
    OUT_FOR_DELIVERY --> DELIVERY_FAILED: NDR reason code
    DELIVERY_FAILED --> OUT_FOR_DELIVERY: reattempt (max 3)
    DELIVERY_FAILED --> RTO_INITIATED: attempts exhausted
    RTO_INITIATED --> RTO_DELIVERED: returned to merchant
    DELIVERED --> [*]
    RTO_DELIVERED --> [*]
    CANCELLED --> [*]
```

`AT_HUB → IN_TRANSIT` is a self-loop across multiple hubs — a parcel from Bengaluru to Guwahati
passes through three or four. That loop is what makes the dwell-time histogram interesting.

### 1.4 Event envelope

One envelope for every message on both exchanges. Versioned from the first commit, because the
first schema change always arrives sooner than expected.

```json
{
  "event_id": "01JQ8F2X9N4K7M3PQRSTVWXYZ0",
  "event_type": "parcel.out_for_delivery",
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
    "runsheet_id": "RS-20260813-BLR-07",
    "expected_delivery_by": "2026-08-13T18:00:00Z"
  }
}
```

Three fields carry disproportionate weight:

- **`event_id`** (ULID — sortable by time) is the dedupe key. Consumers `SETNX event:{id}` in Redis
  with 24h TTL before processing.
- **`idempotency_key`** is the *business* dedupe key. Two different `event_id`s describing the same
  real-world transition must collide here.
- **`traceparent`** is the W3C trace context. **This is the field that makes distributed tracing
  work across the broker.** Without it, every async hop starts a new, disconnected trace and Phase 4
  produces four unrelated trace fragments instead of one parcel lifecycle. See §5.3.

### 1.5 Exchange and queue topology

```mermaid
flowchart LR
    CS[consignment-service] -->|parcel.booked<br/>parcel.cancelled| EX{{fleetpulse.events<br/>topic}}
    FS[facility-service] -->|parcel.picked_up<br/>parcel.in_transit<br/>parcel.at_hub| EX
    DS[dispatch-service] -->|parcel.out_for_delivery<br/>parcel.delivered<br/>parcel.delivery_failed| EX

    EX -->|parcel.*| NQ[notification.parcel-events]
    EX -->|parcel.booked| FQ[facility.inbound-expected]
    EX -->|parcel.at_hub| DQ[dispatch.assignable]

    NQ --> NS[notification-service]
    FQ --> FS
    DQ --> DS

    NS -.->|nack after N| DLX{{fleetpulse.dlx}}
    DLX --> DLQ[(notification.parcel-events.dlq)]

    DS2[dispatch-service] -->|gps.ping| TEX{{fleetpulse.telemetry<br/>topic, non-persistent}}
    TEX --> TQ[telemetry.gps<br/>lazy, max-length 100k<br/>overflow: drop-head]
    TQ --> TC[telemetry consumer]
    TC --> RD[(Redis<br/>last position)]
    TC -.->|batch every 5s| PG[(dispatch_db<br/>gps_tracks)]
```

**Retry ladder** — the standard RabbitMQ pattern, worth building by hand once:

`notification.parcel-events` → nack → `fleetpulse.dlx` → `notification.retry.30s`
(`x-message-ttl: 30000`, `x-dead-letter-exchange: fleetpulse.events`) → back to the main queue.
Three rungs (30s / 5m / 30m) via an `x-death` count header check, then terminal `.dlq`.

Alerting on `.dlq` depth > 0 is one of your highest-signal alerts — it means a message failed every
retry, which is always either a bug or a genuinely dead downstream.

### 1.6 The transactional outbox — the most important pattern here

A service that writes to Postgres and *then* publishes to RabbitMQ has a dual-write problem: the
process can die between the two. The parcel is `DELIVERED` in the database and the customer never
gets told. This is the defining correctness bug of event-driven systems, and this project should
solve it properly rather than pretend it away.

```sql
-- goose migration, in every service that publishes
CREATE TABLE outbox (
    id            BIGSERIAL PRIMARY KEY,
    event_id      TEXT        NOT NULL UNIQUE,
    exchange      TEXT        NOT NULL,
    routing_key   TEXT        NOT NULL,
    payload       JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ,
    attempts      INT         NOT NULL DEFAULT 0
);

-- Partial index: the relay only ever scans unpublished rows, so the index
-- stays small no matter how large the table grows.
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
```

The state change and the event insert share one transaction:

```go
func (s *Service) MarkDelivered(ctx context.Context, waybill string, pod POD) error {
	return pgx.BeginTxFunc(ctx, s.db, pgx.TxOptions{}, func(tx pgx.Tx) error {
		if err := s.q.WithTx(tx).UpdateParcelState(ctx, waybill, "DELIVERED"); err != nil {
			return err
		}
		ev := events.New("parcel.delivered", waybill, pod)
		// Same tx: either both land or neither does.
		return s.q.WithTx(tx).InsertOutbox(ctx, ev.ToOutboxRow())
	})
}
```

A relay goroutine drains it. `FOR UPDATE SKIP LOCKED` lets multiple replicas relay concurrently
without double-publishing:

```go
func (r *Relay) tick(ctx context.Context) error {
	tx, err := r.db.Begin(ctx)
	if err != nil { return err }
	defer tx.Rollback(ctx)

	rows, err := tx.Query(ctx, `
		SELECT id, exchange, routing_key, payload FROM outbox
		WHERE published_at IS NULL
		ORDER BY id
		LIMIT 100
		FOR UPDATE SKIP LOCKED`)
	if err != nil { return err }

	batch := collect(rows)
	for _, m := range batch {
		// Publisher confirms ON. A publish that is not confirmed is not published.
		if err := r.pub.PublishConfirmed(ctx, m); err != nil {
			return err // tx rolls back, row stays unpublished, retried next tick
		}
	}
	_, err = tx.Exec(ctx, `UPDATE outbox SET published_at = now() WHERE id = ANY($1)`, ids(batch))
	if err != nil { return err }
	return tx.Commit(ctx)
}
```

**Consequence for delivery semantics:** this is *at-least-once*. A crash after publish-confirm but
before the `UPDATE` republishes the message. That is exactly why every consumer must be idempotent
on `event_id`. At-least-once delivery plus idempotent consumers equals effectively-once processing;
there is no simpler correct answer.

**Expose `fleetpulse_outbox_pending` and `fleetpulse_outbox_oldest_age_seconds` as gauges.** A
growing outbox means the relay is stuck and events are silently not flowing while every HTTP
endpoint still returns 200. It is the highest-value custom metric in the system.

---

## 2. Phase 1 — Local Development & Sandbox

### 2.1 Repository layout

```
fleetpulse/
├── CLAUDE.md
├── .env.example                      # the config contract
├── docs/
│   └── FleetPulse-Blueprint.md       # this file
├── services/
│   ├── consignment-service/
│   │   ├── cmd/server/main.go
│   │   ├── internal/
│   │   │   ├── api/                  # chi handlers, DTOs
│   │   │   ├── domain/               # state machine, business rules — no I/O
│   │   │   ├── store/                # sqlc-generated + queries.sql
│   │   │   ├── events/               # envelope, publisher, outbox relay
│   │   │   └── telemetry/            # metrics, tracer, slog setup
│   │   ├── migrations/               # goose .sql files
│   │   ├── Dockerfile
│   │   └── go.mod
│   ├── facility-service/             # same shape
│   ├── dispatch-service/             # same shape
│   └── notification-service/         # same shape, no store/ or migrations/
├── pkg/                              # shared Go module: envelope, otel bootstrap, amqp helpers
├── simulators/
│   ├── pyproject.toml
│   ├── fleetsim/
│   │   ├── __main__.py
│   │   ├── scenarios.py              # lifecycle walkers
│   │   ├── routes.py                 # hub graph + GPS interpolation
│   │   └── chaos.py                  # failure injection modes
│   └── README.md
├── infra/
│   ├── docker/
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.observability.yml
│   │   ├── postgres-init/01-init-databases.sql
│   │   ├── prometheus/{prometheus.yml,rules/}
│   │   ├── grafana/provisioning/{datasources,dashboards}/
│   │   └── otel/collector-config.yaml
│   ├── helm/
│   │   ├── fleetpulse-common/        # library chart: deployment, svc, hpa, servicemonitor
│   │   └── fleetpulse/               # umbrella chart
│   │       ├── Chart.yaml
│   │       ├── values.yaml
│   │       ├── values-kind.yaml
│   │       ├── values-dev.yaml
│   │       └── charts/{consignment,facility,dispatch,notification}/
│   └── terraform/
│       ├── modules/{vpc,eks,rds,mq,elasticache,irsa,ecr,addons}/
│       └── environments/dev/{main.tf,backend.tf,variables.tf,terraform.tfvars}
├── deploy/                           # GitOps desired state — ArgoCD watches this
│   ├── argocd/{root-app.yaml,projects/}
│   └── envs/dev/{kustomization.yaml,images.yaml}
└── .github/workflows/{ci.yml,build-push.yml,promote.yml,terraform-plan.yml}
```

Two structural notes. **`pkg/` is a shared Go module**, not a copy-paste: the event envelope must be
identical across all four services or the dedupe logic silently diverges. **`deploy/` is separate
from `infra/helm/`** — Helm charts are the *templates* you author, `deploy/` is the *desired state*
ArgoCD reconciles. Conflating them is the most common GitOps mistake.

### 2.2 Docker Compose

Use **profiles** so day-one Compose stays light while the observability stack lives in the same file
set and comes up with one flag.

```yaml
# infra/docker/docker-compose.yml (excerpt)
x-service-defaults: &service-defaults
  restart: unless-stopped
  environment: &common-env
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    OTEL_SERVICE_NAME: ${COMPOSE_SERVICE_NAME:-unset}
    AMQP_URL: amqp://${RABBITMQ_USER}:${RABBITMQ_PASS}@rabbitmq:5672/
    REDIS_ADDR: redis:6379
    LOG_LEVEL: ${LOG_LEVEL:-info}
  depends_on:
    postgres:   { condition: service_healthy }
    rabbitmq:   { condition: service_healthy }
    redis:      { condition: service_healthy }

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      # NOTE: strip the UTF-8 BOM from this file or psql fails on line 1.
      - ./postgres-init:/docker-entrypoint-initdb.d:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      retries: 10

  rabbitmq:
    image: rabbitmq:3.13-management-alpine
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASS}
    ports: ["15672:15672"]            # management UI — you will live here
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 10s
      retries: 10

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s

  consignment-service:
    <<: *service-defaults
    build: ../../services/consignment-service
    environment:
      <<: *common-env
      OTEL_SERVICE_NAME: consignment-service
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/consignment_db?sslmode=disable
    ports: ["8081:8080"]

  # facility (8082), dispatch (8083), notification (8084) follow the same shape.

  webhook-sink:
    image: mendhak/http-https-echo:34
    environment: { HTTP_PORT: 8080 }
    ports: ["8090:8080"]

  prometheus:
    profiles: ["observability"]
    image: prom/prometheus:latest
    volumes: ["./prometheus:/etc/prometheus:ro"]
    ports: ["9090:9090"]

  grafana:
    profiles: ["observability"]
    image: grafana/grafana:latest
    volumes: ["./grafana/provisioning:/etc/grafana/provisioning:ro"]
    ports: ["3000:3000"]

  jaeger:
    profiles: ["observability"]
    image: jaegertracing/all-in-one:latest
    ports: ["16686:16686"]

  otel-collector:
    profiles: ["observability"]
    image: otel/opentelemetry-collector-contrib:latest
    command: ["--config=/etc/otel/collector-config.yaml"]
    volumes: ["./otel:/etc/otel:ro"]

volumes:
  pgdata:
```

```bash
docker compose up -d                              # app only
docker compose --profile observability up -d      # + Prometheus/Grafana/Jaeger
```

The `webhook-sink` container is the mock merchant endpoint. `mendhak/http-https-echo` reflects the
request back and can be told to return specific status codes — which is how you test the retry
ladder without writing a mock server.

**Dockerfile** — replace the alpine placeholders with a multi-stage distroless build:

```dockerfile
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/server /server
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]
```

### 2.3 `.env.example` evolution

CLAUDE.md makes `.env.example` the contract — new variables land there with safe placeholders in
the same change. The additions Phase 1 needs:

```ini
# existing
POSTGRES_USER=fleetadmin
POSTGRES_PASSWORD=fleetpassword
RABBITMQ_USER=fleetuser
RABBITMQ_PASS=fleetpass
AWS_REGION=us-east-1

# added in Phase 1
LOG_LEVEL=info
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=1.0
REDIS_ADDR=redis:6379
WEBHOOK_HMAC_SECRET=changeme-local-only
WEBHOOK_TIMEOUT_MS=3000
WEBHOOK_MAX_ATTEMPTS=4
OUTBOX_RELAY_INTERVAL_MS=200
OUTBOX_RELAY_BATCH_SIZE=100
GPS_FLUSH_INTERVAL_MS=5000
```

### 2.4 Traffic simulator

Python, `asyncio` + `httpx`. The design goal is **realistic shape**, not raw load — a load generator
that fires uniform random requests teaches you nothing about a logistics system. Each simulated
parcel is a coroutine walking the state machine over compressed time, and GPS pings interpolate
along a real hub-to-hub route.

```python
# simulators/fleetsim/scenarios.py (excerpt)
import asyncio, random
from dataclasses import dataclass

@dataclass
class SimConfig:
    parcels_per_min: int = 100
    gps_interval_s: float = 10.0
    time_compression: float = 120.0   # 1 sim-hour per 30 real-seconds
    ndr_rate: float = 0.12            # 12% first-attempt failure — realistic for India
    rto_rate: float = 0.03

async def run_parcel(client, cfg: SimConfig, route: list[str]):
    """One parcel's full lifecycle. Sleeps are wall-clock/compression."""
    wb = (await client.post("/v1/waybills", json=make_booking(route))).json()["waybill"]

    await nap(random.uniform(1800, 7200), cfg)                    # pickup lag
    await client.post(f"/v1/scans", json=scan(wb, route[0], "PICKUP"))

    for i, hub in enumerate(route[1:], start=1):                  # line-haul legs
        await nap(random.uniform(14400, 43200), cfg)
        await client.post("/v1/scans", json=scan(wb, hub, "INBOUND"))
        if i < len(route) - 1:
            await client.post("/v1/manifests", json=manifest(wb, hub, route[i + 1]))

    for attempt in range(1, 4):                                   # delivery attempts
        rs = await client.post("/v1/runsheets/assign", json={"waybill": wb})
        asyncio.create_task(emit_gps(client, cfg, rs.json()["vehicle_id"], route[-1]))
        await nap(random.uniform(3600, 10800), cfg)
        if random.random() > cfg.ndr_rate:
            await client.post("/v1/pod", json=pod(wb));  return
        await client.post("/v1/ndr", json=ndr(wb, attempt))
    await client.post("/v1/rto", json={"waybill": wb})

async def nap(sim_seconds: float, cfg: SimConfig):
    await asyncio.sleep(sim_seconds / cfg.time_compression)
```

```bash
python -m fleetsim --rps 100 --duration 10m --profile steady
python -m fleetsim --profile spike       # 10x burst for 90s — drives HPA/KEDA
python -m fleetsim --profile hub-outage  # one hub stops scanning — dwell-time alerts fire
python -m fleetsim --profile flaky-merchant --webhook-500-rate 0.4   # exercises the retry ladder
```

Those four profiles map directly onto the Phase 4 chaos experiments. Build the simulator with them
in mind and you get your load testing and your resilience testing from one tool.

### 2.5 Local Kubernetes — kind over minikube

**kind**, for three reasons that matter later: multi-node clusters in seconds (so pod anti-affinity
and topology spread are actually testable), `kind load docker-image` skips a registry round-trip,
and the node image is the same `kubeadm` path EKS conceptually follows.

```yaml
# infra/kind-cluster.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs: { node-labels: "ingress-ready=true" }
    extraPortMappings:
      - { containerPort: 80,  hostPort: 80 }
      - { containerPort: 443, hostPort: 443 }
  - role: worker
  - role: worker
```

**Helm structure: one library chart, four thin charts.** `fleetpulse-common` defines the templates
(Deployment, Service, ServiceAccount, HPA, ServiceMonitor, PDB, migration Job); each service chart
supplies values only. This is the difference between maintaining four sets of near-identical YAML
and maintaining one.

```yaml
# infra/helm/fleetpulse/charts/notification/values.yaml
fleetpulse-common:
  image: { repository: ghcr.io/OWNER/notification-service, tag: "" }  # tag from CI
  replicaCount: 2
  hasDatabase: false
  service: { port: 8080 }
  resources:
    requests: { cpu: 50m,  memory: 64Mi }
    limits:   { memory: 256Mi }          # NO cpu limit — see note
  autoscaling:
    kind: keda                            # queue-depth driven, not CPU
    queue: notification.parcel-events
    lagThreshold: 100
    minReplicas: 2
    maxReplicas: 20
  podDisruptionBudget: { minAvailable: 1 }
```

**No CPU limits, always memory limits.** CPU limits cause CFS throttling that shows up as
inexplicable p99 latency — the single most common self-inflicted Kubernetes performance bug. Memory
is incompressible, so a limit there is a real safety boundary. Set CPU *requests* accurately and let
bursting happen.

**Graceful shutdown is not optional for queue consumers.** On `SIGTERM`: stop accepting new
deliveries (`channel.Cancel`), finish in-flight messages, ack them, *then* close. Without this,
every rolling deploy nacks in-flight work into the retry ladder and you will see mysterious DLQ
growth on every release. Set `terminationGracePeriodSeconds: 45` and make `preStop` sleep ~5s so
endpoint removal propagates before the process starts draining.

---

## 3. Phase 2 — CI/CD & GitOps

### 3.1 Pipeline topology

```mermaid
flowchart LR
    PR[Pull Request] --> CI[ci.yml<br/>lint · unit · integration]
    CI --> RV{merge}
    RV -->|push to main| BP[build-push.yml<br/>buildx · trivy · cosign · GHCR]
    BP --> PROM[promote.yml<br/>commit new tag to deploy/envs/dev]
    PROM --> GIT[(git: deploy/)]
    GIT -.->|polls / webhook| ARGO[ArgoCD]
    ARGO -->|reconcile| K8S[EKS cluster]
    K8S -.->|health + sync status| ARGO
```

**The discipline that makes this GitOps and not just automation: CI never touches the cluster.** No
`kubectl apply`, no `helm upgrade`, no cluster credentials in GitHub Actions secrets. CI's terminal
output is a signed image and a git commit. ArgoCD is the only thing with write access to
Kubernetes. That single constraint gives you a complete audit trail, trivial rollback (`git revert`),
and no drift.

### 3.2 CI workflow

Path filtering matters immediately — four services in a monorepo should not all rebuild when one
changes.

```yaml
# .github/workflows/ci.yml
name: ci
on:
  pull_request:
  push: { branches: [main] }

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      services: ${{ steps.filter.outputs.changes }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            consignment:  ['services/consignment-service/**', 'pkg/**']
            facility:     ['services/facility-service/**',    'pkg/**']
            dispatch:     ['services/dispatch-service/**',     'pkg/**']
            notification: ['services/notification-service/**', 'pkg/**']

  test:
    needs: changes
    if: needs.changes.outputs.services != '[]'
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        service: ${{ fromJSON(needs.changes.outputs.services) }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.23'
          cache-dependency-path: services/${{ matrix.service }}-service/go.sum
      - uses: golangci/golangci-lint-action@v6
        with: { working-directory: services/${{ matrix.service }}-service }
      - name: unit + integration
        working-directory: services/${{ matrix.service }}-service
        # testcontainers-go spins real Postgres/RabbitMQ; Docker is present on ubuntu-latest.
        run: go test -race -covermode=atomic -coverprofile=cover.out ./...

  helm-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: helm dependency build infra/helm/fleetpulse
      - run: helm lint infra/helm/fleetpulse -f infra/helm/fleetpulse/values-dev.yaml
      - run: helm template infra/helm/fleetpulse | kubeconform -strict -summary -
```

`-race` on every test run is non-negotiable in Go with this much concurrency. It will find the
data race in your GPS batch-flusher before production does.

### 3.3 Build, scan, sign, push

```yaml
# .github/workflows/build-push.yml (job body, matrix over changed services)
permissions:
  contents: read
  packages: write
  id-token: write        # keyless cosign via GitHub OIDC — no key material to manage

steps:
  - uses: actions/checkout@v4
  - uses: docker/setup-buildx-action@v3
  - uses: docker/login-action@v3
    with:
      registry: ghcr.io
      username: ${{ github.actor }}
      password: ${{ secrets.GITHUB_TOKEN }}

  - uses: docker/build-push-action@v6
    id: build
    with:
      context: services/${{ matrix.service }}-service
      platforms: linux/amd64,linux/arm64      # arm64 → Graviton spot nodes, ~20% cheaper
      push: true
      tags: |
        ghcr.io/${{ github.repository_owner }}/${{ matrix.service }}-service:${{ github.sha }}
        ghcr.io/${{ github.repository_owner }}/${{ matrix.service }}-service:latest
      cache-from: type=gha
      cache-to: type=gha,mode=max
      provenance: true
      sbom: true

  - uses: aquasecurity/trivy-action@0.28.0
    with:
      image-ref: ghcr.io/${{ github.repository_owner }}/${{ matrix.service }}-service:${{ github.sha }}
      exit-code: '1'
      severity: 'CRITICAL,HIGH'
      ignore-unfixed: true

  - uses: sigstore/cosign-installer@v3
  - run: cosign sign --yes ghcr.io/${{ github.repository_owner }}/${{ matrix.service }}-service@${{ steps.build.outputs.digest }}
```

**Tag by git SHA, never deploy `latest`.** `latest` makes rollback ambiguous and breaks the
GitOps guarantee that the repo describes exactly what is running. Push `latest` for convenience,
deploy the SHA.

Later, close the loop with a Kyverno or OPA Gatekeeper policy in-cluster that rejects unsigned
images. That turns cosign from decoration into an enforced supply-chain control.

### 3.4 GitOps with ArgoCD

**ArgoCD over Flux** — for a single engineer learning the model, ArgoCD's UI makes reconciliation,
drift, and sync waves *visible*, which is worth a great deal pedagogically. Flux is arguably the
better pure-GitOps citizen; revisit once the concepts are second nature.

**Keep `deploy/` in this repo initially.** The textbook answer is a separate config repo (prevents
CI commits from re-triggering CI, cleaner RBAC). For one engineer the cross-repo token dance is
pure friction. Guard the loop with `paths-ignore: ['deploy/**']` on the CI triggers and split the
repo when a second person joins.

**App-of-apps**, so one Application bootstraps everything:

```yaml
# deploy/argocd/root-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: fleetpulse-root
  namespace: argocd
  finalizers: [resources-finalizer.argocd.argoproj.io]
spec:
  project: fleetpulse
  source:
    repoURL: https://github.com/OWNER/fleetpulse.git
    targetRevision: main
    path: deploy/envs/dev
  destination: { server: https://kubernetes.default.svc, namespace: fleetpulse }
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true, ServerSideApply=true]
    retry:
      limit: 5
      backoff: { duration: 15s, factor: 2, maxDuration: 5m }
```

`selfHeal: true` is where GitOps becomes real. Run `kubectl scale deploy/notification-service
--replicas=9` and watch ArgoCD revert it within seconds. Manual cluster changes stop being possible;
the only way to change production is a commit.

**Sync waves** order the rollout: wave `-2` namespaces and secrets, `-1` the goose migration Job
(as a `PreSync` hook so it completes before pods roll), `0` the Deployments, `1` the
ServiceMonitors and KEDA ScaledObjects.

**Image promotion** — CI commits the new tag. Explicit and auditable; you can read the deployment
history in `git log`:

```yaml
# .github/workflows/promote.yml (excerpt)
- run: |
    cd deploy/envs/dev
    kustomize edit set image \
      ghcr.io/${{ github.repository_owner }}/${{ matrix.service }}-service=ghcr.io/${{ github.repository_owner }}/${{ matrix.service }}-service:${{ github.sha }}
    git config user.name  "fleetpulse-ci"
    git config user.email "ci@fleetpulse.local"
    git commit -am "deploy(dev): ${{ matrix.service }} → ${{ github.sha }}"
    git push
```

ArgoCD Image Updater automates this with less YAML, but writing the commit yourself once makes the
git-as-source-of-truth loop concrete in a way the automated version does not.

---

## 4. Phase 3 — Infrastructure as Code on AWS

### 4.1 State backend

```hcl
# infra/terraform/environments/dev/backend.tf
terraform {
  required_version = ">= 1.11"
  backend "s3" {
    bucket       = "fleetpulse-tfstate-<your-account-id>"
    key          = "environments/dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true    # S3-native locking; DynamoDB lock table no longer needed
  }
}
```

`use_lockfile` (Terraform 1.11+) replaces the DynamoDB lock table with a conditional-write lock
object in S3 — one less resource, one less bill line. The bootstrap bucket is the one resource you
create by hand or in a tiny separate `bootstrap/` state.

### 4.2 Module composition

```hcl
# infra/terraform/environments/dev/main.tf (abridged)
locals {
  env  = "dev"
  name = "fleetpulse-${local.env}"
  tags = {
    Project     = "fleetpulse"
    Environment = local.env
    ManagedBy   = "terraform"
    Owner       = "platform"
  }
}

module "vpc" {
  source = "../../modules/vpc"
  name   = local.name
  cidr   = "10.20.0.0/16"
  azs    = ["us-east-1a", "us-east-1b", "us-east-1c"]

  # COST: one shared NAT saves ~$65/mo vs one-per-AZ, at the price of a
  # single-AZ egress failure domain. Correct trade for dev; flip for prod.
  single_nat_gateway = true

  enable_flow_logs = true
  tags = local.tags
}

module "eks" {
  source     = "../../modules/eks"
  name       = local.name
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnet_ids
  version    = "1.31"

  # Small always-on group for cluster-critical addons (CoreDNS, Karpenter itself).
  # Everything else is Karpenter-provisioned.
  system_node_group = {
    instance_types = ["t4g.medium"]
    min_size = 2, max_size = 3, desired_size = 2
    ami_type = "AL2023_ARM_64_STANDARD"
  }

  cluster_endpoint_public_access       = true
  cluster_endpoint_public_access_cidrs = [var.admin_cidr]   # your IP only
  enable_cluster_creator_admin_permissions = true
  cluster_enabled_log_types = ["api", "audit", "authenticator"]
  tags = local.tags
}

module "rds" {
  source     = "../../modules/rds"
  name       = local.name
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnet_ids

  engine_version    = "16.4"
  instance_class    = "db.t4g.micro"
  allocated_storage = 20
  multi_az          = false          # true in prod; doubles cost

  # Mirrors the local decision: ONE instance, one database per service.
  # Bootstrapped by a null_resource/psql provisioner or a migration Job.
  databases = ["consignment_db", "facility_db", "dispatch_db"]

  manage_master_user_password = true   # AWS-managed rotation in Secrets Manager
  backup_retention_period     = 7
  deletion_protection         = false  # dev only — you WILL destroy this often
  skip_final_snapshot         = true
  performance_insights_enabled = true
  tags = local.tags
}

module "mq" {
  source     = "../../modules/mq"
  name       = local.name
  engine     = "RabbitMQ"
  version    = "3.13"
  host_type  = "mq.t3.micro"
  deployment = "SINGLE_INSTANCE"      # CLUSTER_MULTI_AZ in prod
  subnet_ids = [module.vpc.private_subnet_ids[0]]
  tags       = local.tags
}

module "elasticache" {
  source     = "../../modules/elasticache"
  name       = local.name
  engine     = "valkey"
  node_type  = "cache.t4g.micro"
  subnet_ids = module.vpc.private_subnet_ids
  tags       = local.tags
}
```

**Module design rules.** Modules take primitives and return IDs — never let a module reach out with
its own `data` lookups for things the caller knows, or composition becomes untestable. No `provider`
blocks inside modules. Version every provider and every external module. `environments/dev` is
composition only: no `resource` blocks, only `module` and `locals`.

### 4.3 Karpenter over Cluster Autoscaler

Cluster Autoscaler scales pre-defined node groups; you decide the instance types in advance and it
adds nodes of that shape. **Karpenter** looks at pending pods and provisions the cheapest instance
that fits, from a broad type list, bin-packs aggressively, and consolidates underused nodes. For a
bursty workload driven by a traffic simulator, the difference is dramatic and *visible*.

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata: { name: fleetpulse-default }
spec:
  template:
    spec:
      requirements:
        - { key: kubernetes.io/arch,              operator: In, values: ["arm64"] }
        - { key: karpenter.sh/capacity-type,      operator: In, values: ["spot", "on-demand"] }
        - { key: karpenter.k8s.aws/instance-category, operator: In, values: ["t", "m", "c"] }
        - { key: karpenter.k8s.aws/instance-generation, operator: Gt, values: ["5"] }
      nodeClassRef: { group: karpenter.k8s.aws, kind: EC2NodeClass, name: default }
      expireAfter: 168h                  # forced node rotation = patching for free
  limits: { cpu: "32" }                  # hard ceiling — protects you from a runaway bill
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 60s
```

Graviton (`arm64`) + spot is roughly 60–70% off on-demand x86. Your images are already multi-arch
from §3.3. `limits.cpu` is a genuine financial safety net — a misconfigured HPA cannot provision
unbounded nodes.

Spot interruptions are a *feature* here: they give you free chaos engineering. If a spot reclaim
causes lost work, your graceful shutdown (§2.5) is wrong.

### 4.4 Pod identity — IRSA and its successor

The brief asks for IRSA. Worth knowing that **EKS Pod Identity** (2023+) is now the simpler path:
no OIDC provider per cluster, no trust-policy string-matching on the service account, and roles are
reusable across clusters. Learn IRSA because it is everywhere in existing estates; prefer Pod
Identity for anything new.

```hcl
# modules/irsa — least-privilege example for notification-service
data "aws_iam_policy_document" "notification" {
  statement {
    sid       = "PublishMerchantNotifications"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.merchant_events.arn]   # this topic, not "*"
  }
  statement {
    sid       = "ReadWebhookSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:fleetpulse/${var.env}/webhook-*"]
  }
}

# IRSA: trust policy pins BOTH the namespace and the service account name.
data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals { type = "Federated", identifiers = [var.oidc_provider_arn] }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:fleetpulse:notification-service"]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}
```

Both conditions are required. Pinning only `:aud` lets *any* service account in the cluster assume
the role — a complete failure of the control, and a very common real-world misconfiguration.

**Secrets:** External Secrets Operator syncing AWS Secrets Manager → Kubernetes Secrets, with ESO's
own service account IRSA-scoped to `fleetpulse/${env}/*`. Database credentials never appear in git,
and rotation propagates automatically.

### 4.5 Cost control — read this before `terraform apply`

This is a personal sandbox paying real money. Rough `dev` monthly, us-east-1, running continuously:

| Item | ~USD/mo |
|---|---|
| EKS control plane | $73 |
| NAT Gateway (single) | $33 + data |
| Karpenter nodes (2–4 × t4g.medium spot) | $15–30 |
| RDS `db.t4g.micro` single-AZ | $13 |
| Amazon MQ `mq.t3.micro` single | $16 |
| ElastiCache `cache.t4g.micro` | $12 |
| ALB, ECR, logs, data transfer | $20–30 |
| **Total** | **~$180–210** |

Non-negotiable guardrails, set up *before* the first apply:

1. **AWS Budgets alarm at $50/$100/$200** with email actions. First thing, before any infrastructure.
2. **`terraform destroy` between sessions.** The whole stack rebuilds in ~20 minutes. Practising
   destroy-and-rebuild is itself the exercise — it proves your IaC is complete.
3. **`infracost` in CI on `terraform plan`** so every PR shows its cost delta.
4. **Karpenter `limits.cpu`** as the runaway ceiling (§4.3).
5. **Tag everything** with `Project=fleetpulse` and use Cost Explorer grouped by tag.

A `make dev-up` / `make dev-down` pair around `terraform apply`/`destroy` removes the friction that
otherwise makes you leave it running.

---

## 5. Phase 4 — Observability, Reliability & Resilience

### 5.1 Custom metrics that actually matter

Instrument the *domain*, not just the HTTP layer. Anyone can graph request rate; the metrics below
tell you whether parcels are moving.

| Metric | Type | Labels | Why it earns its cardinality |
|---|---|---|---|
| `fleetpulse_waybills_created_total` | counter | `origin_hub`, `service_type` | Business throughput |
| `fleetpulse_parcel_state_transitions_total` | counter | `from_state`, `to_state` | Funnel; ratios expose stuck states |
| `fleetpulse_parcel_dwell_seconds` | histogram | `state`, `hub_id` | **Detects a hub silently not scanning** |
| `fleetpulse_illegal_transition_total` | counter | `from_state`, `to_state` | Pure bug signal; should be flat zero |
| `fleetpulse_outbox_pending` | gauge | `service` | Events stuck undelivered while HTTP looks healthy |
| `fleetpulse_outbox_oldest_age_seconds` | gauge | `service` | Relay stalled |
| `fleetpulse_event_propagation_seconds` | histogram | `event_type` | `occurred_at` → consumer processed; the true async SLI |
| `fleetpulse_webhook_delivery_total` | counter | `status_class`, `attempt` | Merchant-facing reliability |
| `fleetpulse_webhook_delivery_duration_seconds` | histogram | `status_class` | Slow merchants consume your workers |
| `fleetpulse_gps_pings_ingested_total` | counter | `source` | Telemetry volume |
| `fleetpulse_gps_batch_flush_duration_seconds` | histogram | — | Batch writer health |
| `rabbitmq_queue_messages_ready` | gauge | `queue` | From `rabbitmq-exporter`; KEDA's scaling input |

**Cardinality discipline.** Never label with `waybill`, `merchant_id`, or `driver_id` — millions of
values will kill Prometheus. Those belong in traces and logs, where high cardinality is free. Hub
count is in the hundreds and is fine. This is the lesson that separates people who have run
Prometheus at scale from people who have not.

`fleetpulse_parcel_dwell_seconds` deserves special note: it is the metric that catches the failure
mode with no error signal at all. A hub stops scanning; no request fails, no error rate moves, no
pod restarts — parcels just quietly stop progressing. Only dwell time sees it.

### 5.2 SLOs and burn-rate alerting

Three SLOs, each with an explicit error budget. Alert on *budget burn rate*, not on raw thresholds —
this is what stops your phone from buzzing at every transient blip.

| SLO | Target | Window |
|---|---|---|
| Waybill creation availability | 99.5% of `POST /v1/waybills` non-5xx | 30d rolling |
| Waybill creation latency | 99% under 300ms | 30d rolling |
| Event propagation | 99% of lifecycle events consumed within 5s | 30d rolling |
| Webhook delivery | 99% delivered within 60s (incl. retries) | 30d rolling |

```yaml
# Multi-window multi-burn-rate. Fast burn = page; slow burn = ticket.
groups:
  - name: fleetpulse-slo
    rules:
      - record: slo:waybill_availability:ratio_rate5m
        expr: |
          sum(rate(http_requests_total{service="consignment-service",route="/v1/waybills",code!~"5.."}[5m]))
          / sum(rate(http_requests_total{service="consignment-service",route="/v1/waybills"}[5m]))

      - alert: WaybillAvailabilityFastBurn
        # 14.4x burn over 1h exhausts a 30d budget in ~2 days.
        expr: |
          (1 - slo:waybill_availability:ratio_rate1h) > (14.4 * 0.005)
          and
          (1 - slo:waybill_availability:ratio_rate5m) > (14.4 * 0.005)
        for: 2m
        labels: { severity: page }
        annotations:
          summary: "Waybill creation burning error budget 14.4x — 2 days to exhaustion"

      - alert: OutboxRelayStalled
        # No error rate moves when this fires. That is the point.
        expr: fleetpulse_outbox_oldest_age_seconds > 60
        for: 2m
        labels: { severity: page }
        annotations:
          summary: "{{ $labels.service }} outbox stalled — events not publishing"

      - alert: DeadLetterQueueNonEmpty
        expr: rabbitmq_queue_messages_ready{queue=~".*\\.dlq"} > 0
        for: 5m
        labels: { severity: ticket }

      - alert: HubNotScanning
        # No parcel has left this hub in 30 min during business hours.
        expr: |
          sum by (hub_id) (rate(fleetpulse_parcel_state_transitions_total{from_state="AT_HUB"}[30m])) == 0
          and on() (hour() > 3 < 14)
        for: 15m
        labels: { severity: ticket }
```

Deploy `kube-prometheus-stack` via Helm, discover targets with `ServiceMonitor` CRDs from the
library chart, and provision Grafana dashboards as ConfigMaps with the `grafana_dashboard: "1"`
label so they are version-controlled rather than clicked together.

**Four dashboards:** *Parcel Flow* (business funnel, dwell heatmap by hub), *Service Health* (RED per
service), *Async Backbone* (queue depths, consumer lag, DLQ, outbox age, propagation latency), and
*Cost & Capacity* (node count, spot ratio, requests vs. usage).

### 5.3 Distributed tracing across the broker

HTTP tracing is solved by middleware. **The hard part is the async hop**, and it is where most
implementations quietly fail: the producer's span ends, the consumer starts a fresh root span, and
you get four disconnected traces instead of one parcel journey.

Fix it by injecting W3C trace context into AMQP headers on publish and extracting on consume:

```go
// pkg/events/propagation.go
var propagator = propagation.TraceContext{}

// amqpCarrier adapts AMQP headers to the OTel TextMapCarrier interface.
type amqpCarrier amqp.Table

func (c amqpCarrier) Get(k string) string {
	if v, ok := c[k]; ok { if s, ok := v.(string); ok { return s } }
	return ""
}
func (c amqpCarrier) Set(k, v string) { c[k] = v }
func (c amqpCarrier) Keys() []string  { /* ... */ }

func Publish(ctx context.Context, ch *amqp.Channel, ex, rk string, body []byte) error {
	ctx, span := tracer.Start(ctx, "publish "+rk,
		trace.WithSpanKind(trace.SpanKindProducer),
		trace.WithAttributes(
			semconv.MessagingSystemRabbitmq,
			semconv.MessagingDestinationName(ex),
			semconv.MessagingRabbitmqDestinationRoutingKey(rk),
		))
	defer span.End()

	headers := amqp.Table{}
	propagator.Inject(ctx, amqpCarrier(headers))   // writes `traceparent`

	return ch.PublishWithContext(ctx, ex, rk, false, false, amqp.Publishing{
		Headers:      headers,
		Body:         body,
		DeliveryMode: amqp.Persistent,
		MessageId:    eventIDFrom(body),
	})
}

func Consume(d amqp.Delivery, h Handler) {
	// Link back to the producer's trace instead of starting a fresh root.
	ctx := propagator.Extract(context.Background(), amqpCarrier(d.Headers))
	ctx, span := tracer.Start(ctx, "process "+d.RoutingKey,
		trace.WithSpanKind(trace.SpanKindConsumer))
	defer span.End()

	// High-cardinality IDs belong HERE, not on metrics labels.
	span.SetAttributes(
		attribute.String("fleetpulse.waybill", waybillFrom(d.Body)),
		attribute.String("fleetpulse.merchant_id", merchantFrom(d.Body)),
	)
	if err := h(ctx, d); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
}
```

One subtlety worth planning for: with the outbox, the publish happens in the *relay*, potentially
seconds after the originating request's span has closed. Store the `traceparent` in the outbox row
(it is already in the envelope) and have the relay use it as the parent — otherwise the trace
breaks precisely at the durability boundary you added for correctness.

**Collector topology:** OTel Collector as a DaemonSet agent (local, low-latency receive, adds
`k8s.*` resource attributes) forwarding to a gateway Deployment (tail sampling, batching, single
egress point). Jaeger as the backend locally per the brief; consider Grafana Tempo on AWS so traces
sit beside metrics and logs with exemplar links between them.

**Sampling:** 100% locally. On AWS use tail-based sampling — keep every error and every trace over
1s, sample the healthy remainder at 5%. Head sampling throws away the failures you actually need.

### 5.4 Autoscaling — the right signal per workload

Not every service should scale on CPU. Matching the signal to the workload is the interesting part:

| Service | Signal | Mechanism | Why |
|---|---|---|---|
| `consignment-service` | RPS + CPU | HPA | Synchronous, user-facing |
| `facility-service` | CPU | HPA | Scan bursts track CPU well |
| `dispatch-service` | Custom: GPS ingest rate | HPA + prometheus-adapter | Ingest-bound, not CPU-bound |
| `notification-service` | **Queue depth** | **KEDA** | Work is *in the queue*; CPU stays flat while the backlog grows |

The notification case is the one worth understanding deeply. A slow merchant webhook means workers
sit blocked on I/O — CPU near zero, backlog climbing. A CPU-based HPA never fires. KEDA scales on
the thing that actually represents unserved work:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: { name: notification-service, namespace: fleetpulse }
spec:
  scaleTargetRef: { name: notification-service }
  minReplicaCount: 2
  maxReplicaCount: 20
  cooldownPeriod: 120
  triggers:
    - type: rabbitmq
      metadata:
        protocol: amqp
        queueName: notification.parcel-events
        mode: QueueLength
        value: "100"              # target ~100 ready messages per replica
      authenticationRef: { name: rabbitmq-auth }
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 30      # react fast to backlog
          policies: [{ type: Percent, value: 100, periodSeconds: 30 }]
        scaleDown:
          stabilizationWindowSeconds: 300     # descend slowly; avoid flapping
```

Asymmetric scaling behaviour — fast up, slow down — is the setting people miss, and it is the
difference between smooth autoscaling and thrashing.

### 5.5 Chaos experiments

Chaos engineering is **hypothesis-first**. "Break something and see" is not an experiment. Each one
below states a steady-state definition, an injection, and a prediction — and the value is entirely
in the cases where the prediction turns out wrong.

| # | Hypothesis | Injection (Chaos Mesh) | Expected | Common real outcome |
|---|---|---|---|---|
| 1 | Lifecycle delivery is unaffected by telemetry floods | `--profile spike` at 50× GPS | Telemetry queue drops head; `parcel.*` propagation p99 stays <5s | Shared channel/conn pool couples them; you need separate connections |
| 2 | Consumer restart loses no events | `PodChaos: kill` notification pods | Unacked messages redelivered; no gap in webhooks | Missing graceful shutdown → DLQ growth on every deploy |
| 3 | Slow merchant does not starve other merchants | `NetworkChaos: delay 5s` to webhook-sink | Only that merchant's deliveries slow | Head-of-line blocking; you need per-merchant concurrency limits |
| 4 | Outbox survives broker outage | Stop RabbitMQ 5 min | Writes keep succeeding; `outbox_pending` climbs; full drain on recovery | Relay backs off wrongly, or the API 500s on publish it should not do |
| 5 | KEDA absorbs a 10× burst | `--profile spike` | Scale 2→~15 in <90s; queue drains; no DLQ | `maxReplicas` too low, or DB connection pool exhausts before pods do |
| 6 | Postgres failover is survivable | RDS reboot-with-failover | <60s of 5xx, then automatic recovery | pgx pool caches dead conns; needs health checks + retry |
| 7 | Spot reclaim is a non-event | Karpenter node drain | PDB holds availability; work completes elsewhere | PDB missing → all replicas evicted at once |
| 8 | Losing one AZ degrades but does not break | `NetworkChaos: partition` one AZ | Topology spread keeps serving | Single-NAT dev topology makes this fatal — an accurate finding |

```yaml
# Experiment 3 — slow merchant
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata: { name: slow-merchant-webhook, namespace: fleetpulse }
spec:
  action: delay
  mode: all
  selector:
    namespaces: [fleetpulse]
    labelSelectors: { app: webhook-sink }
  delay: { latency: "5s", jitter: "1s", correlation: "50" }
  duration: "5m"
```

Run each experiment, record what actually happened against the prediction, and fix the gaps. That
written record — hypothesis, result, fix — is the most valuable artifact this whole project
produces, and it is what makes the work legible in an interview.

### 5.6 Reliability primitives to have in place first

Do not run chaos experiments against a system missing these; you will only rediscover their absence:

- **PodDisruptionBudget** on every Deployment (`minAvailable: 1`, or 50% for larger ones).
- **`topologySpreadConstraints`** across `topology.kubernetes.io/zone`, `whenUnsatisfiable: ScheduleAnyway`.
- **Accurate resource requests** — Karpenter bin-packs on requests; wrong values mean wrong nodes.
- **Readiness vs. liveness** distinguished properly. Readiness checks dependencies (DB, broker);
  liveness checks only that the process is not deadlocked. A liveness probe that checks the database
  turns a brief DB blip into a cluster-wide restart storm — a classic self-inflicted outage.
- **Graceful shutdown** (§2.5), verified by experiment 2.
- **Client-side retries with jitter** and circuit breakers on cross-service HTTP.
- **Connection pool sizing** — `max_conns` per replica × `maxReplicas` must stay under the RDS
  connection limit. `db.t4g.micro` allows ~85. Twenty notification pods at 10 connections each is
  200: an outage caused by successful autoscaling. Use PgBouncer or cap the pool.

That last one bites almost everyone the first time they combine aggressive autoscaling with a small
RDS instance. Work out the arithmetic before experiment 5, not during it.

---

## 6. Execution roadmap

Six milestones, each with a hard exit criterion. **Do not start the next milestone until the current
one's criterion is demonstrably met** — the failure mode for a project this size is a half-built
Kubernetes layer on top of a service that was never correct.

Estimates assume roughly 8–12 focused hours per week.

### Milestone 1 — One service, end to end (Weeks 1–2)

Depth before breadth. One service done properly teaches more than four scaffolded.

- [ ] Replace `consignment-service/Dockerfile` with the multi-stage distroless build
- [ ] `POST /v1/waybills`, `GET /v1/waybills/{id}`, `GET /healthz`, `GET /readyz`
- [ ] `pgx` + `sqlc` + `goose` migrations; state machine in `internal/domain` with **no I/O**
- [ ] Structured `slog` JSON logging
- [ ] `/metrics` with `fleetpulse_waybills_created_total` and RED HTTP histograms
- [ ] Compose: Postgres + consignment-service, healthchecks wired
- [ ] Unit tests on the state machine; one `testcontainers` integration test
- [ ] **Strip the UTF-8 BOM from all existing files** (see CLAUDE.md)
- [ ] First git commit — the repo currently has zero

> **Exit:** `docker compose up -d` → `curl -X POST localhost:8081/v1/waybills` returns a waybill,
> the row is in `consignment_db`, `curl localhost:8081/metrics | grep fleetpulse` shows the counter
> incrementing, and `go test ./...` is green.

### Milestone 2 — The event backbone (Weeks 3–4)

The most conceptually demanding milestone. Take the time.

- [ ] `pkg/events`: envelope, ULID `event_id`, versioning
- [ ] Outbox table, relay goroutine with `FOR UPDATE SKIP LOCKED`, publisher confirms
- [ ] RabbitMQ topology as code (exchanges, queues, bindings, DLX ladder) at service startup
- [ ] `notification-service`: consume `parcel.*`, HMAC-signed webhook POST to `webhook-sink`
- [ ] Redis dedupe on `event_id`; retry ladder 30s → 5m → 30m → DLQ
- [ ] Graceful shutdown: SIGTERM → cancel consumer → drain in-flight → ack → exit
- [ ] Metrics: `outbox_pending`, `outbox_oldest_age_seconds`, `webhook_delivery_total`

> **Exit:** booking a waybill produces a webhook hit at `localhost:8090`. Then: `docker compose stop
> rabbitmq`, book 10 parcels (all 201s, outbox grows), `docker compose start rabbitmq` → all 10
> webhooks arrive. Separately, `docker kill` the notification container mid-flight and confirm zero
> lost and zero duplicate deliveries.

### Milestone 3 — Full domain and load (Weeks 5–6)

- [ ] `facility-service`: hub scans, bags, transit manifests, `facility_db`
- [ ] `dispatch-service`: drivers, runsheets, POD/NDR, `dispatch_db`
- [ ] GPS ingest → `fleetpulse.telemetry` → Redis last-position → 5s batch flush to Postgres
- [ ] Full compose stack including all four services
- [ ] Python simulator with all four profiles (`steady`, `spike`, `hub-outage`, `flaky-merchant`)
- [ ] `fleetpulse_parcel_dwell_seconds` and `event_propagation_seconds` instrumented

> **Exit:** `python -m fleetsim --rps 100 --duration 10m --profile steady` runs clean — every parcel
> reaches a terminal state, DLQ depth stays 0, outbox age stays under 1s, no goroutine leak
> (`/debug/pprof/goroutine` flat).

### Milestone 4 — Kubernetes and CI (Weeks 7–8)

- [ ] `fleetpulse-common` library chart; four thin service charts; umbrella chart
- [ ] Migration Job as a Helm `pre-install`/`pre-upgrade` hook
- [ ] kind cluster config; full stack running on kind
- [ ] `ci.yml` with path filtering, lint, `-race` tests, helm lint + kubeconform
- [ ] `build-push.yml`: buildx multi-arch → GHCR, Trivy gate, cosign keyless signing
- [ ] PDBs, topology spread, correct requests, readiness/liveness split
- [ ] Simulator runs against the kind cluster through an ingress

> **Exit:** a PR runs green CI in under 5 minutes and rebuilds only the changed service; `helm
> install` on a fresh kind cluster brings up the whole stack with zero manual steps; the simulator
> drives it successfully.

### Milestone 5 — AWS and GitOps (Weeks 9–11)

The longest milestone. Set the budget alarm on day one.

- [ ] **AWS Budgets alarms at $50/$100/$200 — before anything else**
- [ ] S3 state backend with `use_lockfile`
- [ ] `vpc` module: 3 AZs, public/private, single NAT, flow logs
- [ ] `eks` module: 1.31, system node group, OIDC provider, audit logging
- [ ] `rds`, `mq`, `elasticache` modules in private subnets
- [ ] SSM Session Manager access (no bastion)
- [ ] Karpenter with arm64 + spot NodePool and a `limits.cpu` ceiling
- [ ] External Secrets Operator with IRSA
- [ ] ArgoCD installed; `deploy/` structure; app-of-apps; `selfHeal: true`
- [ ] `promote.yml` commits image tags; ArgoCD reconciles
- [ ] `infracost` on `terraform plan` in CI
- [ ] `make dev-up` / `make dev-down`

> **Exit:** from zero, `terraform apply` → EKS running; ArgoCD syncs all four services; the API is
> reachable through an ALB. Then `kubectl scale deploy/notification-service --replicas=9` and watch
> ArgoCD revert it. Finally `terraform destroy` leaves nothing behind (verify in Cost Explorer next
> day), and a second `apply` reproduces everything.

### Milestone 6 — Observability and resilience (Weeks 12–14)

- [ ] `kube-prometheus-stack`; ServiceMonitors from the library chart
- [ ] `rabbitmq-exporter` and `postgres-exporter`
- [ ] Four Grafana dashboards, provisioned as labelled ConfigMaps
- [ ] Recording rules + multi-window burn-rate alerts + `OutboxRelayStalled` + `HubNotScanning`
- [ ] Alertmanager → email/Slack, with severity-based routing
- [ ] OTel Collector agent + gateway; Jaeger (or Tempo); **AMQP trace propagation working**
- [ ] Tail sampling: all errors, all >1s, 5% baseline
- [ ] KEDA `ScaledObject` for notification; HPAs for the rest; asymmetric scale behaviour
- [ ] Connection-pool arithmetic verified against the RDS connection limit
- [ ] Chaos Mesh; run all 8 experiments; **write up hypothesis vs. actual for each**
- [ ] Rewrite the CLAUDE.md "Current state: scaffold only" section — it will be badly out of date

> **Exit:** open one trace in Jaeger and follow a single waybill from `POST /v1/waybills` through
> facility scans, dispatch assignment, and the outbound webhook — **one connected trace, all four
> services, spanning both HTTP and AMQP hops**. Then run `--profile spike` and watch KEDA scale
> notification 2→15 while the burn-rate alert stays quiet because the system absorbed it.

That last exit criterion is the real finish line. A single trace crossing four services and an
asynchronous broker boundary is the artifact that proves the whole system — architecture,
instrumentation, and infrastructure — actually works together.

---

## 7. Recommended reading order for revisiting

When you come back to this document mid-project, the sections you will actually need again are
§1.6 (outbox — the correctness core), §5.3 (trace propagation — the hardest implementation detail),
§5.4 (KEDA vs. HPA signal choice), and §4.5 (cost guardrails). The rest is mostly read-once.
