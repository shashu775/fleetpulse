#!/usr/bin/env python3
"""
simulate_delhivery_scans.py -- fake traffic for FleetPulse.

Books AWBs, walks them through hub scans, assigns runsheets, streams GPS pings,
and delivers or RTOs each parcel -- exercising every endpoint in both services
and, critically, the cross-service HTTP call between them.

Run it with Docker (no local Python needed):

    docker compose --profile sim run --rm simulator --parcels 20

Or directly, if you do have Python:

    pip install -r simulator/requirements.txt
    python simulator/simulate_delhivery_scans.py --parcels 20
"""

import argparse
import os
import random
import sys
import time

import requests

# Defaults suit running from the host. docker-compose overrides these with the
# internal service names when the simulator runs inside the Compose network.
CONSIGNMENT = os.getenv("CONSIGNMENT_URL", "http://localhost:8001")
DISPATCH = os.getenv("DISPATCH_URL", "http://localhost:8002")

HUBS = ["HUB-BLR-01", "HUB-CHN-02", "HUB-HYD-01",
        "HUB-DEL-03", "HUB-MUM-01", "HUB-KOL-02"]

# Real coordinates so the GPS track looks plausible if you ever plot it.
HUB_COORDS = {
    "HUB-BLR-01": (12.9716, 77.5946),
    "HUB-CHN-02": (13.0827, 80.2707),
    "HUB-HYD-01": (17.3850, 78.4867),
    "HUB-DEL-03": (28.7041, 77.1025),
    "HUB-MUM-01": (19.0760, 72.8777),
    "HUB-KOL-02": (22.5726, 88.3639),
}

MERCHANTS = ["Nykaa", "Meesho", "Ajio", "FirstCry", "boAt", "Lenskart"]
NAMES = ["Ravi Kumar", "Priya Sharma", "Amit Patel", "Sneha Reddy",
         "Arjun Nair", "Kavya Iyer", "Rohit Verma"]
STREETS = ["MG Road", "Park Street", "Brigade Road", "Linking Road",
           "Anna Salai", "Banjara Hills"]
# Drivers are NOT hardcoded here any more -- dispatch.drivers is the roster, and
# a copy in this file would drift from it. Fetched per hub at runtime, because a
# runsheet must be worked by a driver who actually reports to that hub.
DRIVER_CACHE: dict[str, list[tuple[str, str, str]]] = {}
NDR_REASONS = ["Consignee unavailable", "Address incorrect",
               "Refused delivery", "Premises closed"]

OK, BAD = "[ok]", "[!!]"


# ---------------------------------------------------------------------------
def drivers_at(session: requests.Session, hub_id: str) -> list[tuple[str, str, str]]:
    """The ACTIVE roster for one hub, cached for the run.

    Returns [] if the hub has no drivers -- which on a database predating the
    dispatch.drivers table means init.sql has not been applied. The caller
    reports that rather than inventing a driver, because a made-up driver_id
    would then show up in the driver app's picker as a real person.
    """
    if hub_id in DRIVER_CACHE:
        return DRIVER_CACHE[hub_id]
    try:
        r = session.get(f"{DISPATCH}/api/v1/drivers", params={"hub_id": hub_id}, timeout=10)
        r.raise_for_status()
        roster = [
            (d["driver_id"], d["driver_name"], d["vehicle_id"]) for d in r.json()["drivers"]
        ]
    except Exception as e:
        print(f"  {BAD} could not load roster for {hub_id}: {e}")
        roster = []
    DRIVER_CACHE[hub_id] = roster
    return roster


def book_parcel(session: requests.Session) -> tuple[str, str, str] | None:
    """POST a booking. Returns (awb, origin_hub, destination_hub)."""
    origin, dest = random.sample(HUBS, 2)
    is_cod = random.random() < 0.6        # ~60% COD is realistic for India

    body = {
        "merchant_name": random.choice(MERCHANTS),
        "consignee_name": random.choice(NAMES),
        "consignee_phone": f"9{random.randint(10**8, 10**9 - 1)}",
        "consignee_addr": f"{random.randint(1, 999)}, {random.choice(STREETS)}",
        "origin_hub": origin,
        "destination_hub": dest,
        "weight_grams": random.randint(100, 5000),
        "payment_mode": "COD" if is_cod else "PREPAID",
        "cod_amount": round(random.uniform(299, 4999), 2) if is_cod else 0,
    }

    r = session.post(f"{CONSIGNMENT}/api/v1/waybills", json=body, timeout=10)
    if r.status_code != 201:
        print(f"  {BAD} booking failed: {r.status_code} {r.text[:120]}")
        return None

    awb = r.json()["awb"]
    print(f"  {OK} {awb}  {origin} -> {dest}  "
          f"{'COD' if is_cod else 'PREPAID':<7} {body['weight_grams']:>5} g")
    return awb, origin, dest


def hub_scan(session, awb: str, status: str, hub: str, remarks: str) -> bool:
    r = session.post(f"{CONSIGNMENT}/api/v1/scans", timeout=10,
                     json={"awb": awb, "status": status,
                           "hub_id": hub, "remarks": remarks})
    ok = r.status_code == 201
    suffix = "" if ok else f"   ({r.status_code} {r.text[:90]})"
    print(f"  {OK if ok else BAD} {awb}  {status:<22} @ {hub}{suffix}")
    return ok


def send_gps(session, vehicle: str, runsheet: str,
             origin: str, dest: str, n: int) -> int:
    """Interpolate a line between two hubs, one ping per step."""
    lat1, lon1 = HUB_COORDS[origin]
    lat2, lon2 = HUB_COORDS[dest]
    sent = 0
    for i in range(n):
        f = (i + 1) / n
        r = session.post(f"{DISPATCH}/api/v1/gps", timeout=10, json={
            "vehicle_id": vehicle,
            # Jitter so the track looks like a road, not a ruler.
            "lat": lat1 + (lat2 - lat1) * f + random.uniform(-0.01, 0.01),
            "lon": lon1 + (lon2 - lon1) * f + random.uniform(-0.01, 0.01),
            "speed_kmph": round(random.uniform(15, 55), 1),
            "runsheet_id": runsheet,
        })
        if r.status_code == 202:
            sent += 1
    print(f"  {OK if sent == n else BAD} {vehicle}  {sent}/{n} GPS pings accepted")
    return sent


def wait_for_services(session) -> bool:
    for name, url in (("consignment", CONSIGNMENT), ("dispatch", DISPATCH)):
        for attempt in range(30):
            try:
                if session.get(f"{url}/health", timeout=3).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if attempt == 0:
                print(f"  waiting for {name}-service at {url} ...")
            time.sleep(2)
        else:
            print(f"\nERROR: {name}-service never became healthy at {url}")
            print("Start the stack first:  docker compose up -d")
            return False
    return True


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="FleetPulse traffic simulator")
    ap.add_argument("--parcels", type=int, default=10, help="how many to create")
    ap.add_argument("--gps-pings", type=int, default=5, help="pings per runsheet")
    ap.add_argument("--delay", type=float, default=0.05,
                    help="seconds between API calls")
    ap.add_argument("--seed", type=int, help="fix the RNG for reproducible runs")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    session = requests.Session()
    if not wait_for_services(session):
        return 1

    bar = "=" * 66
    print(f"\n{bar}\n  FleetPulse simulator  --  {args.parcels} parcels\n{bar}")

    # ---- STEP 1 ---------------------------------------------------------
    print("\nSTEP 1  Booking parcels")
    parcels: list[tuple[str, str, str]] = []
    for _ in range(args.parcels):
        p = book_parcel(session)
        if p:
            parcels.append(p)
        time.sleep(args.delay)

    if not parcels:
        print("\nNothing was booked -- aborting.")
        return 1

    # ---- STEP 2 ---------------------------------------------------------
    print("\nSTEP 2  Line haul (origin hub -> destination hub)")
    for awb, origin, dest in parcels:
        hub_scan(session, awb, "IN_TRANSIT", origin, "Departed origin facility")
        time.sleep(args.delay)
        hub_scan(session, awb, "ARRIVED_AT_FACILITY", dest,
                 "Received at destination facility")
        time.sleep(args.delay)

    # ---- STEP 3 ---------------------------------------------------------
    print("\nSTEP 3  Runsheets and GPS  (dispatch -> consignment over HTTP)")
    out_for_delivery: list[tuple[str, str]] = []
    for start in range(0, len(parcels), 5):
        chunk = parcels[start:start + 5]
        dest = chunk[0][2]
        # The driver must belong to the hub the runsheet is raised at.
        roster = drivers_at(session, dest)
        if not roster:
            print(f"  {BAD} no drivers on the roster at {dest} -- "
                  f"apply db/init.sql to seed dispatch.drivers")
            continue
        driver_id, driver_name, vehicle = random.choice(roster)

        r = session.post(f"{DISPATCH}/api/v1/runsheets", timeout=20, json={
            "driver_id": driver_id,
            "driver_name": driver_name,
            "vehicle_id": vehicle,
            "hub_id": dest,
            "awbs": [awb for awb, _, _ in chunk],
        })
        if r.status_code != 201:
            print(f"  {BAD} runsheet failed: {r.status_code} {r.text[:140]}")
            continue

        rs = r.json()
        n_ok, n_bad = len(rs["assigned"]), len(rs["failed"])
        print(f"  {OK if not n_bad else BAD} {rs['runsheet_id']}  {driver_name:<14} "
              f"{n_ok} assigned, {n_bad} failed")
        for f in rs["failed"]:
            print(f"       -> {f['awb']}: {f['error'][:90]}")

        send_gps(session, vehicle, rs["runsheet_id"], chunk[0][1], dest, args.gps_pings)
        out_for_delivery += [(awb, rs["runsheet_id"]) for awb in rs["assigned"]]

    # ---- STEP 4 ---------------------------------------------------------
    print("\nSTEP 4  Final delivery")
    n_delivered = n_rto = n_failed = 0
    for awb, runsheet in out_for_delivery:
        # ~88% delivered / ~12% RTO mirrors real Indian e-commerce rates.
        if random.random() < 0.88:
            outcome, reason = "DELIVERED", "Handed to consignee"
        else:
            outcome, reason = "RTO", random.choice(NDR_REASONS)

        r = session.post(f"{DISPATCH}/api/v1/delivery", timeout=10, json={
            "awb": awb, "runsheet_id": runsheet,
            "outcome": outcome, "reason": reason,
        })
        if r.status_code == 201:
            n_delivered += outcome == "DELIVERED"
            n_rto += outcome == "RTO"
            print(f"  {OK} {awb}  {outcome}")
        else:
            n_failed += 1
            print(f"  {BAD} {awb}  {outcome} -> {r.status_code} {r.text[:90]}")
        time.sleep(args.delay)

    # ---- Summary --------------------------------------------------------
    total = len(parcels)
    rate = (100 * n_delivered / (n_delivered + n_rto)) if (n_delivered + n_rto) else 0
    print(f"\n{bar}")
    print(f"  Booked {total}   Delivered {n_delivered}   RTO {n_rto}   Failed {n_failed}")
    print(f"  Delivery success rate: {rate:.1f}%")
    print(f"\n  Inspect one parcel's full history:")
    print(f"    curl {CONSIGNMENT}/api/v1/waybills/{parcels[0][0]}/history")
    print(f"  See the cache working (MISS then HIT):")
    print(f"    curl {CONSIGNMENT}/api/v1/waybills/{parcels[0][0]}")
    print(f"{bar}\n")

    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
