"""Pydantic models for dispatch-service."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class RunsheetRequest(BaseModel):
    driver_id: str = Field(..., examples=["DRV-4417"])
    driver_name: str = Field(..., min_length=1, max_length=120, examples=["Suresh Yadav"])
    vehicle_id: str = Field(..., examples=["KA01AB1234"])
    hub_id: str = Field(..., examples=["HUB-BLR-01"])
    # A driver carries a bounded number of parcels; the cap also stops one
    # request making 50+ sequential HTTP calls to consignment-service.
    awbs: list[str] = Field(..., min_length=1, max_length=50)


class GPSPing(BaseModel):
    vehicle_id: str = Field(..., examples=["KA01AB1234"])
    # Bounds validation means a GPS unit reporting garbage is rejected at the
    # edge rather than stored and confusing a map later.
    lat: float = Field(..., ge=-90, le=90, examples=[12.9716])
    lon: float = Field(..., ge=-180, le=180, examples=[77.5946])
    speed_kmph: float = Field(default=0, ge=0, le=200)
    runsheet_id: Optional[str] = None


class DeliveryRequest(BaseModel):
    awb: str = Field(..., examples=["FP4820193756"])
    runsheet_id: str = Field(..., examples=["RS-20260813-01-417"])
    outcome: Literal["DELIVERED", "RTO"]
    reason: Optional[str] = None

    # ---- Proof of delivery, captured by the driver app ----
    pod_type: Optional[Literal["OTP", "SIGNATURE", "PHOTO"]] = None
    pod_receiver: Optional[str] = Field(
        default=None, max_length=120, description="Who actually took the parcel"
    )
    # For SIGNATURE this is a data: URL from the canvas; for OTP, the code the
    # consignee read out. Capped because an unbounded TEXT field accepting
    # base64 images is a denial-of-service waiting to happen.
    pod_data: Optional[str] = Field(default=None, max_length=200_000)
