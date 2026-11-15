"""Prometheus metrics for the consignment service."""

from prometheus_client import Counter, Histogram

http_requests_total = Counter(
    "fleetpulse_http_requests_total",
    "HTTP requests handled by FleetPulse.",
    ["method", "endpoint", "status"],
)
http_request_duration_seconds = Histogram(
    "fleetpulse_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
