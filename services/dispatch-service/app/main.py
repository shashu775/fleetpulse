"""Fleet & Dispatch Service.

Owns drivers, vehicles, runsheets and delivery attempts. Drives the last three
parcel transitions (OUT_FOR_DELIVERY, DELIVERED, RTO) but performs none of them
itself -- it asks consignment-service over HTTP. See consignment_client.py.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from . import consignment_client as cc
from .cache import redis_client
from .db import close_pool, get_conn, init_pool
from .models import DeliveryRequest, GPSPing, RunsheetRequest

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dispatch")

# A vehicle that stops reporting expires on its own -- no cleanup job needed.
GPS_TTL_SECONDS = 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    log.info("dispatch-service ready")
    yield
    close_pool()


app = FastAPI(
    title="FleetPulse - Fleet & Dispatch Service",
    description="Driver runsheets, live GPS tracking, and final delivery status.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "service": "dispatch"}


# ---------------------------------------------------------------------------
# Runsheets
# ---------------------------------------------------------------------------
@app.post("/api/v1/runsheets", status_code=201, tags=["dispatch"])
def create_runsheet(req: RunsheetRequest) -> dict:
    """Assign parcels to a driver. Each becomes OUT_FOR_DELIVERY.

    Three steps, in this order for a reason -- see the comments below.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    hub_suffix = req.hub_id.split("-")[-1]
    runsheet_id = f"RS-{today}-{hub_suffix}-{req.driver_id[-3:]}"

    # ---- STEP 1: validate EVERY AWB before writing anything -------------
    # If one is bad we reject the whole request rather than half-creating a
    # runsheet. Cheap to do, and it removes a whole class of partial state.
    for awb in req.awbs:
        try:
            cc.get_waybill(awb)
        except cc.ConsignmentError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---- STEP 2: create the runsheet and its stops locally --------------
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dispatch.runsheets
                (id, driver_id, driver_name, vehicle_id, hub_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (runsheet_id, req.driver_id, req.driver_name, req.vehicle_id, req.hub_id),
        )
        # Record the stops so the driver app can list them. ON CONFLICT makes
        # re-posting the same runsheet idempotent.
        for seq, awb in enumerate(req.awbs, start=1):
            cur.execute(
                """
                INSERT INTO dispatch.runsheet_items (runsheet_id, awb, sequence)
                VALUES (%s, %s, %s)
                ON CONFLICT (runsheet_id, awb) DO NOTHING
                """,
                (runsheet_id, awb, seq),
            )
        conn.commit()

    # ---- STEP 3: ask consignment to move each parcel --------------------
    # This can PARTIALLY fail. Without a message broker there is no way to make
    # "create runsheet" and "update N parcels" atomic, so we report both lists
    # honestly instead of pretending everything worked.
    assigned: list[str] = []
    failed: list[dict] = []
    for awb in req.awbs:
        try:
            cc.update_status(
                awb,
                "OUT_FOR_DELIVERY",
                req.hub_id,
                f"Assigned to {req.driver_name} ({runsheet_id})",
            )
            assigned.append(awb)
        except cc.ConsignmentError as e:
            log.error("could not assign awb=%s: %s", awb, e)
            failed.append({"awb": awb, "error": str(e)})

    return {
        "runsheet_id": runsheet_id,
        "driver": req.driver_name,
        "vehicle": req.vehicle_id,
        "hub_id": req.hub_id,
        "assigned": assigned,
        "failed": failed,
    }


@app.get("/api/v1/runsheets", tags=["driver"])
def list_runsheets(
    driver_id: str | None = Query(default=None, description="Filter to one driver"),
    status: str | None = Query(default=None, description="ACTIVE | CLOSED"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Runsheets, newest first. The driver app's home screen.

    Includes per-runsheet stop counts so the app can render progress without
    a second round trip per runsheet.
    """
    clauses, params = [], []
    if driver_id:
        clauses.append("r.driver_id = %s")
        params.append(driver_id)
    if status:
        clauses.append("r.status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.id, r.driver_id, r.driver_name, r.vehicle_id, r.hub_id,
                   r.status, r.created_at,
                   count(i.awb)                                          AS total,
                   count(*) FILTER (WHERE i.status = 'PENDING')          AS pending,
                   count(*) FILTER (WHERE i.status = 'DELIVERED')        AS delivered,
                   count(*) FILTER (WHERE i.status = 'RTO')              AS rto
            FROM dispatch.runsheets r
            LEFT JOIN dispatch.runsheet_items i ON i.runsheet_id = r.id
            {where}
            GROUP BY r.id
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (*params, limit),
        )
        rows = cur.fetchall()

    return {
        "count": len(rows),
        "runsheets": [
            {
                "runsheet_id": r[0],
                "driver_id": r[1],
                "driver_name": r[2],
                "vehicle_id": r[3],
                "hub_id": r[4],
                "status": r[5],
                "created_at": r[6].isoformat(),
                "stops": {"total": r[7], "pending": r[8], "delivered": r[9], "rto": r[10]},
            }
            for r in rows
        ],
    }


def _driver_filters(hub_id: str | None, status: str | None) -> tuple[str, list]:
    """Build the WHERE clause for the roster query.

    Split out of the handler so it is testable without a database -- the values
    always travel as bound parameters, never interpolated into the SQL, so the
    only thing f-string-formatted into the query is this fixed clause text.
    """
    clauses, params = [], []
    if hub_id:
        clauses.append("d.hub_id = %s")
        params.append(hub_id)
    if status and status.upper() != "ALL":
        clauses.append("d.status = %s")
        params.append(status.upper())
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


@app.get("/api/v1/drivers", tags=["driver"])
def list_drivers(
    hub_id: str | None = Query(default=None, description="Filter to one hub's roster"),
    # Defaults to ACTIVE so a caller that asks for nothing cannot accidentally
    # offer a driver who has left. "ALL" is the deliberate opt-out -- a blank
    # value cannot serve as one because the shared client's qs() strips empty
    # strings before they reach the wire.
    status: str = Query(default="ACTIVE", description='ACTIVE, INACTIVE, or "ALL"'),
) -> dict:
    """The driver roster. Powers the driver app's login picker and the admin
    console's assignment dropdown.

    Reads dispatch.drivers, NOT dispatch.runsheets. The older version derived
    this list from runsheets, which meant a driver did not exist until someone
    had already assigned them work -- so on a fresh database the picker was
    empty and there was no way to create the first runsheet.

    The runsheet count is still returned, via LEFT JOIN, so a driver with no
    work yet appears with `runsheets: 0` instead of not appearing at all.
    """
    where, params = _driver_filters(hub_id, status)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.driver_id, d.driver_name, d.vehicle_id, d.hub_id, d.phone,
                   d.status, count(r.id)
            FROM dispatch.drivers d
            LEFT JOIN dispatch.runsheets r ON r.driver_id = d.driver_id
            {where}
            GROUP BY d.driver_id, d.driver_name, d.vehicle_id, d.hub_id,
                     d.phone, d.status
            ORDER BY d.hub_id, d.driver_name
            """,
            params,
        )
        rows = cur.fetchall()
    return {
        "drivers": [
            {
                "driver_id": r[0],
                "driver_name": r[1],
                "vehicle_id": r[2],
                "hub_id": r[3],
                "phone": r[4],
                "status": r[5],
                "runsheets": r[6],
            }
            for r in rows
        ]
    }


@app.get("/api/v1/hubs", tags=["driver"])
def list_hubs() -> dict:
    """Hubs that have a roster, each with its drivers.

    Exists because the hub list was hardcoded in four separate places
    (admin console, hub app, merchant portal, simulator). Those copies are
    still there -- this is the source of truth for anything that needs to know
    which drivers and vehicles a hub actually has.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT hub_id, driver_id, driver_name, vehicle_id, status
            FROM dispatch.drivers
            ORDER BY hub_id, driver_name
            """
        )
        rows = cur.fetchall()

    hubs: dict[str, dict] = {}
    for hub_id, driver_id, driver_name, vehicle_id, status in rows:
        hub = hubs.setdefault(hub_id, {"hub_id": hub_id, "drivers": [], "vehicles": []})
        hub["drivers"].append(
            {
                "driver_id": driver_id,
                "driver_name": driver_name,
                "vehicle_id": vehicle_id,
                "status": status,
            }
        )
        if status == "ACTIVE":
            hub["vehicles"].append(vehicle_id)

    return {"hubs": list(hubs.values())}


@app.get("/api/v1/runsheets/{runsheet_id}", tags=["dispatch"])
def get_runsheet(runsheet_id: str) -> dict:
    """A runsheet, its stops, and every delivery attempt recorded against it."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, driver_id, driver_name, vehicle_id, hub_id, status, created_at
            FROM dispatch.runsheets WHERE id = %s
            """,
            (runsheet_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Runsheet {runsheet_id} not found")

        cur.execute(
            """
            SELECT awb, sequence, status
            FROM dispatch.runsheet_items
            WHERE runsheet_id = %s
            ORDER BY sequence
            """,
            (runsheet_id,),
        )
        items = cur.fetchall()

        cur.execute(
            """
            SELECT awb, outcome, reason, pod_type, pod_receiver, attempted_at
            FROM dispatch.delivery_attempts
            WHERE runsheet_id = %s
            ORDER BY attempted_at
            """,
            (runsheet_id,),
        )
        attempts = cur.fetchall()

    # Enrich each stop with consignee details from consignment-service. The
    # driver needs an address and a phone number, and dispatch does not own
    # that data -- so it asks, one call per stop.
    #
    # N calls for N stops is acceptable at runsheet size (<= 50) and keeps the
    # ownership boundary clean. If this ever became hot, the fix is a batch
    # endpoint on consignment, not a cross-schema SELECT.
    stops = []
    for awb, seq, item_status in items:
        stop = {"awb": awb, "sequence": seq, "status": item_status}
        try:
            p = cc.get_waybill(awb)
            stop.update(
                consignee_name=p["consignee_name"],
                consignee_phone=p["consignee_phone"],
                consignee_addr=p["consignee_addr"],
                payment_mode=p["payment_mode"],
                cod_amount=p["cod_amount"],
                parcel_status=p["current_status"],
            )
        except cc.ConsignmentError as e:
            # Degrade rather than fail: the driver still sees the AWB and can
            # work, which beats a blank screen.
            log.warning("could not enrich stop %s: %s", awb, e)
            stop["enrichment_error"] = str(e)
        stops.append(stop)

    return {
        "runsheet_id": row[0],
        "driver_id": row[1],
        "driver_name": row[2],
        "vehicle_id": row[3],
        "hub_id": row[4],
        "status": row[5],
        "created_at": row[6].isoformat(),
        "stops": stops,
        "attempts": [
            {
                "awb": a[0],
                "outcome": a[1],
                "reason": a[2],
                "pod_type": a[3],
                "pod_receiver": a[4],
                "attempted_at": a[5].isoformat(),
            }
            for a in attempts
        ],
    }


# ---------------------------------------------------------------------------
# GPS -- Redis only, never Postgres
# ---------------------------------------------------------------------------
@app.post("/api/v1/gps", status_code=202, tags=["tracking"])
def gps_ping(ping: GPSPing) -> dict:
    """Record a vehicle position.

    202 Accepted, not 201 Created -- the API is being honest that this write is
    deliberately non-durable. Hundreds of these arrive per minute and only the
    newest matters; writing them to Postgres would burn the whole IO budget of
    a db.t3.micro on data that is stale in ten seconds.
    """
    value = {
        "lat": ping.lat,
        "lon": ping.lon,
        "speed_kmph": ping.speed_kmph,
        "runsheet_id": ping.runsheet_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        redis_client().setex(
            f"vehicle:{ping.vehicle_id}:location", GPS_TTL_SECONDS, json.dumps(value)
        )
    except Exception as e:
        # Unlike the tracking cache, there is no fallback store for GPS --
        # Redis IS the store. Say so plainly rather than returning a false 202.
        log.error("gps write failed for %s: %s", ping.vehicle_id, e)
        raise HTTPException(status_code=503, detail="Location store unavailable")

    return {"accepted": True, "vehicle_id": ping.vehicle_id}


@app.get("/api/v1/vehicles", tags=["tracking"])
def list_vehicles() -> dict:
    """Every vehicle that has reported a position in the last hour.

    Uses SCAN, not KEYS. KEYS blocks the whole Redis server while it walks the
    keyspace -- fine with 3 vehicles, catastrophic with 300,000. SCAN iterates
    in small batches instead.
    """
    vehicles = []
    try:
        client = redis_client()
        for key in client.scan_iter(match="vehicle:*:location", count=100):
            raw = client.get(key)
            if not raw:
                continue  # expired between the scan and the read
            vehicle_id = key.split(":")[1]
            vehicles.append({"vehicle_id": vehicle_id, **json.loads(raw)})
    except Exception as e:
        log.error("vehicle scan failed: %s", e)
        raise HTTPException(status_code=503, detail="Location store unavailable")

    vehicles.sort(key=lambda v: v.get("recorded_at", ""), reverse=True)
    return {"count": len(vehicles), "vehicles": vehicles}


@app.get("/api/v1/vehicles/{vehicle_id}/location", tags=["tracking"])
def get_location(vehicle_id: str) -> dict:
    try:
        raw = redis_client().get(f"vehicle:{vehicle_id}:location")
    except Exception as e:
        log.error("gps read failed for %s: %s", vehicle_id, e)
        raise HTTPException(status_code=503, detail="Location store unavailable")

    if not raw:
        raise HTTPException(
            status_code=404, detail=f"No recent location for {vehicle_id}"
        )
    return {"vehicle_id": vehicle_id, **json.loads(raw)}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
@app.post("/api/v1/delivery", status_code=201, tags=["dispatch"])
def record_delivery(req: DeliveryRequest) -> dict:
    """Final outcome for a parcel: DELIVERED or RTO."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM dispatch.runsheets WHERE id = %s", (req.runsheet_id,))
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404, detail=f"Runsheet {req.runsheet_id} not found"
            )

        # Record locally FIRST: the attempt happened, whatever follows.
        cur.execute(
            """
            INSERT INTO dispatch.delivery_attempts
                (awb, runsheet_id, outcome, reason, pod_type, pod_receiver, pod_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                req.awb, req.runsheet_id, req.outcome, req.reason,
                req.pod_type, req.pod_receiver, req.pod_data,
            ),
        )
        # Close out the stop so the driver app stops showing it as pending.
        cur.execute(
            """
            UPDATE dispatch.runsheet_items
            SET status = %s
            WHERE runsheet_id = %s AND awb = %s
            """,
            (req.outcome, req.runsheet_id, req.awb),
        )
        conn.commit()

    try:
        cc.update_status(req.awb, req.outcome, remarks=req.reason)
    except cc.ConsignmentError as e:
        # 207 Multi-Status is the honest answer: the attempt was saved but the
        # parcel status was not updated. A 500 would imply nothing happened;
        # a 201 would claim everything worked. Neither is true.
        #
        # This gap is exactly what the transactional outbox in
        # docs/FleetPulse-Addon-Notification.md closes.
        log.error("delivery recorded but status update failed awb=%s: %s", req.awb, e)
        raise HTTPException(
            status_code=207,
            detail=f"Attempt saved, but parcel status update failed: {e}",
        )

    return {
        "awb": req.awb,
        "outcome": req.outcome,
        "runsheet_id": req.runsheet_id,
    }
