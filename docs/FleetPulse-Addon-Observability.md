# Add-On: Observability

Bolt observability onto FleetPulse **after** the core two-service system works.

Prerequisite: [FleetPulse-Simple.md](FleetPulse-Simple.md) built and running. Read
[FleetPulse-Architecture.md](FleetPulse-Architecture.md) first if you have not — you cannot
instrument flows you do not understand.

Nothing here changes the architecture. It adds visibility to what already exists.

---

## 0. Why this comes second

Instrumenting a system before it works means debugging two things at once. Instrumenting one that
already works means every metric you add answers a question you have actually had.

**Five stages, each independently useful.** Stop at any point and you still have something better
than before.

| Stage | What you get | Effort | Cost |
|---|---|---|---|
| 1. Structured logging | Searchable logs with request IDs | 1 hr | $0 |
| 2. `/metrics` endpoint | Prometheus-format numbers | 2 hr | $0 |
| 3. Prometheus + Grafana | Dashboards | 2 hr | $0 local |
| 4. Alert rules | Told when something breaks | 1 hr | $0 |
| 5. Tracing (OpenTelemetry) | One trace across both services | 3 hr | $0 local |

Stages 1 and 2 give you most of the value. Do not skip them to get to Grafana — a pretty dashboard
built on bad metrics is worse than no dashboard.

### ⚠️ Where this can and cannot run

| Environment | RAM available | Prometheus + Grafana (~380 MB) |
|---|---|---|
| Laptop (Compose / minikube) | 8–16 GB | ✅ Yes |
| **EC2 `t3.micro` (1 GB)** | ~538 MB free | ⚠️ **Barely — will be unstable** |
| EKS `t3.small` nodes | 2 GB/node | ✅ Yes |

On your `t3.micro`, stages 1–2 are free (they run inside the app). For stages 3–4 use **Grafana
Cloud's free tier** (10k series, forever) with a lightweight agent, rather than self-hosting. §3.3
covers it.

---

## 1. Stage 1 — Structured logging

Plain-text logs are unsearchable. JSON logs with a request ID let you follow one request across both
services.

```python
# services/consignment-service/app/logging_setup.py
"""JSON logging with a request ID that follows a request across services."""
import json
import logging
import sys
import uuid
from contextvars import ContextVar

# ContextVar survives across async awaits — a plain global would leak
# between concurrent requests.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": getattr(record, "service", "unknown"),
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        # Anything passed as extra={...} shows up as a top-level field.
        for k, v in getattr(record, "extra_fields", {}).items():
            entry[k] = v
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(service_name: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Stamp every record with the service name.
    old_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.service = service_name
        return record

    logging.setLogRecordFactory(factory)
```

```python
# app/middleware.py
"""Assign a request ID, propagate it, and log every request."""
import time
import uuid
from fastapi import Request
from .logging_setup import request_id_var
import logging

log = logging.getLogger("http")


async def request_context(request: Request, call_next):
    # Reuse an inbound ID if present, so a request that crosses from
    # dispatch to consignment keeps ONE id in both services' logs.
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
    request_id_var.set(rid)

    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000

    log.info("request", extra={"extra_fields": {
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "duration_ms": round(duration_ms, 2),
    }})
    response.headers["X-Request-ID"] = rid
    return response
```

```python
# app/main.py — wire it up
from .logging_setup import setup_logging
from .middleware import request_context

setup_logging("consignment-service")
app.middleware("http")(request_context)
```

**Propagate the ID on the cross-service call** — this is what makes it worth doing:

```python
# services/dispatch-service/app/consignment_client.py
from .logging_setup import request_id_var

def _headers() -> dict:
    return {"X-Request-ID": request_id_var.get()}

def get_waybill(awb: str) -> dict:
    r = httpx.get(f"{BASE_URL}/api/v1/waybills/{awb}",
                  headers=_headers(), timeout=TIMEOUT)
    ...
```

```bash
# Now one request ID shows the whole journey across BOTH services:
docker compose logs | grep '"request_id":"a3f9c210"' | jq .
```

✅ **Done when:** you can trace a single runsheet creation through both services' logs with one grep.

---

## 2. Stage 2 — Metrics

```
pip install prometheus-client==0.21.0
```

### 2.1 The metrics worth having

Generic HTTP metrics tell you the API is up. **Domain metrics tell you parcels are moving** — which
is the thing that actually matters, and the thing most portfolio projects miss.

```python
# app/metrics.py
"""Prometheus metrics for FleetPulse.

CARDINALITY RULE: never use awb, merchant_id, driver_id, or vehicle_id as a
label. Each unique value creates a separate time series; millions of AWBs
would kill Prometheus. Hub IDs (a few hundred) are fine.
"""
from prometheus_client import Counter, Histogram, Gauge

# ---- RED: Rate, Errors, Duration (works for any HTTP service) ----
http_requests_total = Counter(
    "fleetpulse_http_requests_total",
    "HTTP requests",
    ["method", "endpoint", "status"],
)
http_request_duration_seconds = Histogram(
    "fleetpulse_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# ---- Business metrics: is the logistics network working? ----
waybills_created_total = Counter(
    "fleetpulse_waybills_created_total",
    "Waybills booked",
    ["origin_hub", "payment_mode"],       # bounded label values
)
parcel_transitions_total = Counter(
    "fleetpulse_parcel_transitions_total",
    "Parcel status changes",
    ["from_status", "to_status"],
)
illegal_transitions_total = Counter(
    "fleetpulse_illegal_transitions_total",
    "Rejected status changes (409)",
    ["from_status", "to_status"],
)
deliveries_total = Counter(
    "fleetpulse_deliveries_total",
    "Final delivery outcomes",
    ["outcome"],                           # DELIVERED | RTO
)

# ---- Cache: proves your Redis decision was worth it ----
cache_operations_total = Counter(
    "fleetpulse_cache_operations_total",
    "Cache lookups",
    ["result"],                            # hit | miss | error
)

# ---- Cross-service calls: the only network hop in the system ----
cross_service_calls_total = Counter(
    "fleetpulse_cross_service_calls_total",
    "Calls from dispatch to consignment",
    ["operation", "result"],               # result: success | client_error | unreachable
)
cross_service_duration_seconds = Histogram(
    "fleetpulse_cross_service_duration_seconds",
    "Latency of calls to consignment-service",
    ["operation"],
)

# ---- GPS ----
gps_pings_total = Counter("fleetpulse_gps_pings_total", "GPS pings ingested")
active_vehicles = Gauge("fleetpulse_active_vehicles", "Vehicles reporting in the last hour")
```

### 2.2 Expose and record

```python
# app/main.py
from fastapi import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from . import metrics

@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

Record inside the existing handlers — a few lines each:

```python
# In create_waybill()
metrics.waybills_created_total.labels(
    origin_hub=req.origin_hub, payment_mode=req.payment_mode
).inc()

# In record_scan(), on the 409 path
metrics.illegal_transitions_total.labels(
    from_status=current, to_status=req.status
).inc()

# In record_scan(), on success
metrics.parcel_transitions_total.labels(
    from_status=current, to_status=req.status
).inc()

# In cache.py
def cache_get(key: str) -> dict | None:
    try:
        raw = _r.get(key)
        metrics.cache_operations_total.labels(result="hit" if raw else "miss").inc()
        return json.loads(raw) if raw else None
    except Exception as e:
        metrics.cache_operations_total.labels(result="error").inc()
        log.warning("cache read failed: %s", e)
        return None
```

Add HTTP metrics to the middleware you already wrote:

```python
# app/middleware.py — inside request_context, after call_next
# Use the ROUTE TEMPLATE, not the raw path. Otherwise every AWB creates
# its own time series and you have a cardinality explosion.
route = request.scope.get("route")
endpoint = route.path if route else "unmatched"

metrics.http_requests_total.labels(
    method=request.method, endpoint=endpoint, status=response.status_code
).inc()
metrics.http_request_duration_seconds.labels(
    method=request.method, endpoint=endpoint
).observe(duration_ms / 1000)
```

> **That route-template detail is the most important line in this section.** Labelling with
> `request.url.path` means `/api/v1/waybills/FP123...` becomes a unique series per parcel. A few
> thousand parcels and Prometheus falls over. `route.path` gives you
> `/api/v1/waybills/{awb}` — one series.

✅ **Done when:** `curl localhost:8001/metrics | grep fleetpulse` shows counters incrementing as you
run the simulator.

---

## 3. Stage 3 — Prometheus and Grafana

### 3.1 Local Docker Compose

```yaml
# docker-compose.yml — add these under a profile so `docker compose up`
# stays light. Start them with:  docker compose --profile obs up -d
  prometheus:
    profiles: ["obs"]
    image: prom/prometheus:latest
    volumes:
      - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./observability/alerts.yml:/etc/prometheus/alerts.yml:ro
    ports: ["9090:9090"]
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=24h'   # short: this is a laptop

  grafana:
    profiles: ["obs"]
    image: grafana/grafana:latest
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Admin      # no login prompt locally
    volumes:
      - ./observability/grafana:/etc/grafana/provisioning:ro
    ports: ["3000:3000"]
    depends_on: [prometheus]
```

```yaml
# observability/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/alerts.yml

scrape_configs:
  - job_name: consignment-service
    static_configs:
      - targets: ['consignment-service:8000']
        labels: { service: consignment-service }

  - job_name: dispatch-service
    static_configs:
      - targets: ['dispatch-service:8000']
        labels: { service: dispatch-service }
```

```yaml
# observability/grafana/datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
```

```bash
docker compose --profile obs up -d
# Prometheus targets: http://localhost:9090/targets  (both should be UP)
# Grafana:            http://localhost:3000
```

### 3.2 Queries worth putting on a dashboard

```promql
# --- Row 1: Service health (RED) ---

# Request rate by endpoint
sum by (endpoint) (rate(fleetpulse_http_requests_total[5m]))

# Error rate as a percentage
100 * sum(rate(fleetpulse_http_requests_total{status=~"5.."}[5m]))
    / sum(rate(fleetpulse_http_requests_total[5m]))

# p95 latency
histogram_quantile(0.95,
  sum by (le, endpoint) (rate(fleetpulse_http_request_duration_seconds_bucket[5m])))

# --- Row 2: Business ---

# Bookings per minute by hub
sum by (origin_hub) (rate(fleetpulse_waybills_created_total[5m])) * 60

# Delivery success rate — the single most important business number
100 * sum(rate(fleetpulse_deliveries_total{outcome="DELIVERED"}[30m]))
    / sum(rate(fleetpulse_deliveries_total[30m]))

# Parcel flow by transition
sum by (to_status) (rate(fleetpulse_parcel_transitions_total[5m]))

# --- Row 3: Dependencies ---

# Cache hit ratio — proves the Redis decision paid off
100 * sum(rate(fleetpulse_cache_operations_total{result="hit"}[5m]))
    / sum(rate(fleetpulse_cache_operations_total{result=~"hit|miss"}[5m]))

# Cross-service call latency p99
histogram_quantile(0.99,
  sum by (le) (rate(fleetpulse_cross_service_duration_seconds_bucket[5m])))

# Cross-service failures
sum by (result) (rate(fleetpulse_cross_service_calls_total{result!="success"}[5m]))
```

**Build four panels first**, not forty: request rate, error rate, delivery success rate, cache hit
ratio. Add more only when you have a question they cannot answer.

### 3.3 On the `t3.micro` — use Grafana Cloud instead

Prometheus + Grafana need ~380 MB. Your EC2 box has ~538 MB free. It would technically start and
then fall over under load.

Grafana Cloud's free tier (10k series, 14-day retention, no time limit) plus a ~60 MB agent is the
right answer:

```yaml
# docker-compose.prod.yml — add to the EC2 stack
  alloy:
    image: grafana/alloy:latest
    volumes:
      - ./observability/alloy.river:/etc/alloy/config.river:ro
    command: ["run", "/etc/alloy/config.river"]
    environment:
      GC_PROM_URL:  ${GC_PROM_URL}
      GC_PROM_USER: ${GC_PROM_USER}
      GC_PROM_KEY:  ${GC_PROM_KEY}
    deploy:
      resources:
        limits: { memory: 128M }
```

```river
// observability/alloy.river
prometheus.scrape "fleetpulse" {
  targets = [
    { __address__ = "consignment-service:8000", service = "consignment-service" },
    { __address__ = "dispatch-service:8000",    service = "dispatch-service" },
  ]
  forward_to = [prometheus.remote_write.gc.receiver]
  // 60s not 15s — 4x fewer samples against the 10k series cap.
  scrape_interval = "60s"
}

prometheus.remote_write "gc" {
  endpoint {
    url = sys.env("GC_PROM_URL")
    basic_auth {
      username = sys.env("GC_PROM_USER")
      password = sys.env("GC_PROM_KEY")
    }
  }
}
```

### 3.4 On Kubernetes

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prom prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --set prometheus.prometheusSpec.retention=24h
```

```yaml
# k8s/base/09-servicemonitor.yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: fleetpulse
  namespace: fleetpulse
  labels:
    release: kube-prom          # must match the Helm release, or it is ignored
spec:
  selector:
    matchLabels: { app: consignment-service }
  endpoints:
    - port: http
      path: /metrics
      interval: 30s
```

That `release:` label is the usual reason a ServiceMonitor is silently ignored — Prometheus only
picks up monitors matching its configured selector.

---

## 4. Stage 4 — Alerts

```yaml
# observability/alerts.yml
groups:
  - name: fleetpulse
    rules:
      - alert: HighErrorRate
        expr: |
          100 * sum(rate(fleetpulse_http_requests_total{status=~"5.."}[5m]))
              / sum(rate(fleetpulse_http_requests_total[5m])) > 5
        for: 5m
        labels: { severity: critical }
        annotations:
          summary: "Over 5% of requests are failing"

      - alert: ConsignmentUnreachable
        # Dispatch cannot reach consignment — runsheets and deliveries
        # are silently half-completing.
        expr: sum(rate(fleetpulse_cross_service_calls_total{result="unreachable"}[5m])) > 0
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "dispatch-service cannot reach consignment-service"

      - alert: DeliverySuccessRateLow
        expr: |
          100 * sum(rate(fleetpulse_deliveries_total{outcome="DELIVERED"}[1h]))
              / sum(rate(fleetpulse_deliveries_total[1h])) < 80
        for: 15m
        labels: { severity: warning }
        annotations:
          summary: "Delivery success below 80% — RTO rate is abnormal"

      - alert: NoParcelMovement
        # THE IMPORTANT ONE. Nothing errors, nothing 500s, no pod restarts —
        # parcels just quietly stop progressing. Only this catches it.
        expr: sum(rate(fleetpulse_parcel_transitions_total[30m])) == 0
        for: 30m
        labels: { severity: warning }
        annotations:
          summary: "No parcel status changes in 30 minutes"

      - alert: IllegalTransitionsSpiking
        expr: sum(rate(fleetpulse_illegal_transitions_total[10m])) > 0.5
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "Many 409s — a client is sending out-of-order scans"

      - alert: CacheHitRateCollapsed
        expr: |
          100 * sum(rate(fleetpulse_cache_operations_total{result="hit"}[10m]))
              / sum(rate(fleetpulse_cache_operations_total{result=~"hit|miss"}[10m])) < 30
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "Cache hit rate below 30% — Redis may be down or evicting"
```

`NoParcelMovement` is the alert to be proudest of. Every other alert fires on an *error*. This one
fires on an *absence* — the failure mode where everything reports healthy and nothing is actually
happening. Real logistics systems fail this way (a hub stops scanning, an integration silently
stops) and it is invisible to error-rate monitoring.

---

## 5. Stage 5 — Distributed tracing

With two services and one hop, tracing is optional. Do it because the *concept* matters and this is
the smallest system in which it makes sense.

```
pip install opentelemetry-distro opentelemetry-exporter-otlp \
            opentelemetry-instrumentation-fastapi \
            opentelemetry-instrumentation-httpx \
            opentelemetry-instrumentation-psycopg
```

```python
# app/tracing.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
import os


def setup_tracing(app, service_name: str) -> None:
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317"),
        insecure=True,
    )))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    # THIS is the one that matters: it injects W3C traceparent headers into
    # outgoing httpx calls, so dispatch → consignment appears as ONE trace
    # instead of two disconnected ones.
    HTTPXClientInstrumentor().instrument()
```

```yaml
# docker-compose.yml, obs profile
  jaeger:
    profiles: ["obs"]
    image: jaegertracing/all-in-one:latest
    environment:
      COLLECTOR_OTLP_ENABLED: "true"
    ports:
      - "16686:16686"    # UI
      - "4317:4317"      # OTLP gRPC
```

Add domain context to spans — this is where high-cardinality IDs belong, since traces are sampled
and stored differently from metrics:

```python
from opentelemetry import trace

@app.post("/api/v1/runsheets")
def create_runsheet(req: RunsheetRequest):
    span = trace.get_current_span()
    span.set_attribute("fleetpulse.driver_id", req.driver_id)
    span.set_attribute("fleetpulse.hub_id", req.hub_id)
    span.set_attribute("fleetpulse.parcel_count", len(req.awbs))
    ...
```

✅ **Done when:** you open Jaeger, search for `dispatch-service`, and see a single trace with the
parent span in dispatch, child spans for each HTTP call, and the corresponding consignment spans
nested beneath — **one trace crossing a network boundary.** That picture is the whole point.

---

## 6. Milestone checklist

### Milestone O1 — Logs and metrics (Week 1)
- [ ] JSON logging in both services
- [ ] Request-ID middleware; propagate via `X-Request-ID` on the cross-service call
- [ ] `app/metrics.py`; expose `/metrics`
- [ ] HTTP metrics in middleware — **using the route template, not the raw path**
- [ ] Business counters wired into the handlers
- [ ] Cache hit/miss counters

> ✅ Run the simulator; `curl localhost:8001/metrics | grep fleetpulse` shows numbers moving.

### Milestone O2 — Dashboards (Week 2)
- [ ] Prometheus + Grafana behind an `obs` Compose profile
- [ ] Both targets UP at `localhost:9090/targets`
- [ ] Four panels: request rate, error rate, delivery success, cache hit ratio
- [ ] Run the simulator and watch them move

> ✅ You can point at a graph and explain what the system was doing.

### Milestone O3 — Alerts (Week 2)
- [ ] `alerts.yml` loaded
- [ ] **Break something on purpose** — stop consignment-service and watch `ConsignmentUnreachable` fire
- [ ] Stop the simulator and watch `NoParcelMovement` fire after 30 min

> ✅ An alert fired because of a real condition you caused.

### Milestone O4 — Tracing (Week 3, optional)
- [ ] Jaeger in the `obs` profile
- [ ] OTel in both services, `HTTPXClientInstrumentor` enabled
- [ ] One trace spanning both services for a runsheet creation
- [ ] Screenshot it — this is a strong portfolio image

### Milestone O5 — Cloud (Week 3)
- [ ] Grafana Cloud free tier + Alloy on the EC2 box
- [ ] Or `kube-prometheus-stack` + ServiceMonitor on minikube/EKS

---

## 7. Interview answers

**"What would you monitor in this system?"**
> "RED metrics per endpoint for service health, but the ones I care about are domain metrics —
> parcel state transitions, delivery success rate, cache hit ratio, and cross-service call failures.
> The alert I'm proudest of is `NoParcelMovement`: if no parcel changes status for 30 minutes,
> something is broken even though nothing errored. That's a failure mode error-rate monitoring
> can't see."

**"How do you avoid a cardinality explosion?"**
> "Never label a metric with an unbounded value. I label with hub ID — a few hundred values — never
> with AWB or merchant ID. The specific trap I hit was HTTP metrics: labelling with the raw request
> path meant every parcel lookup created its own time series. I use the route template
> `/api/v1/waybills/{awb}` instead. High-cardinality IDs go on trace spans, where they're cheap."

**"Why didn't you run Prometheus on your EC2 instance?"**
> "It doesn't fit. Prometheus and Grafana need about 380 MB and the `t3.micro` had 538 MB free after
> the app — it would run until it didn't. I used Grafana Cloud's free tier with a 60 MB agent
> instead, and scraped at 60 seconds rather than 15 to stay inside the 10k series limit."
