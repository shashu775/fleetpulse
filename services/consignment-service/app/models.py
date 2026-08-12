"""Pydantic models.

FastAPI uses these to validate incoming JSON and to generate the interactive
docs at /docs. Invalid input is rejected with a 422 before any handler code
runs, so the handlers never defend against bad types.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class BookingRequest(BaseModel):
    merchant_name: str = Field(..., min_length=1, max_length=120, examples=["Nykaa"])
    consignee_name: str = Field(..., min_length=1, max_length=120, examples=["Ravi Kumar"])
    consignee_phone: str = Field(..., min_length=10, max_length=15, examples=["9876543210"])
    consignee_addr: str = Field(..., min_length=1, examples=["12 MG Road, Bengaluru"])
    origin_hub: str = Field(..., examples=["HUB-BLR-01"])
    destination_hub: str = Field(..., examples=["HUB-DEL-03"])
    # gt=0 is what makes the "negative weight is rejected" test pass without
    # any checking code in the handler.
    weight_grams: int = Field(..., gt=0, le=50_000, examples=[900])
    payment_mode: Literal["PREPAID", "COD"]
    cod_amount: float = Field(default=0, ge=0)


class ScanRequest(BaseModel):
    """A warehouse hub scanning a parcel.

    Only the two hub-controlled transitions are allowed here. OUT_FOR_DELIVERY,
    DELIVERED and RTO belong to dispatch-service and arrive via PATCH .../status.
    """

    awb: str = Field(..., examples=["FP4820193756"])
    status: Literal["IN_TRANSIT", "ARRIVED_AT_FACILITY"]
    hub_id: str = Field(..., examples=["HUB-BLR-01"])
    remarks: Optional[str] = None


class StatusUpdate(BaseModel):
    """Internal -- sent by dispatch-service over HTTP."""

    status: Literal["OUT_FOR_DELIVERY", "DELIVERED", "RTO"]
    hub_id: Optional[str] = None
    remarks: Optional[str] = None
