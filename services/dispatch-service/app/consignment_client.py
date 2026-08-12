"""HTTP client for talking to consignment-service.

THIS FILE IS THE HEART OF THE PROJECT.

It is what makes FleetPulse a microservices system rather than one application
in two folders. dispatch-service could trivially run
`UPDATE consignment.waybills SET current_status = ...` -- same database, same
credentials, right there. It must not.

consignment-service owns the state machine. If dispatch wrote to those tables
directly, that rule would live in two places and eventually disagree. So the
only way dispatch changes a parcel is by asking, over HTTP.
"""

import logging
import os

import httpx

log = logging.getLogger("consignment_client")

BASE_URL = os.getenv("CONSIGNMENT_URL", "http://consignment-service:8000")

# Bounded timeouts are not optional. Without them one slow call would hang a
# worker indefinitely, and enough of those take the whole service down.
TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class ConsignmentError(Exception):
    """consignment-service said no, or could not be reached.

    We never guess on failure -- guessing here corrupts parcel state.
    """


def get_waybill(awb: str) -> dict:
    """Check an AWB exists before putting it on a runsheet."""
    try:
        r = httpx.get(f"{BASE_URL}/api/v1/waybills/{awb}", timeout=TIMEOUT)
    except httpx.RequestError as e:
        raise ConsignmentError(f"consignment-service unreachable: {e}") from e

    if r.status_code == 404:
        raise ConsignmentError(f"AWB {awb} does not exist")
    if r.status_code >= 400:
        raise ConsignmentError(
            f"consignment-service returned {r.status_code}: {r.text[:200]}"
        )
    return r.json()


def update_status(
    awb: str,
    status: str,
    hub_id: str | None = None,
    remarks: str | None = None,
) -> dict:
    """Tell consignment-service the parcel moved.

    Used for OUT_FOR_DELIVERY, DELIVERED and RTO -- the three transitions
    dispatch drives but does not perform.
    """
    payload = {"status": status, "hub_id": hub_id, "remarks": remarks}
    try:
        r = httpx.patch(
            f"{BASE_URL}/api/v1/waybills/{awb}/status", json=payload, timeout=TIMEOUT
        )
    except httpx.RequestError as e:
        raise ConsignmentError(f"consignment-service unreachable: {e}") from e

    if r.status_code == 404:
        raise ConsignmentError(f"AWB {awb} does not exist")
    if r.status_code == 409:
        # The state machine rejected it. Surface the reason verbatim -- it is
        # the most useful debugging information available.
        detail = r.json().get("detail", r.text[:200])
        raise ConsignmentError(f"Illegal status change for {awb}: {detail}")
    if r.status_code >= 400:
        raise ConsignmentError(
            f"consignment-service returned {r.status_code}: {r.text[:200]}"
        )

    log.info("updated awb=%s -> %s", awb, status)
    return r.json()
