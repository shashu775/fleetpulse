# Add-On: OTP-Verified Delivery

Turn the proof-of-delivery fields FleetPulse already captures into proof that actually proves
something — **a one-time code sent to the consignee, verified server-side before the parcel is
marked delivered.**

Prerequisite: **[FleetPulse-Addon-Notification.md](FleetPulse-Addon-Notification.md) built first.**
This design sends the OTP as an event on that add-on's transactional outbox rather than bolting a
sender onto `consignment-service`. Nothing here is implementable until the notification service
exists — see [§1.1](#11-why-this-waits-for-the-outbox) for why that ordering is the point rather
than an inconvenience.

Also assumes [FleetPulse-Architecture.md](FleetPulse-Architecture.md) read, in particular the rule
that `dispatch-service` never writes the `consignment` schema.

---

## 0. The interesting problem

FleetPulse captures proof of delivery and verifies none of it.

The driver app has a complete POD modal — outcome, OTP or signature, receiver name.
`dispatch.delivery_attempts` has `pod_type`, `pod_receiver` and `pod_data` columns.
`POST /api/v1/delivery` stores whatever arrives. It all looks finished. It is theatre:

| Piece | Expected | Actual |
|---|---|---|
| Generate a code | consignment issues one per parcel | **nothing, anywhere, ever** |
| Send it to the consignee | SMS to `consignee_phone` | **no sender exists** |
| Verify it at the door | server-side comparison | **`/^\d{4,6}$/` in the browser** |
| Store it | hashed | **plaintext in `pod_data`** |

The only check in the system is client-side (`apps/driver-app/app.js`, in the `#pod-submit`
handler). Server-side, `pod_data` is `Optional[str]` with `max_length=200_000` and no comparison
against anything at all. So **any six digits complete a delivery — and so does omitting the field
entirely**, since every POD field is optional in `DeliveryRequest`.

> **This is worse than having no OTP.** A field nobody fills in is obviously empty. A field that
> is always filled and never checked produces an audit trail that *looks* like evidence — a
> plaintext code and a receiver name against every delivered parcel, none of it meaning anything.
> When a merchant disputes a delivery, that trail will be produced as proof, and it is not proof.

### 0.1 What is *not* broken

`pod_receiver` accepts any name, and that is correct. Parcels legitimately go to neighbours,
security guards, family members and building reception — the column comment already says
`-- who actually took it`, not "consignee". Constraining it to match `consignee_name` would break
the common case.

The design always intended a division of labour:

- **`pod_receiver`** answers *who took it* — a record, deliberately unconstrained.
- **`pod_data`** answers *were they entitled to* — this was meant to be the proof.

Only the second half is missing. Do not "fix" the first.

### 0.2 The flow being built

```
merchant books                    → MANIFESTED
hub scans                         → IN_TRANSIT → ARRIVED_AT_FACILITY
runsheet created                  → OUT_FOR_DELIVERY  ← code issued HERE
                                                       ← outbox event → SMS to consignee
driver arrives, consignee reads out the code
driver enters it                  → verified server-side → DELIVERED
wrong code                        → 401, nothing recorded
```

---

## 1. Where each piece lives

**`consignment-service` owns the OTP**, because it owns parcel state. It already owns
`ALLOWED_TRANSITIONS` and is the only service allowed to move a parcel between statuses. An OTP is
a precondition on the `OUT_FOR_DELIVERY → DELIVERED` edge, so it belongs to whoever owns that edge.

**`dispatch-service` never validates locally.** It holds the code the driver typed, and it asks
consignment whether that code is right — over HTTP, through `app/consignment_client.py`, exactly as
it already asks for status changes. It never reads `consignment.delivery_otps`, even though the
same database and the same credentials make that trivially possible.

> **This is the rule that defines the codebase, applied to a new feature.** Two places that can
> decide whether a delivery is authorised will eventually disagree, and the disagreement will be
> discovered by a customer who did not receive a parcel the system says was delivered. Keep one
> decision point.

### 1.1 Why this waits for the outbox

The code has to reach the consignee's phone. There are three ways to do that and only one is
acceptable:

| # | Approach | Problem |
|---|---|---|
| 1 | `consignment` calls an SMS API inline, in the request | A slow SMS vendor makes runsheet creation slow; a down vendor makes it **fail**. Ops cannot dispatch parcels because a third party is having a bad day. |
| 2 | `consignment` fires a background thread | Lost on restart, no retry, no record of what was sent. Invisible when it breaks. |
| 3 | **Outbox event, worker delivers** | Send is durable, retryable and auditable, and issuing the code cannot fail because of the vendor. |

Option 1 is the same mistake `Addon-Notification.md` §0.1 already rejects for merchant webhooks,
and it is more serious here: a parcel that goes `OUT_FOR_DELIVERY` without its code being sent is
a parcel that **cannot be delivered at all** under this design. The send has to be durable, which
means it has to be the outbox, which means the notification service comes first.

---

## 2. Schema

```sql
-- db/init.sql — append
CREATE TABLE IF NOT EXISTS consignment.delivery_otps (
    awb          VARCHAR(20)  PRIMARY KEY REFERENCES consignment.waybills(awb),
    -- HMAC-SHA256 hex. The code itself is NEVER stored -- see §6.
    otp_hash     VARCHAR(64)  NOT NULL,
    issued_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ  NOT NULL,
    -- Failed verifications. At MAX_ATTEMPTS the code is dead and must be reissued.
    attempts     INTEGER      NOT NULL DEFAULT 0,
    -- Single use: set on the verification that succeeds.
    consumed_at  TIMESTAMPTZ
);
```

A separate table, not three more columns on `waybills`. Two reasons worth the extra join:

- **`waybills` is the hot read.** Every tracking view, every label render and every cache miss
  selects that row. OTP bookkeeping — an attempt counter that changes on failure, an expiry
  nobody tracking a parcel cares about — has no business being carried by all of it.
- **`consumed_at` gives single-use for free.** As a nullable column on a dedicated row it is one
  `UPDATE ... WHERE consumed_at IS NULL`; as a column on `waybills` it is another piece of
  delivery state mixed into the parcel record, next to `current_status`, inviting exactly the
  confusion about which one is authoritative that the `scan_events` / `current_status` split
  already has to explain.

The `REFERENCES consignment.waybills(awb)` FK is fine and does **not** violate the no-cross-schema-FK
rule — both tables are in `consignment`. That rule exists to keep a future service split possible;
this FK is entirely inside one service's schema.

> **⚠️ This migration is genuinely safe, unlike most in this project.** `CREATE TABLE IF NOT
> EXISTS` on a *brand new* table is idempotent, so you can append it to `db/init.sql` **and** run
> that one statement against the running database:
>
> ```bash
> docker compose exec -T postgres psql -U fleetadmin -d fleetpulse < db/init.sql
> ```
>
> No `docker compose down -v`, no data loss. The traps documented in CLAUDE.md — `init.sql` only
> running on a fresh volume, `POSTGRES_PASSWORD` only applying to a fresh volume — bite when you
> change *existing* objects. Adding a new one is the easy case. Say so out loud, because the
> reflex from those gotchas is to reach for `down -v` and destroy every parcel you have.

---

## 3. Generation

### 3.1 The pure helpers

Everything with no I/O goes in its own module, so it can be unit-tested with no database and no
Redis — see [§8](#8-testing-the-failure-paths).

```python
# services/consignment-service/app/otp.py
"""One-time delivery codes.

Pure functions only -- no database, no Redis, no clock beyond what is passed in.
That is what lets the tests run with no infrastructure at all.
"""

import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256

OTP_DIGITS = 6
TTL_HOURS = 4
MAX_ATTEMPTS = 5


def _secret() -> bytes:
    s = os.getenv("OTP_SECRET")
    if not s:
        # Same posture as db.py: name the variable and the fix, fail loudly.
        raise RuntimeError("OTP_SECRET is not set -- add it to .env (see .env.example)")
    return s.encode()


def generate() -> str:
    """A cryptographically random 6-digit code, zero-padded.

    secrets, not random: random is a Mersenne Twister seeded predictably enough
    that observing a few codes can reveal the rest.
    """
    return f"{secrets.randbelow(10 ** OTP_DIGITS):0{OTP_DIGITS}d}"


def hash_code(code: str) -> str:
    """Keyed hash. See §6 for why a bare sha256 would be useless here."""
    return hmac.new(_secret(), code.encode(), sha256).hexdigest()


def verify_hash(code: str, expected_hash: str) -> bool:
    # compare_digest, not ==: a short-circuiting comparison leaks how many
    # leading characters were correct through timing.
    return hmac.compare_digest(hash_code(code), expected_hash)


def expiry_from(now: datetime) -> datetime:
    return now + timedelta(hours=TTL_HOURS)


def is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)) >= expires_at
```

### 3.2 The hook — and the correction it carries

Issue the code inside the existing status transaction, when and only when the parcel reaches
`OUT_FOR_DELIVERY`.

> **⚠️ Hook `_apply_transition()`, not `record_scan()`.**
> `Addon-Notification.md` §3 shows the outbox producer being wired into `record_scan()`. **That
> guidance is now stale.** The code has since been refactored: `services/consignment-service/app/main.py`
> defines `_apply_transition(awb, new_status, hub_id, remarks)`, shared by **both**
> `POST /api/v1/scans` **and** the internal `PATCH /api/v1/waybills/{awb}/status`.
>
> Hooking `record_scan` alone would miss every parcel moved by runsheet creation — and since
> `dispatch` moves parcels to `OUT_FOR_DELIVERY` through that PATCH, it would miss **every OTP the
> feature exists to issue**. Fix the notification add-on's producer placement at the same time.

```python
# services/consignment-service/app/main.py — inside _apply_transition(),
# after the scan_events INSERT and BEFORE conn.commit()

        if new_status == "OUT_FOR_DELIVERY":
            code = otp.generate()
            cur.execute(
                """
                INSERT INTO consignment.delivery_otps (awb, otp_hash, expires_at)
                VALUES (%s, %s, %s)
                -- Re-dispatch of a parcel that came back to the hub reissues:
                -- new code, new expiry, attempt counter reset, unconsumed.
                ON CONFLICT (awb) DO UPDATE SET
                    otp_hash    = EXCLUDED.otp_hash,
                    issued_at   = now(),
                    expires_at  = EXCLUDED.expires_at,
                    attempts    = 0,
                    consumed_at = NULL
                """,
                (awb, otp.hash_code(code), otp.expiry_from(datetime.now(timezone.utc))),
            )
            outbox.enqueue(
                cur,                                   # SAME cursor, SAME transaction
                event_key=f"{awb}:OTP_ISSUED:{issued_seq}",
                awb=awb,
                merchant_name=merchant_name,
                event_type="OTP_ISSUED",
                payload={
                    "awb": awb,
                    "consignee_phone": consignee_phone,
                    "code": code,                      # plaintext, deliberately -- see §4
                },
            )

        conn.commit()   # status, scan event, OTP and send-request land together, or none do
```

Both statements share the caller's cursor, so the guarantee is the same one the outbox pattern
exists to provide: **a parcel cannot go out for delivery without its code being issued and its
send being queued.** All four rows commit together or none do.

`merchant_name` and `consignee_phone` come from widening the existing `SELECT ... FOR UPDATE` at
the top of `_apply_transition`, which currently fetches only `current_status`.

> **`event_key` cannot be `f"{awb}:OTP_ISSUED"` alone.** The outbox has
> `UNIQUE (event_key)` with `ON CONFLICT DO NOTHING`, which is exactly right for
> "same parcel + same status = same event". But a reissued code is a genuinely *new* send — a
> parcel returned to the hub and sent out again needs its new code delivered. A bare key would
> silently drop it and the consignee would be quoted a code that no longer works. Include the
> issue sequence (or `issued_at`) in the key.

### 3.3 Issue at dispatch, never at booking

Generate when the parcel goes out for delivery, not when it is booked. A code created at booking
sits in the database for the entire line haul — days — with no compensating benefit, widening the
window in which a database dump is useful to an attacker. The consignee also cannot reasonably be
expected to keep a code from Tuesday for a Friday delivery.

---

## 4. Sending

The notification worker grows an SMS channel beside its webhook delivery. `OTP_ISSUED` events are
routed to it by `event_type`; everything else about the worker loop, the backoff schedule and the
dead-lettering is unchanged from `Addon-Notification.md` §4.2.

```python
# services/notification-service/app/sms.py
"""Stub SMS sender.

Local and CI runs log the code instead of sending it. Swapping in a real vendor
is one function, and the outbox retry/backoff already wraps it.
"""

import logging
import os

log = logging.getLogger("sms")


def send(phone: str, code: str) -> None:
    if os.getenv("SMS_PROVIDER", "stub") == "stub":
        log.info("SMS to %s: Your FleetPulse delivery code is %s", phone, code)
        return
    raise NotImplementedError("No real SMS provider is wired up yet")
```

Read the code during local testing with:

```bash
docker compose logs -f notification-service | grep "delivery code"
```

> **⚠️ The outbox row is now sensitive.** `payload` carries the **plaintext** code — it has to,
> since that is what gets sent, and the hash is one-way. That means `notification.outbox` is the
> one place in the system holding usable codes, sitting in a table designed to *retain* processed
> rows for audit.
>
> Two consequences, both non-optional:
>
> 1. **Scrub on completion.** When the worker sets `processed_at`, it must also
>    `SET payload = payload - 'code'` in the same `UPDATE`. The audit trail keeps the AWB, the
>    phone number and the timestamp; it does not keep the code.
> 2. **Never log the payload wholesale.** The worker's existing error paths log `payload` on
>    failure. Redact `code` there, or a failed send writes the code to stdout, into
>    `docker compose logs`, and on a cluster into whatever aggregates them.

---

## 5. Verification

### 5.1 The endpoint

```python
# services/consignment-service/app/main.py

@app.post("/api/v1/waybills/{awb}/verify-otp", tags=["internal"])
def verify_otp(awb: str, req: VerifyOtpRequest) -> dict:
    """Check a delivery code. Called by dispatch-service, never by a browser."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT otp_hash, expires_at, attempts, consumed_at
            FROM consignment.delivery_otps
            WHERE awb = %s
            FOR UPDATE
            """,
            (awb,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"No delivery code issued for AWB {awb}")

        otp_hash, expires_at, attempts, consumed_at = row

        if consumed_at is not None:
            raise HTTPException(409, "This code has already been used")
        if attempts >= otp.MAX_ATTEMPTS:
            raise HTTPException(429, "Too many incorrect attempts -- reissue required")
        if otp.is_expired(expires_at):
            raise HTTPException(410, "This code has expired -- reissue required")

        if not otp.verify_hash(req.code, otp_hash):
            # Count the failure in the SAME transaction as the read, so five
            # parallel guesses cannot all observe attempts=0.
            cur.execute(
                "UPDATE consignment.delivery_otps SET attempts = attempts + 1 WHERE awb = %s",
                (awb,),
            )
            conn.commit()
            remaining = otp.MAX_ATTEMPTS - (attempts + 1)
            raise HTTPException(401, f"Incorrect code -- {remaining} attempt(s) remaining")

        cur.execute(
            "UPDATE consignment.delivery_otps SET consumed_at = now() WHERE awb = %s",
            (awb,),
        )
        conn.commit()

    return {"awb": awb, "verified": True}
```

`FOR UPDATE` matters for the same reason it matters in `_apply_transition`: without the row lock,
concurrent guesses each read `attempts` before any of them writes, and the cap does nothing.

Note the status codes are distinct on purpose — `401` wrong, `410` expired, `429` locked out,
`409` already used. The driver needs to know which, because the remedy differs: try again, ask ops
to reissue, or stop.

### 5.2 The client call

```python
# services/dispatch-service/app/consignment_client.py

def verify_otp(awb: str, code: str) -> None:
    """Raise ConsignmentError if the code is not accepted. Returns None on success."""
    r = httpx.post(f"{BASE_URL}/api/v1/waybills/{awb}/verify-otp",
                   json={"code": code}, timeout=TIMEOUT)
    if r.status_code == 200:
        return
    detail = r.json().get("detail", r.text[:200])
    raise OtpRejected(detail, status=r.status_code)
```

`OtpRejected` subclasses the existing `ConsignmentError`, so callers that only care about "the
cross-service call failed" keep working unchanged.

### 5.3 Ordering — verify before the write

This is the part that is easy to get wrong, because it reverses an existing, deliberate decision.

`record_delivery` currently comments: *"Record locally FIRST: the attempt happened, whatever
follows."* That remains correct for the **status update** and is why the endpoint returns 207 when
consignment is unreachable after the attempt is stored. It is **not** correct for OTP
verification. An unverified code is not an attempt that happened — it is an attempt that must not
be recorded as a delivery.

```
# services/dispatch-service/app/main.py — record_delivery()

1. runsheet exists?                          -> else 404
2. outcome == DELIVERED and pod_type == OTP?
       cc.verify_otp(awb, pod_data)          -> else 401/410/429/409, NOTHING written
3. INSERT delivery_attempts
   UPDATE runsheet_items                     -> commit
4. cc.update_status(awb, outcome)            -> 207 if this fails  (unchanged)
```

Step 2 sits **above** step 3. A rejected code leaves no `delivery_attempts` row at all.

> **Where the failed-attempt audit trail lives.** It is tempting to record rejected codes in
> `delivery_attempts` "for the audit trail". Don't — that table means *deliveries that were
> attempted*, and filling it with rejections makes every query against it wrong, including the
> `stops.delivered` counts the driver app and admin console render. The count of failures lives in
> `delivery_otps.attempts`, which is where the rate limit needs it anyway.

> **RTO is not verified.** A parcel coming back has no consignee to read out a code. Step 2 is
> guarded on `outcome == DELIVERED`; `RTO` skips it entirely, exactly as it already skips POD
> capture in the driver app.

---

## 6. Why the hashing has to be keyed

The security section, and the part most worth understanding.

**A bare `sha256(code)` would be useless here.** The keyspace is six digits — 10⁶, one million
candidates. Anyone with a database dump can hash all million in about a second on a laptop and
recover every outstanding code. Password hashing intuitions do not transfer: bcrypt and Argon2 are
slow *because* passwords have entropy worth protecting with a work factor. A six-digit number has
19.9 bits. No work factor saves it.

What actually provides the security is a **secret the attacker does not have**:

```python
hmac.new(OTP_SECRET, code.encode(), sha256).hexdigest()
```

With `OTP_SECRET` held in the environment and not the database, a dump of `delivery_otps` is
inert — the attacker cannot precompute the space without the key. That is the whole mechanism.

The other four controls are what keep an **online** attacker out, since they can guess against the
live endpoint without any dump at all:

| Control | Value | Without it |
|---|---|---|
| `MAX_ATTEMPTS` | 5 | 10⁶ guesses against the API succeeds eventually |
| `expires_at` | 4 hours | An old code stays valid indefinitely |
| `consumed_at` | single use | A code seen once is reusable forever |
| `compare_digest` | constant time | Timing reveals correct leading digits |

`OTP_SECRET` goes in `.env.example` with a safe placeholder in the same change as the code that
reads it — the file is the contract. Generate a real one with
`python -c "import secrets; print(secrets.token_hex(32))"`.

> **⚠️ Do NOT surface the code through the tracking API.** There is no authentication anywhere in
> FleetPulse. `GET /api/v1/waybills/{awb}` is open to anyone holding the AWB, and **the AWB is
> printed on the label** — so exposing the OTP there hands it to whoever is physically holding the
> parcel, which is precisely the person it is meant to defend against. The same applies to the
> customer tracking page and to `_fetch_waybill`'s cached payload. Keep `delivery_otps` out of
> every read path a browser can reach.
>
> This is also the honest limit of the feature as specified: **it verifies the code, not the
> person.** Real defence needs authentication, which is a backend-first project of its own.

---

## 7. Driver app

`apps/driver-app/app.js`, in the `#pod-submit` handler.

- **Distinguish the rejection codes.** A wrong code, an expired code and a locked-out code need
  different messages, because the remedies differ. `ApiError.status` already carries what is
  needed:

  ```js
  if (ex.status === 401) err.textContent = ex.message;                    // "…2 attempt(s) remaining"
  else if (ex.status === 410) err.textContent = "Code expired — ask ops to reissue.";
  else if (ex.status === 429) err.textContent = "Too many attempts — ask ops to reissue.";
  else if (ex.status === 409) err.textContent = "This code was already used.";
  ```

- **Keep the `/^\d{4,6}$/` check, and demote it.** It is a convenience that saves a round trip on
  an obvious typo. It is **not** a security control, and the comment above it should say so —
  otherwise the next reader assumes the validation is handled.

- **A "resend code" affordance needs no new data.** `dispatch` already enriches every stop with
  `consignee_name`, `consignee_phone` and `consignee_addr` from consignment-service, so the driver
  app can show which number the code went to without any new endpoint.

- **Tighten `pod_data` to 6 for OTP.** The model's `max_length=200_000` exists for signature data
  URLs. Once `pod_type == "OTP"`, a 200KB "code" is meaningless — validate the discriminated shape
  in `DeliveryRequest` rather than accepting it and comparing.

---

## 8. Testing the failure paths

> **⚠️ Preserve the no-infrastructure property.** The existing 36 tests never need Postgres or
> Redis — `TestClient(app)` is used *without* a context manager so lifespan never runs, and every
> case either touches nothing or is rejected by Pydantic first. That is what lets them run in CI
> with nothing up. A test that reaches a live handler fails with
> `RuntimeError: DATABASE_URL is not set`.
>
> This is why §3.1 puts every piece of logic worth testing in a pure `app/otp.py`. Unit-test that
> module directly; keep endpoint tests to Pydantic-rejection cases.

Pure unit tests, no infrastructure:

```python
def test_generate_is_always_six_digits():
    assert all(len(otp.generate()) == 6 and c.isdigit()
               for _ in range(500) for c in otp.generate())

def test_verify_accepts_the_right_code_and_rejects_a_near_miss(monkeypatch):
    monkeypatch.setenv("OTP_SECRET", "test-secret")
    h = otp.hash_code("123456")
    assert otp.verify_hash("123456", h)
    assert not otp.verify_hash("123457", h)

def test_hash_depends_on_the_secret(monkeypatch):
    monkeypatch.setenv("OTP_SECRET", "secret-a"); a = otp.hash_code("123456")
    monkeypatch.setenv("OTP_SECRET", "secret-b"); b = otp.hash_code("123456")
    assert a != b            # this is the whole security argument in one assertion

def test_missing_secret_fails_loudly(monkeypatch):
    monkeypatch.delenv("OTP_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="OTP_SECRET"):
        otp.hash_code("123456")

def test_expiry_is_four_hours():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not otp.is_expired(otp.expiry_from(now), now + timedelta(hours=3, minutes=59))
    assert otp.is_expired(otp.expiry_from(now), now + timedelta(hours=4, seconds=1))
```

Integration checks, run by hand against the live stack:

| # | Test | Expected |
|---|---|---|
| 1 | Deliver with the wrong code | `401`, message names attempts remaining, **no `delivery_attempts` row** |
| 2 | Six wrong codes | 6th returns `429`; correct code afterwards still `429` |
| 3 | Deliver with the correct code | `201`, parcel `DELIVERED`, `consumed_at` set |
| 4 | Replay the same correct code | `409` |
| 5 | Hand-expire a row, then verify | `410` |
| 6 | Stop `consignment-service`, deliver | Fails at step 2 — no half-recorded delivery |
| 7 | Re-dispatch a parcel | New code issued, `attempts` reset, **a second SMS is sent** |
| 8 | RTO with no code at all | `201` — RTO is never verified |

Test 1 is the one that proves the whole feature: it is exactly what silently succeeds today.

---

## 9. Milestones

### Milestone A — prerequisite

- [ ] Build [FleetPulse-Addon-Notification.md](FleetPulse-Addon-Notification.md) through §5
- [ ] **Fix its §3 producer placement**: hook `_apply_transition()`, not `record_scan()` (§3.2)
- [ ] Confirm an outbox event is written in the same transaction as a status change

### Milestone B — issue and send

- [ ] `app/otp.py` with the pure helpers, plus its unit tests — **these pass with nothing running**
- [ ] `OTP_SECRET` in `.env.example` and `docker-compose.yml`
- [ ] `consignment.delivery_otps` appended to `db/init.sql`, applied in place (§2)
- [ ] Issue on `OUT_FOR_DELIVERY` inside the existing transaction
- [ ] `OTP_ISSUED` routed to the stub SMS sender; read a real code out of the logs
- [ ] Scrub `payload->'code'` when the worker sets `processed_at`

> ✅ **Checkpoint:** create a runsheet, and a code for each parcel appears in the notification
> service logs.

### Milestone C — verify

- [ ] `POST /waybills/{awb}/verify-otp` with the four rejection codes
- [ ] `cc.verify_otp` in `consignment_client.py`
- [ ] Reorder `record_delivery` so verification precedes the insert (§5.3)
- [ ] Driver app distinguishes 401 / 410 / 429 / 409
- [ ] Walk all eight integration tests in §8

> ✅ **Checkpoint:** a wrong OTP returns 401 and leaves no trace in `delivery_attempts`; the right
> one delivers the parcel. The flow at §0.2 works end to end.

---

## 10. What this does not solve

- **It verifies the code, not the person.** With no authentication, anyone who can reach
  `/api/dispatch/v1/delivery` can submit a delivery; the OTP only stops them completing one for a
  parcel whose code they do not have. Real driver identity is a backend-first auth project.
- **The consignee's phone number is trusted as booked.** A merchant that books with the wrong
  number sends the code to the wrong person, and nothing in the system can tell.
- **Nothing rate-limits by caller.** `MAX_ATTEMPTS` is per parcel. An attacker guessing across
  thousands of AWBs is not slowed at all.
- **The stub sender is not an SMS provider.** Delivery receipts, opt-outs, retries against carrier
  errors and per-message cost are all real and all out of scope here.
