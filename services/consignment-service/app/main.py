"""Consignment & Hub Service.

Owns the parcel record and its status. This service is the SYSTEM OF RECORD:
every status change in the business must go through it, which is why the state
machine below lives here and nowhere else.

dispatch-service drives the last three transitions but does not perform them --
it calls PATCH /api/v1/waybills/{awb}/status.
"""

import logging
import os
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .cache import cache_delete, cache_get, cache_set
from .db import close_pool, get_conn, init_pool
from .labels import render_label
from .models import BookingRequest, ScanRequest, StatusUpdate

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("consignment")

# ---------------------------------------------------------------------------
# THE STATE MACHINE.
#
# This dict is the single enforcement point for legal parcel movement. An
# attempt to make an illegal move returns 409 rather than silently corrupting
# the record -- which catches retried requests, out-of-order scans, and
# double-submitting driver apps.
#
# dispatch-service could technically UPDATE these rows directly (same database,
# same credentials). It must not. If it did, this rule would live in two places
# and eventually disagree.
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "MANIFESTED": {"IN_TRANSIT"},
    "IN_TRANSIT": {"ARRIVED_AT_FACILITY"},
    "ARRIVED_AT_FACILITY": {"IN_TRANSIT", "OUT_FOR_DELIVERY"},
    "OUT_FOR_DELIVERY": {"DELIVERED", "RTO"},
    "DELIVERED": set(),  # terminal
    "RTO": set(),        # terminal
}

CACHE_TTL_SECONDS = 300


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    log.info("consignment-service ready")
    yield
    close_pool()


app = FastAPI(
    title="FleetPulse - Consignment & Hub Service",
    description=(
        "Waybill booking, shipping labels, and warehouse hub scans. "
        "This service is the system of record for parcel status."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness. Deliberately does NOT check the database.

    A health check that depends on Postgres would turn a brief database blip
    into every container restarting at once.
    """
    return {"status": "ok", "service": "consignment"}


def generate_awb() -> str:
    """Delhivery-style airway bill number, e.g. FP4820193756."""
    return f"FP{random.randint(10**9, 10**10 - 1)}"


def _fetch_waybill(awb: str) -> dict | None:
    """Read one waybill straight from Postgres (no cache)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT awb, merchant_name, consignee_name, consignee_phone,
                   consignee_addr, origin_hub, destination_hub, weight_grams,
                   payment_mode, cod_amount, current_status, created_at, updated_at
            FROM consignment.waybills
            WHERE awb = %s
            """,
            (awb,),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return {
        "awb": row[0],
        "merchant_name": row[1],
        "consignee_name": row[2],
        "consignee_phone": row[3],
        "consignee_addr": row[4],
        "origin_hub": row[5],
        "destination_hub": row[6],
        "weight_grams": row[7],
        "payment_mode": row[8],
        "cod_amount": float(row[9]),
        "current_status": row[10],
        "created_at": row[11].isoformat(),
        "updated_at": row[12].isoformat(),
    }


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------
@app.post("/api/v1/waybills", status_code=201, tags=["booking"])
def create_waybill(req: BookingRequest) -> dict:
    """Book a shipment and issue an AWB number."""
    awb = generate_awb()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO consignment.waybills
                (awb, merchant_name, consignee_name, consignee_phone, consignee_addr,
                 origin_hub, destination_hub, weight_grams, payment_mode, cod_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                awb, req.merchant_name, req.consignee_name, req.consignee_phone,
                req.consignee_addr, req.origin_hub, req.destination_hub,
                req.weight_grams, req.payment_mode, req.cod_amount,
            ),
        )
        # Same transaction as the INSERT above: a parcel always has at least
        # one history row. If this failed on its own, the audit trail would
        # silently have holes.
        cur.execute(
            """
            INSERT INTO consignment.scan_events (awb, status, hub_id, remarks)
            VALUES (%s, 'MANIFESTED', %s, 'Shipment booked')
            """,
            (awb, req.origin_hub),
        )
        conn.commit()

    log.info("booked awb=%s origin=%s dest=%s", awb, req.origin_hub, req.destination_hub)
    return {
        "awb": awb,
        "status": "MANIFESTED",
        "tracking_url": f"/api/v1/waybills/{awb}",
        "label_url": f"/api/v1/waybills/{awb}/label",
    }


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
@app.get("/api/v1/waybills/{awb}", tags=["tracking"])
def get_waybill(awb: str) -> dict:
    """Tracking lookup -- the hot path.

    Reads Redis first. The `_cache` field in the response makes the cache
    visible so you can demonstrate it working.
    """
    cached = cache_get(f"awb:{awb}")
    if cached is not None:
        return {**cached, "_cache": "HIT"}

    result = _fetch_waybill(awb)
    if result is None:
        raise HTTPException(status_code=404, detail=f"AWB {awb} not found")

    # Cache the clean record; the _cache marker is added afterwards so it is
    # never itself stored.
    cache_set(f"awb:{awb}", result, CACHE_TTL_SECONDS)
    return {**result, "_cache": "MISS"}


@app.get("/api/v1/waybills", tags=["tracking"])
def list_waybills(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, description="Filter by current status"),
) -> dict:
    """Recent waybills, newest first. Powers the dashboard.

    Deliberately capped at 100 -- an uncapped list endpoint is how you
    accidentally SELECT a million rows into memory.
    """
    where, params = "", []
    if status:
        where = "WHERE current_status = %s"
        params.append(status)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT awb, merchant_name, consignee_name, origin_hub, destination_hub,
                   current_status, payment_mode, cod_amount, created_at, updated_at
            FROM consignment.waybills
            {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()

        cur.execute(
            f"SELECT count(*) FROM consignment.waybills {where}",
            tuple(params),
        )
        total = cur.fetchone()[0]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "waybills": [
            {
                "awb": r[0],
                "merchant_name": r[1],
                "consignee_name": r[2],
                "origin_hub": r[3],
                "destination_hub": r[4],
                "current_status": r[5],
                "payment_mode": r[6],
                "cod_amount": float(r[7]),
                "created_at": r[8].isoformat(),
                "updated_at": r[9].isoformat(),
            }
            for r in rows
        ],
    }


@app.get("/api/v1/stats", tags=["tracking"])
def get_stats() -> dict:
    """Aggregate counts for the dashboard."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_status, count(*)
            FROM consignment.waybills
            GROUP BY current_status
            """
        )
        by_status = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT count(*) FROM consignment.waybills
            WHERE created_at >= date_trunc('day', now())
            """
        )
        booked_today = cur.fetchone()[0]

    total = sum(by_status.values())
    delivered = by_status.get("DELIVERED", 0)
    rto = by_status.get("RTO", 0)
    finished = delivered + rto

    return {
        "total": total,
        "booked_today": booked_today,
        "by_status": by_status,
        "in_flight": total - finished,
        # Guard the divide -- an empty database must not 500 the dashboard.
        "delivery_success_rate": round(100 * delivered / finished, 1) if finished else None,
    }


@app.get("/api/v1/waybills/{awb}/history", tags=["tracking"])
def get_history(awb: str) -> dict:
    """Every scan recorded for this parcel, newest first."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM consignment.waybills WHERE awb = %s", (awb,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"AWB {awb} not found")

        cur.execute(
            """
            SELECT status, hub_id, remarks, scanned_at
            FROM consignment.scan_events
            WHERE awb = %s
            ORDER BY scanned_at DESC, id DESC
            """,
            (awb,),
        )
        rows = cur.fetchall()

    return {
        "awb": awb,
        "scans": [
            {
                "status": r[0],
                "hub_id": r[1],
                "remarks": r[2],
                "scanned_at": r[3].isoformat(),
            }
            for r in rows
        ],
    }


@app.get(
    "/api/v1/waybills/{awb}/label",
    response_class=HTMLResponse,
    tags=["booking"],
)
def get_label(awb: str) -> str:
    """Printable shipping label. Open in a browser to see it rendered."""
    data = _fetch_waybill(awb)
    if data is None:
        raise HTTPException(status_code=404, detail=f"AWB {awb} not found")
    return render_label(data)


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------
def _apply_transition(
    awb: str, new_status: str, hub_id: str | None, remarks: str | None
) -> dict:
    """Validate and apply one status transition. Shared by /scans and PATCH."""
    with get_conn() as conn, conn.cursor() as cur:
        # FOR UPDATE locks the row for the duration of the transaction, so two
        # concurrent scans for the same parcel cannot both read the old status
        # and both decide their transition is legal.
        cur.execute(
            "SELECT current_status FROM consignment.waybills WHERE awb = %s FOR UPDATE",
            (awb,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"AWB {awb} not found")

        current = row[0]
        if new_status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot move AWB {awb} from {current} to {new_status}",
            )

        cur.execute(
            """
            UPDATE consignment.waybills
            SET current_status = %s, updated_at = now()
            WHERE awb = %s
            """,
            (new_status, awb),
        )
        cur.execute(
            """
            INSERT INTO consignment.scan_events (awb, status, hub_id, remarks)
            VALUES (%s, %s, %s, %s)
            """,
            (awb, new_status, hub_id, remarks),
        )
        conn.commit()

    # Invalidate only AFTER the transaction committed.
    cache_delete(f"awb:{awb}")

    log.info("transition awb=%s %s -> %s hub=%s", awb, current, new_status, hub_id)
    return {"awb": awb, "previous_status": current, "new_status": new_status}


@app.post("/api/v1/scans", status_code=201, tags=["hub-operations"])
def record_scan(req: ScanRequest) -> dict:
    """A warehouse hub scans a parcel: IN_TRANSIT or ARRIVED_AT_FACILITY."""
    return _apply_transition(req.awb, req.status, req.hub_id, req.remarks)


@app.patch("/api/v1/waybills/{awb}/status", tags=["internal"])
def update_status(awb: str, req: StatusUpdate) -> dict:
    """Internal endpoint -- called by dispatch-service over HTTP.

    This is the boundary that keeps the state machine enforceable in one place.
    In a public deployment this would be authenticated; here the security group
    keeps it off the internet.
    """
    return _apply_transition(awb, req.status, req.hub_id, req.remarks)
