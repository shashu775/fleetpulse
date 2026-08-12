"""Unit tests for dispatch-service.

    pytest -q
    pytest -q -k gps

Like consignment's tests, these need NO database, NO Redis and NO running
consignment-service. TestClient(app) is used without a context manager so
lifespan never runs, and every case here is rejected by Pydantic validation
before any handler code executes.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.consignment_client import ConsignmentError
from app.main import app
from app.models import DeliveryRequest

client = TestClient(app)


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "dispatch"}


# ---------------------------------------------------------------------------
# GPS validation -- a unit reporting garbage is rejected at the edge
# ---------------------------------------------------------------------------
def _ping(**overrides) -> dict:
    body = {"vehicle_id": "KA01AB1234", "lat": 12.9716, "lon": 77.5946, "speed_kmph": 32.5}
    body.update(overrides)
    return body


@pytest.mark.parametrize("lat", [-91, 91, 1000])
def test_gps_rejects_out_of_range_latitude(lat):
    assert client.post("/api/v1/gps", json=_ping(lat=lat)).status_code == 422


@pytest.mark.parametrize("lon", [-181, 181, -999])
def test_gps_rejects_out_of_range_longitude(lon):
    assert client.post("/api/v1/gps", json=_ping(lon=lon)).status_code == 422


def test_gps_rejects_negative_speed():
    assert client.post("/api/v1/gps", json=_ping(speed_kmph=-10)).status_code == 422


def test_gps_rejects_missing_vehicle_id():
    body = _ping()
    del body["vehicle_id"]
    assert client.post("/api/v1/gps", json=body).status_code == 422


# ---------------------------------------------------------------------------
# Runsheet validation
# ---------------------------------------------------------------------------
def _runsheet(**overrides) -> dict:
    body = {
        "driver_id": "DRV-4417",
        "driver_name": "Suresh Yadav",
        "vehicle_id": "KA01AB1234",
        "hub_id": "HUB-BLR-01",
        "awbs": ["FP1234567890"],
    }
    body.update(overrides)
    return body


def test_runsheet_rejects_empty_awb_list():
    """A runsheet with no parcels is meaningless."""
    assert client.post("/api/v1/runsheets", json=_runsheet(awbs=[])).status_code == 422


def test_runsheet_rejects_oversized_awb_list():
    """Caps the number of sequential HTTP calls one request can trigger."""
    assert client.post(
        "/api/v1/runsheets", json=_runsheet(awbs=[f"FP{i:010d}" for i in range(51)])
    ).status_code == 422


def test_runsheet_rejects_missing_driver():
    body = _runsheet()
    del body["driver_id"]
    assert client.post("/api/v1/runsheets", json=body).status_code == 422


# ---------------------------------------------------------------------------
# Delivery validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("outcome", ["LOST", "delivered", "", "MAYBE"])
def test_delivery_rejects_unknown_outcome(outcome):
    r = client.post("/api/v1/delivery", json={
        "awb": "FP1234567890", "runsheet_id": "RS-1", "outcome": outcome,
    })
    assert r.status_code == 422


@pytest.mark.parametrize("outcome", ["DELIVERED", "RTO"])
def test_delivery_accepts_the_two_real_outcomes(outcome):
    """The two valid outcomes pass validation.

    Validated against the model directly rather than through the endpoint:
    posting would reach the handler and need a live database, which would make
    this an integration test rather than a unit test.
    """
    req = DeliveryRequest(awb="FP1234567890", runsheet_id="RS-1", outcome=outcome)
    assert req.outcome == outcome
    assert req.reason is None


# ---------------------------------------------------------------------------
# The cross-service client -- the most important code in this service
# ---------------------------------------------------------------------------
def test_client_raises_when_consignment_is_unreachable(monkeypatch):
    """A network failure must raise, never return a guess.

    Guessing here would corrupt parcel state.
    """
    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)

    from app import consignment_client as cc
    with pytest.raises(ConsignmentError, match="unreachable"):
        cc.get_waybill("FP1234567890")


def test_client_raises_on_404(monkeypatch):
    def not_found(*args, **kwargs):
        return httpx.Response(404, json={"detail": "AWB not found"})

    monkeypatch.setattr(httpx, "get", not_found)

    from app import consignment_client as cc
    with pytest.raises(ConsignmentError, match="does not exist"):
        cc.get_waybill("FP0000000000")


def test_client_surfaces_409_reason(monkeypatch):
    """A rejected transition must report WHY -- it is the best debug signal."""
    def conflict(*args, **kwargs):
        return httpx.Response(
            409, json={"detail": "Cannot move AWB FP1 from DELIVERED to OUT_FOR_DELIVERY"}
        )

    monkeypatch.setattr(httpx, "patch", conflict)

    from app import consignment_client as cc
    with pytest.raises(ConsignmentError, match="Illegal status change"):
        cc.update_status("FP1", "OUT_FOR_DELIVERY")
