"""Unit tests for consignment-service.

    pytest -q                              run all
    pytest -q -k terminal                  run one by name
    pytest -q tests/test_waybills.py::test_health_returns_ok

These need NO database and NO Redis. TestClient(app) is used WITHOUT a
context manager, so FastAPI's lifespan never runs and the connection pool is
never opened. Every test here either hits a handler that touches nothing, or
is rejected by Pydantic validation before the handler runs.
"""

from fastapi.testclient import TestClient

from app.main import ALLOWED_TRANSITIONS, app, generate_awb

client = TestClient(app)


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "consignment"}


# ---------------------------------------------------------------------------
# AWB generation
# ---------------------------------------------------------------------------
def test_awb_format_is_fp_plus_ten_digits():
    awb = generate_awb()
    assert awb.startswith("FP")
    assert len(awb) == 12
    assert awb[2:].isdigit()


def test_awbs_are_not_repeated():
    """Not a uniqueness guarantee -- just a smoke test that it is random."""
    assert len({generate_awb() for _ in range(200)}) > 190


# ---------------------------------------------------------------------------
# The state machine. These are the highest-value tests in the service:
# they guard the one rule that keeps parcel status coherent.
# ---------------------------------------------------------------------------
def test_delivered_and_rto_are_terminal():
    assert ALLOWED_TRANSITIONS["DELIVERED"] == set()
    assert ALLOWED_TRANSITIONS["RTO"] == set()


def test_cannot_skip_straight_to_delivered():
    assert "DELIVERED" not in ALLOWED_TRANSITIONS["MANIFESTED"]
    assert "DELIVERED" not in ALLOWED_TRANSITIONS["IN_TRANSIT"]
    assert "DELIVERED" not in ALLOWED_TRANSITIONS["ARRIVED_AT_FACILITY"]


def test_out_for_delivery_only_reachable_from_a_facility():
    sources = [s for s, targets in ALLOWED_TRANSITIONS.items()
               if "OUT_FOR_DELIVERY" in targets]
    assert sources == ["ARRIVED_AT_FACILITY"]


def test_hub_loop_is_allowed():
    """A parcel passes through several hubs, so this self-loop must exist."""
    assert "IN_TRANSIT" in ALLOWED_TRANSITIONS["ARRIVED_AT_FACILITY"]
    assert "ARRIVED_AT_FACILITY" in ALLOWED_TRANSITIONS["IN_TRANSIT"]


def test_every_target_status_is_itself_a_known_state():
    """Guards against a typo creating an unreachable dead-end status."""
    for targets in ALLOWED_TRANSITIONS.values():
        for t in targets:
            assert t in ALLOWED_TRANSITIONS, f"{t} is not a declared state"


# ---------------------------------------------------------------------------
# Input validation -- Pydantic rejects these before any handler code runs,
# which is why they need no database.
# ---------------------------------------------------------------------------
def _booking(**overrides) -> dict:
    body = {
        "merchant_name": "Nykaa",
        "consignee_name": "Ravi Kumar",
        "consignee_phone": "9876543210",
        "consignee_addr": "12 MG Road, Bengaluru",
        "origin_hub": "HUB-BLR-01",
        "destination_hub": "HUB-DEL-03",
        "weight_grams": 900,
        "payment_mode": "PREPAID",
        "cod_amount": 0,
    }
    body.update(overrides)
    return body


def test_booking_rejects_negative_weight():
    assert client.post("/api/v1/waybills", json=_booking(weight_grams=-5)).status_code == 422


def test_booking_rejects_zero_weight():
    assert client.post("/api/v1/waybills", json=_booking(weight_grams=0)).status_code == 422


def test_booking_rejects_absurd_weight():
    assert client.post("/api/v1/waybills", json=_booking(weight_grams=99_999)).status_code == 422


def test_booking_rejects_short_phone():
    assert client.post("/api/v1/waybills", json=_booking(consignee_phone="123")).status_code == 422


def test_booking_rejects_unknown_payment_mode():
    assert client.post("/api/v1/waybills", json=_booking(payment_mode="BITCOIN")).status_code == 422


def test_booking_rejects_missing_field():
    body = _booking()
    del body["origin_hub"]
    assert client.post("/api/v1/waybills", json=body).status_code == 422


def test_scan_rejects_dispatch_owned_status():
    """A hub may not mark a parcel DELIVERED -- that belongs to dispatch."""
    r = client.post("/api/v1/scans", json={
        "awb": "FP1234567890", "status": "DELIVERED", "hub_id": "HUB-BLR-01",
    })
    assert r.status_code == 422
