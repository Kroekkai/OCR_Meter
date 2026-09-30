"""
CFO Platform (NT Carbon) external push — POST /external/v1/meter-readings
and GET /external/v1/device.

Confirmed scope so far:
  - Section 4.1 (request payload fields) — generate_external_ref() and
    build_meter_reading_payload(), pure functions, no DB/HTTP.
  - push_meter_reading() / verify_device() — the actual HTTP calls,
    response parsing (sections 3.1 and 4.2), and error categorization
    (section 4.3) into ExternalPushClientError (400/401/403/413 — the
    spec says never retry these) vs ExternalPushServerError (5xx/
    timeout/network — retryable).
  - Section 5 (retry) — attempt_push_for_row() / process_due_pushes(),
    the 30s/2min/10min/1hr schedule, and persisting push/retry status
    into ocr_meter.push_* (db/init.sql). Test data (ocr_meter_test) is never pushed.

Deliberately NOT in scope yet (flagged, not guessed at): anything that
actually CALLS process_due_pushes() on a schedule (ข้อ 21, auto-push —
this module only does one batch per call, something else has to invoke
it repeatedly), offline queueing beyond what the retry schedule already
gives, re-pushing after a manual edit with a fresh external_ref, and an
admin-facing alert when push_meter_matched comes back false.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import io
import json
import logging
import mimetypes
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import asyncpg
import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, ValidationError

from app.crypto import decrypt_api_key

logger = logging.getLogger("ocr_meter_store.external_push")

# ข้อ 23, confirmed from spec section 4.1's own field table: "image ...
# ไม่เกิน 10 MB". A hard cap the spec itself defines, not a choice made
# here.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_T = TypeVar("_T")


class ExternalRefCollisionError(Exception):
    """
    Confirmed safety valve only — raised when generate_unique_external_ref()
    below has retried 20 times and every candidate ref still collides.
    Should be essentially impossible in practice (see that function's
    own docstring); a plain Exception, not an ExternalPushError subclass,
    since this isn't a CFO Platform push failure at all — it's a
    same-meter/same-minute collision in OUR OWN external_ref generation,
    caught before any HTTP call to CFO Platform ever happens. Callers
    (app/routers/ocr_jobs.py, app/routers/images.py) turn this into an
    HTTPException themselves — this module deliberately has no FastAPI
    dependency.
    """


async def generate_unique_external_ref(
    conn,
    meter_id: str,
    capture_date: dt.date,
    capture_time: dt.time,
    apply: Callable[[str], Awaitable[_T]],
    *,
    exclude_ref: str | None = None,
) -> _T:
    """
    Confirmed shared helper (ข้อ 20) — extracted from
    app/routers/ocr_jobs.py::_submit_ocr_result()'s original inline
    retry loop so the manual-edit re-push path (ข้อ 20) can reuse the
    exact same collision-handling logic instead of duplicating it,
    rather than risking the two copies drifting apart over time.

    apply(ref) — confirmed: the caller supplies whatever DB write
    actually consumes this external_ref (an INSERT for a brand new OCR
    result, an UPDATE for a manual-edit reset) and must raise
    asyncpg.UniqueViolationError if that specific ref collides with an
    existing row. Wrapped in its own nested conn.transaction() (a
    SAVEPOINT, since the caller is always already inside an outer
    transaction) for the same reason as the original: a collision must
    not poison the caller's entire outer transaction, only this one
    failed write.

    exclude_ref — confirmed fix, found during a spec-compliance audit:
    without this, a manual edit that doesn't change meter_id/
    capture_date/capture_time (the normal case — only ocr_reading
    changes) would compute the exact SAME base ref as the row already
    has. An UPDATE that sets a column to its OWN current value never
    trips a UNIQUE constraint (Postgres only checks against OTHER
    rows), so apply(ref) would silently SUCCEED while returning the
    unchanged ref — directly violating spec section 5's "ต้องการแก้
    ค่าที่ส่งไปแล้ว → ส่งรายการใหม่ด้วย externalRef ใหม่": CFO Platform
    would see the exact same externalRef and treat the corrected
    reading as a duplicate retry of the old one, never picking up the
    correction at all. Passing the row's current external_ref here
    forces the very first candidate to already carry a "-2" (or
    higher) suffix whenever it would otherwise collide with itself,
    guaranteeing every manual edit produces a genuinely different ref.
    """
    base_ref = generate_external_ref(meter_id, capture_date, capture_time)

    # Confirmed fix (found by the real-PostgreSQL test, not catchable with
    # mocks): the previous version only refused to hand back the row's
    # CURRENT ref. But on a SECOND manual edit the current ref is already
    # "…-4" (say), so the original "…0800" is free again — and it got
    # handed straight back out, a ref CFO Platform saw on the very first
    # push. They'd answer duplicate:true and silently drop the correction
    # (the exact failure spec section 5's "externalRef ใหม่" rule exists to
    # prevent). A reading's ref must NEVER walk backwards, so the suffix
    # is derived from the current ref and only ever increases:
    #   (none) base -> -2 -> -3 -> ... (skipping any that collide with other
    #   readings' refs), each edit strictly above whatever the row has now.
    start = 1
    if exclude_ref is not None:
        if exclude_ref == base_ref:
            start = 2
        elif exclude_ref.startswith(base_ref + "-") and exclude_ref[len(base_ref) + 1:].isdigit():
            start = int(exclude_ref[len(base_ref) + 1:]) + 1
    attempt = start
    ref = base_ref if attempt == 1 else f"{base_ref}-{attempt}"
    collisions = 0
    while True:
        try:
            async with conn.transaction():
                return await apply(ref)
        except asyncpg.UniqueViolationError:
            attempt += 1
            collisions += 1
            if collisions > 20:
                # Safety valve only — counts collisions, not the suffix value
                # itself (a heavily-edited reading legitimately has a high
                # suffix). See ExternalRefCollisionError's own docstring.
                raise ExternalRefCollisionError(
                    f"Could not generate a unique external_ref after {collisions - 1} collisions (base: {base_ref!r})"
                )
            ref = f"{base_ref}-{attempt}"

# Confirmed fix, found during a correctness audit — bare
# asyncio.create_task(coro) with no reference kept is a documented
# asyncio footgun: "The event loop only keeps weak references to
# tasks. A task that isn't referenced elsewhere may get garbage
# collected at any time, even before it's done." A fire-and-forget
# push (see push_immediately_if_configured() below) was originally
# started exactly this way from app/routers/ocr_jobs.py — silently at
# risk of vanishing mid-HTTP-call with no error, no log, nothing.
# Standard fix straight from the asyncio docs: hold a strong reference
# in a module-level set for the task's lifetime, removed automatically
# via its own done-callback once it finishes.
_background_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro) -> asyncio.Task:
    """
    Confirmed fix (see module-level _background_tasks comment above) —
    the correct way to start a "don't wait for this" background task
    anywhere in this codebase, instead of a bare asyncio.create_task()
    whose result gets discarded.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# --------------------------------------------------------------------------
# Errors — confirmed categorization straight from spec section 4.3's own
# table (400/401/403/413 "ห้าม retry" vs 5xx "retry ได้"). Two classes,
# not one with a status_code attribute, so a caller (the retry logic,
# not built yet) can route on exception TYPE with a plain except clause
# instead of having to inspect a field and remember which values mean
# "don't retry" — the type itself carries that meaning.
class ExternalPushError(Exception):
    """Base class — not meant to be raised directly, only its two subclasses below."""


class ExternalPushClientError(ExternalPushError):
    """
    400 (bad/missing field), 401 (bad/disabled API key), 403 (forbidden), or 413 (file too large).
    attempt_push_for_row() splits these: 400/413 -> failed_permanent for
    that row; 401/403 -> blocked_auth + a key-level pause (AUTH_BLOCK_STATUS_CODES).
    Confirmed: the spec says never retry these — the same request will
    fail the same way every time (a 401 in particular means the key
    itself is the problem, not this one attempt).
    """

    def __init__(self, status_code: int, message: str, errors: dict | None = None):
        self.status_code = status_code
        self.message = message
        self.errors = errors or {}
        super().__init__(f"HTTP {status_code}: {message}")


class ExternalPushServerError(ExternalPushError):
    """
    5xx from CFO Platform, a request timeout, or a network-level failure
    (couldn't even connect). Confirmed retryable per the spec — but this
    module does not retry anything itself; it just raises this so a
    caller can decide to (not built yet, see this file's own docstring).
    """

    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code  # None for timeout/network errors that never got a response at all
        self.message = message
        super().__init__(message)


# --------------------------------------------------------------------------
# Response schemas — field names/types copied directly from the spec's
# own worked JSON examples (sections 3.1 and 4.2), not guessed at.
class DeviceMeterInfo(BaseModel):
    meterNo: str
    type: str
    tenantName: str | None = None
    locationName: str | None = None


class DeviceVerifyData(BaseModel):
    """GET /external/v1/device's data object — confirmed shape from spec section 3.1."""

    deviceSn: str
    deviceType: str
    installLocation: str | None = None
    name: str | None = None
    isActive: bool
    meter: DeviceMeterInfo


class MeterReadingPushData(BaseModel):
    """
    POST .../meter-readings success (201) data object — field names from
    spec section 4.2. status/duplicate/meterMatched meanings are from
    that same section's own field table, not inferred.

    Every field is OPTIONAL on purpose (spec-compliance fix): a 201 means
    CFO Platform has already accepted and stored the reading. If this
    model were strict, one unexpected value (e.g. meterNo: null when
    meterMatched is false) would fail validation, land in
    attempt_push_for_row()'s generic except -> failed_retryable, and the
    same already-accepted reading would be re-sent every hour forever
    (each retry answered duplicate:true, each failing the same parse).
    Success is decided by the HTTP status alone; these fields are only
    what we record about it.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    externalRef: str | None = None
    deviceSn: str | None = None
    meterNo: str | None = None
    status: str | None = None  # "pending" | "confirmed" | "rejected" — CFO Platform staff review outcome (section 1.1)
    duplicate: bool | None = None  # true = externalRef already seen, CFO Platform returned the EXISTING record (section 5)
    meterMatched: bool | None = None  # false = meter tied to this SN isn't in CFO Platform's registry — admin must be told (4.2)
    imageUrl: str | None = None


def generate_external_ref(meter_id: str, capture_date: dt.date, capture_time: dt.time) -> str:
    """
    Confirmed request — format is <SN>-<YYYYMMDD>-<HHmm>, exactly the
    spec's own recommended format and matching its worked example
    ("ELE-000001-20260924-0800" for meter_id="ELE-000001", captured at
    08:00 on 2026-09-24) — verified against that example directly, not
    just inferred from the field description.

    meter_id IS the SN here, confirmed — no separate external_sn lookup
    (see the "meter_id vs SN" decision: ESP32 devices were switched to
    send the CFO Platform-issued SN directly as their meter_id, so this
    server's own meter_id and CFO Platform's SN are now the same string).

    HHmm truncates to the minute (seconds dropped) — the spec's own
    format has no seconds component. capture_date/capture_time are
    assumed already Bangkok-local (true everywhere else in this
    codebase — see app/filename.py's BANGKOK_TZ) — spec requires
    Asia/Bangkok throughout, so no further timezone conversion happens
    here.

    Deliberately does NOT check the 128-char limit from the spec — SN
    format (ELE-000001, 10 chars) + "-YYYYMMDD-HHmm" (14 chars) is 24
    chars for every real value seen so far, nowhere near the limit. Not
    worth a runtime check for a limit this far away under the current
    SN format; revisit if that format ever changes.
    """
    return f"{meter_id}-{capture_date.strftime('%Y%m%d')}-{capture_time.strftime('%H%M')}"


def build_meter_reading_payload(
    *,
    external_ref: str,
    capture_date: dt.date,
    ocr_reading: float | None,
    confidence: float | None,
) -> dict[str, str]:
    """
    Confirmed request — builds the multipart FORM FIELDS only, per spec
    section 4.1. Does NOT include the image itself (the caller attaches
    that separately as a file, since it has to be opened/streamed as
    binary, not built as a plain dict value) — and does not send
    deviceSn (confirmed: spec says omit it, the API key alone already
    identifies the device) or meta (confirmed: no per-reading device
    telemetry — firmware version, RSSI, battery — is available to
    attach to any specific reading in this system yet; esp32_upload_log
    is a decoupled heartbeat now, not tied to any specific reading, so
    there's nothing correct to put here — see external_ref's own column
    comment in db/init.sql for that history).

    external_ref is a parameter, not generated inside this function —
    confirmed scope: WHEN to call generate_external_ref() and persist
    the result is a separate, not-yet-decided question (see this
    module's docstring); this function only shapes whatever ref it's
    given into the rest of the payload.

    readingDate — confirmed: capture_date (the actual reading date),
    NEVER "today"/whenever this push happens to run — the spec is
    explicit that a delayed/retried push must still carry the original
    reading date, not the send date.

    aiValue — confirmed: OMITTED ENTIRELY when ocr_reading is None
    (a failed read), never sent as 0 — the spec explicitly forbids
    using 0 as a stand-in for "couldn't read it". Rounded to 2 decimal
    places (spec: "ทศนิยมไม่เกิน 2 ตำแหน่ง") when present.

    confidence — confirmed: passed through as-is when present, omitted
    entirely when None (same reasoning as aiValue: an absent field, not
    a fabricated 0, is correct for "no confidence to report").
    """
    payload: dict[str, str] = {
        "externalRef": external_ref,
        "readingDate": capture_date.isoformat(),
    }
    if ocr_reading is not None:
        # float(...) — confirmed bug fix: asyncpg returns a NUMERIC column
        # (ocr_reading's actual DB type) as decimal.Decimal, not a plain
        # float, regardless of this function's own float|None type hint
        # (a hint, not an enforced conversion). httpx's multipart encoder
        # (used here since every real call also attaches an image file)
        # rejects Decimal outright with TypeError: "Invalid type for
        # value. Expected primitive type, got <class 'decimal.Decimal'>"
        # — verified directly, not assumed. Converting to float first
        # avoids this unconditionally, whether the caller passed a float
        # or a Decimal straight from a DB row.
        #
        # Formatted as a plain decimal STRING with exactly 2 places (same
        # as the spec's own Python example, f"{value:.2f}") instead of a
        # float: str(float) can produce scientific notation ("1e+16") and
        # float rounding misrounds halves (round(2.675, 2) == 2.67).
        # Decimal(str(x)) keeps the value exactly as the DB/OCR had it.
        payload["aiValue"] = _format_ai_value(ocr_reading)
    if confidence is not None:
        payload["confidence"] = _format_plain_number(confidence)
    return payload


def _format_ai_value(value) -> str:
    """aiValue per spec 4.1: number ≥0, ≤2 decimal places, '.' as the decimal point, never exponent notation."""
    d = decimal.Decimal(str(value)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    if d < 0:
        # Already rejected at ingest (ocr_jobs.py) — re-checked here so a bad
        # row can never go out as a negative value (spec: "number ≥0").
        # Same category as a 400: the data itself is wrong, retrying won't help.
        raise ExternalPushClientError(400, f"aiValue ติดลบ ({value!r}) — ไม่ส่ง (spec 4.1: number ≥0)")
    return format(d, "f")


def _format_plain_number(value) -> str:
    """confidence per spec 4.1: number 0–100 — plain decimal notation, no exponent."""
    d = decimal.Decimal(str(value)).normalize()
    return format(d, "f")


def _parse_error_response(response: httpx.Response) -> tuple[str, dict]:
    """
    Confirmed shape from spec section 4.3: {success:false, statusCode,
    message: [...] (a LIST of strings, not one string — spec's own
    example shows multiple validation messages at once), errors, timestamp}.
    Falls back to the raw response text if the body isn't that shape at
    all (e.g. CFO Platform's own infra — a proxy, a load balancer — returns
    a plain-text or HTML error page instead of their API's own JSON, which
    genuinely happens for some 5xx cases before a request even reaches
    their application code).
    """
    try:
        body = response.json()
        messages = body.get("message", [])
        message = "; ".join(messages) if isinstance(messages, list) else str(messages)
        errors = body.get("errors", {}) or {}
        return message or f"HTTP {response.status_code}", errors
    except (ValueError, AttributeError):
        return response.text[:500] or f"HTTP {response.status_code}", {}


def _require_https(base_url: str) -> None:
    """
    Confirmed fix (line-by-line spec audit) — spec section 2's own
    connection-info table says "HTTPS เท่านั้น (TLS 1.2+)" explicitly.
    Nothing previously checked this at all: a misconfigured
    EXTERNAL_API_BASE_URL (a typo'd "http://", or accidentally copying
    a non-TLS staging URL) would have silently sent the X-API-Key
    header — a real secret — over an unencrypted connection, with no
    warning anywhere. Raises ValueError immediately rather than
    letting that happen even once; not an ExternalPushError subclass
    (not a CFO Platform response failure — this never even reaches the
    network).
    """
    if not base_url.lower().startswith("https://"):
        raise ValueError(
            f"EXTERNAL_API_BASE_URL must start with https:// (spec section 2: \"HTTPS เท่านั้น\") — got {base_url!r}"
        )


async def verify_device(*, base_url: str, api_key: str) -> DeviceVerifyData:
    """
    GET /external/v1/device — confirmed request (spec section 3, step 2
    of the install checklist: "เรียก GET /device ตรวจสอบว่าจับคู่ถูกต้อง").
    One attempt, no retry (see this module's own docstring for why).
    """
    _require_https(base_url)
    url = f"{base_url.rstrip('/')}/external/v1/device"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers={"X-API-Key": api_key})
    except httpx.TimeoutException as e:
        raise ExternalPushServerError(f"Timed out calling {url}") from e
    except httpx.HTTPError as e:
        raise ExternalPushServerError(f"Network error calling {url}: {e}") from e

    if response.status_code == 200:
        return DeviceVerifyData.model_validate(response.json()["data"])

    message, errors = _parse_error_response(response)
    if response.status_code in (400, 401, 403, 413):
        raise ExternalPushClientError(response.status_code, message, errors)
    # confirmed: anything else (5xx, or an unexpected code the spec
    # doesn't document) is treated as retryable — the safer default per
    # the spec's own framing (only 400/401/403/413 are ever named as
    # "ห้าม retry"; everything else falls under "retry ได้").
    raise ExternalPushServerError(message, status_code=response.status_code)


def _ensure_under_size_limit(image_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    """
    ข้อ 23, confirmed fix — spec section 4.1's own field table: "image
    ... ไม่เกิน 10 MB", and section 4.3's error table for 413 says
    "ลดขนาดรูปให้ ≤10 MB" — proactively shrinking an oversized image
    BEFORE sending, per that guidance, not just reacting to a 413 after
    the fact (uploading an oversized file only to have it rejected
    wastes the same bandwidth this is meant to avoid).

    Returns (bytes, content_type) — confirmed fix found during a
    correctness audit: this function ALWAYS re-encodes as JPEG when it
    has to shrink an image (see below), so the content_type it started
    with (e.g. "image/png" for a .png file) would be WRONG for the
    bytes actually being returned if the caller kept using its own
    original content_type — a real mismatch between the multipart
    Content-Type header and the actual file bytes, potentially
    confusing CFO Platform's own server if it validates one against
    the other. Returning content_type here forces the caller to use
    whatever's actually correct for the bytes it got back, rather than
    silently keeping a stale value.

    Returns (image_bytes, content_type) unchanged if already under
    MAX_IMAGE_BYTES — confirmed: never re-encodes a file that doesn't
    need it, avoiding a pointless JPEG-recompression quality loss on
    every single push.

    Re-encodes as JPEG at progressively lower quality (85, 70, 55, 40,
    25) until under the limit or quality options are exhausted.
    Confirmed choice, not spec-mandated (spec doesn't say how to
    shrink, only that it should end up ≤10MB): JPEG quality reduction
    was picked over resizing (changing pixel dimensions) since it's
    less likely to affect OCR-relevant meter-display legibility for a
    HUMAN reviewer looking at the same image CFO Platform's own staff
    see (per spec section 1.1's queue-for-review flow) — quality loss
    is usually less destructive to text legibility than downscaling.

    Raises ExternalPushClientError (never-retry, same category as a
    real 413) if even the lowest quality setting is still over the
    limit — confirmed: retrying wouldn't help, the image is just too
    large/detailed to fit under 10MB via quality reduction alone, a
    human needs to notice and address it (e.g. a lower base
    camera resolution setting), same as any other unretryable
    412/401/403.
    """
    if len(image_bytes) <= MAX_IMAGE_BYTES:
        return image_bytes, content_type

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:
        # Confirmed: an unreadable/corrupt image can't be shrunk at
        # all — same never-retry category as a genuine 413, since
        # retrying with the same corrupt bytes will always fail the
        # same way.
        raise ExternalPushClientError(
            413, f"Image is {len(image_bytes)} bytes (over the 10MB limit) and could not be re-encoded: {e}"
        ) from e

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")  # JPEG has no alpha channel — confirmed, drop it rather than fail on e.g. a PNG with transparency

    for quality in (85, 70, 55, 40, 25):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        candidate = buf.getvalue()
        if len(candidate) <= MAX_IMAGE_BYTES:
            logger.info(
                "compressed image from %d to %d bytes (JPEG quality=%d) to fit CFO Platform's 10MB limit",
                len(image_bytes),
                len(candidate),
                quality,
            )
            return candidate, "image/jpeg"  # confirmed fix: always "image/jpeg" here, matching the format actually written above — never the caller's stale original content_type

    raise ExternalPushClientError(
        413,
        f"Image is {len(image_bytes)} bytes and still exceeds the 10MB limit even at the lowest attempted JPEG quality (25).",
    )


async def push_meter_reading(
    *,
    base_url: str,
    api_key: str,
    external_ref: str,
    capture_date: dt.date,
    ocr_reading: float | None,
    confidence: float | None,
    image_path: str,
) -> MeterReadingPushData:
    """
    POST /external/v1/meter-readings — confirmed request. Builds the
    payload via build_meter_reading_payload() (section 4.1), attaches
    the image as multipart, sends one attempt, parses the response
    (section 4.2) or raises a categorized error (section 4.3). No
    retry — see this module's own docstring.

    image_path — confirmed: the full on-disk path already stored in
    ocr_meter.image / ocr_meter_test.image (see app/storage.py) — this
    function reads that file directly, no separate upload/copy step.
    Raises FileNotFoundError if it's missing, uncaught on purpose: a
    missing image file is a bug elsewhere in this system (the row
    claims an image exists), not a CFO Platform push failure, so it
    shouldn't be mistaken for one of the two categorized push errors.
    """
    _require_https(base_url)
    payload = build_meter_reading_payload(
        external_ref=external_ref,
        capture_date=capture_date,
        ocr_reading=ocr_reading,
        confidence=confidence,
    )
    url = f"{base_url.rstrip('/')}/external/v1/meter-readings"
    path = Path(image_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    filename = path.name

    image_bytes = path.read_bytes()  # raises FileNotFoundError uncaught, confirmed — see docstring
    # ข้อ 23, confirmed fix — see _ensure_under_size_limit()'s own
    # docstring. Raises ExternalPushClientError (never-retry) if the
    # image genuinely can't be shrunk under 10MB — deliberately NOT
    # caught here, propagates to the caller exactly like a real 413
    # from CFO Platform itself would.
    image_bytes, content_type = _ensure_under_size_limit(image_bytes, content_type)
    if content_type == "image/jpeg" and not filename.lower().endswith((".jpg", ".jpeg")):
        # Confirmed fix, found in the same audit as the content_type
        # mismatch above — if _ensure_under_size_limit() had to
        # re-encode (original was e.g. a .png), keep the FILENAME
        # consistent with the bytes/content_type too, not just the
        # content_type header alone — a filename claiming ".png" while
        # both the header and the actual bytes are JPEG would still be
        # an internally-inconsistent multipart part.
        filename = f"{path.stem}.jpg"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:  # longer than verify_device's 30s — this one uploads a file
            response = await client.post(
                url,
                headers={"X-API-Key": api_key},
                data=payload,
                files={"image": (filename, image_bytes, content_type)},
            )
    except httpx.TimeoutException as e:
        raise ExternalPushServerError(f"Timed out calling {url}") from e
    except httpx.HTTPError as e:
        raise ExternalPushServerError(f"Network error calling {url}: {e}") from e

    if response.status_code in (200, 201):
        # Spec 4.2 / section 5: 201 (also for duplicate:true). 200 accepted
        # defensively — any 2xx means CFO Platform stored the reading, and
        # treating it as a failure would only cause pointless re-sends.
        return _parse_success_data(response, external_ref)

    message, errors = _parse_error_response(response)
    if response.status_code in (400, 401, 403, 413):
        raise ExternalPushClientError(response.status_code, message, errors)
    raise ExternalPushServerError(message, status_code=response.status_code)


def _parse_success_data(response: httpx.Response, external_ref: str) -> MeterReadingPushData:
    """
    Never raises — see MeterReadingPushData's docstring. If the body isn't
    the documented shape at all, log it and return a minimal record: the
    push still counts as successful (CFO Platform has the reading).
    """
    try:
        data = response.json().get("data") or {}
        parsed = MeterReadingPushData.model_validate(data)
    except (ValueError, AttributeError, ValidationError) as e:
        logger.warning(
            "CFO Platform returned HTTP %s for %s but the body could not be parsed (%s) — treating as success",
            response.status_code,
            external_ref,
            e,
        )
        parsed = MeterReadingPushData()
    if parsed.externalRef is None:
        parsed.externalRef = external_ref
    return parsed


# --------------------------------------------------------------------------
# Retry logic (spec section 5) — confirmed schedule: 30s, 2min, 10min,
# 1hr after the 1st/2nd/3rd/4th+ failed attempt respectively. Beyond the
# 4th failure, this defaults to retrying every 1hr indefinitely — that
# specific choice (retry forever vs. give up eventually) was flagged as
# undecided earlier and is a default made here, not a confirmed spec
# requirement (the spec's own schedule only lists 4 intervals and says
# nothing about what happens after). Revisit if an eventual give-up
# point (with an admin alert) turns out to be wanted instead.
RETRY_SCHEDULE_SECONDS = [30, 120, 600, 3600]


def _next_retry_delay_seconds(attempt_count: int) -> int:
    idx = min(attempt_count - 1, len(RETRY_SCHEDULE_SECONDS) - 1)
    return RETRY_SCHEDULE_SECONDS[max(idx, 0)]


# Confirmed decision: ONLY ocr_meter (real, timer-triggered readings) is
# ever pushed to CFO Platform. ocr_meter_test (manual wake-ups /
# "_Test" captures — see app/filename.py::is_test_filename) exists to
# exercise THIS system's own pipeline and must never reach CFO
# Platform's review queue: nothing in the payload marks a reading as a
# test, so a pushed test capture would look identical to a real one to
# their staff. This constant is the single source of truth — every
# push path (scheduler, immediate push, manual endpoint, manual-edit
# re-push) goes through _require_pushable_table() below, so a future
# caller can't accidentally reintroduce test-table pushes.
PUSHABLE_TABLE = "ocr_meter"


def _require_pushable_table(table: str) -> None:
    if table != PUSHABLE_TABLE:
        raise ValueError(
            f"Refusing to push from {table!r} — only {PUSHABLE_TABLE!r} is ever sent to CFO Platform "
            "(test data stays local; see PUSHABLE_TABLE's comment)."
        )


# Spec 4.3: these mean the device's KEY can't be used (wrong/disabled key,
# device switched off, IP not allowed, no ingest permission) — see the
# blocked_auth handling in attempt_push_for_row().
AUTH_BLOCK_STATUS_CODES = (401, 403)


async def _set_key_auth_block(conn, meter_id: str, status_code: int, message: str) -> None:
    await conn.execute(
        """
        UPDATE external_api_keys
        SET auth_failed_at = now(), auth_failed_info = $2
        WHERE meter_id = $1
        """,
        meter_id,
        json.dumps({"status": status_code, "message": message[:1000]}),
    )
    logger.warning(
        "CFO Platform rejected the API key for %s (HTTP %s: %s) — pushes for this meter are paused until an admin fixes it",
        meter_id,
        status_code,
        message,
    )


async def _clear_key_auth_block(conn, meter_id: str) -> None:
    await conn.execute(
        """
        UPDATE external_api_keys
        SET auth_failed_at = NULL, auth_failed_info = NULL
        WHERE meter_id = $1 AND auth_failed_at IS NOT NULL
        """,
        meter_id,
    )


async def attempt_push_for_row(conn, row: dict, table: str, api_key: str, base_url: str) -> str:
    """
    Confirmed request (ข้อ 19) — ONE push attempt for one ocr_meter
    row, then updates that row's push_* columns based on
    the outcome. Returns a short status string ("success" /
    "failed_retryable" / "failed_permanent" / "blocked_auth") for the caller to tally,
    doesn't raise — every ExternalPushError case is caught and turned
    into a DB update instead, since a failed push is an expected,
    routine outcome here, not something the caller needs to handle as
    an exception.

    table — must be PUSHABLE_TABLE ("ocr_meter"); anything else raises
    ValueError immediately (see _require_pushable_table). Still
    interpolated directly into the SQL (table names can't be query
    params) — safe because the guard above means it can only ever be
    that one hardcoded literal, never user input.
    """
    _require_pushable_table(table)
    # Config errors (a non-https base URL) must propagate, NOT be pinned
    # on this one row — they affect every row identically and should
    # fail the whole batch loudly once (process_due_pushes checks this
    # up front too), not quietly mark each row failed.
    _require_https(base_url)
    if not row["image"]:
        # Confirmed fix (found in the full component audit): ocr_meter.image
        # is a nullable column. A NULL used to reach Path(None) inside
        # push_meter_reading() → uncaught TypeError → aborted the WHOLE
        # process_due_pushes() batch, every tick, forever (head-of-line
        # blocking: every healthy row after it in id order never got
        # pushed). Retrying can't conjure an image path, so this is
        # permanent, same category as a missing file.
        await conn.execute(
            f"""
            UPDATE {table}
            SET push_status = 'failed_permanent',
                push_last_response = $1,
                push_next_retry_at = NULL,
                push_attempt_count = push_attempt_count + 1
            WHERE id = $2
            """,
            json.dumps({"error": "No image path recorded on this row — nothing to send"}),
            row["id"],
        )
        return "failed_permanent"
    try:
        data = await push_meter_reading(
            base_url=base_url,
            api_key=api_key,
            external_ref=row["external_ref"],
            capture_date=row["capture_date"],
            ocr_reading=row["ocr_reading"],
            confidence=row["confidence"],
            image_path=row["image"],
        )
        await conn.execute(
            f"""
            UPDATE {table}
            SET push_status = 'pending',
                push_last_response = $1,
                push_meter_matched = $2,
                push_next_retry_at = NULL,
                push_attempt_count = push_attempt_count + 1
            WHERE id = $3
            """,
            json.dumps({"response_id": data.id, "cfo_status": data.status, "duplicate": data.duplicate}),
            data.meterMatched,
            row["id"],
        )
        # The key evidently works (e.g. an admin's manual push after CFO
        # Platform re-enabled the device) — lift any auth block so the
        # rest of this meter's queued rows resume on the next sweep.
        await _clear_key_auth_block(conn, row["meter_id"])
        return "success"

    except FileNotFoundError as e:
        # Confirmed fix — found during a correctness audit, not
        # originally handled: push_meter_reading() deliberately lets
        # FileNotFoundError through uncaught (a missing image file is a
        # bug elsewhere in this system, not a CFO Platform push
        # failure — see that function's own docstring). Left
        # completely uncaught HERE too, though, it would blow up this
        # function with a raw, unhandled traceback — both for the
        # admin manual-push endpoint (a confusing 500) and for the
        # future scheduler (ข้อ 21, would crash the whole batch
        # instead of just skipping this one row). Treated as
        # failed_permanent: retrying won't make a missing file
        # reappear, so this needs a human to notice and fix (re-run
        # OCR, restore the file, etc.), same as a genuine 400/401/403.
        await conn.execute(
            f"""
            UPDATE {table}
            SET push_status = 'failed_permanent',
                push_last_response = $1,
                push_next_retry_at = NULL,
                push_attempt_count = push_attempt_count + 1
            WHERE id = $2
            """,
            json.dumps({"error": f"Image file not found: {e}"}),
            row["id"],
        )
        return "failed_permanent"

    except ExternalPushClientError as e:
        if e.status_code in AUTH_BLOCK_STATUS_CODES:
            # Spec 4.3: 401 → "หยุดส่ง แจ้งผู้ดูแล", 403 → "แจ้งผู้ดูแล".
            # A KEY problem, not this row's: every other row of this meter
            # would fail identically. So: don't retry on a schedule (spec:
            # ห้าม retry 401/403), stop sending for the whole meter (key-level
            # block), keep this row queued as blocked_auth so it goes out
            # automatically once the key is fixed — instead of being stranded
            # as failed_permanent forever.
            await _set_key_auth_block(conn, row["meter_id"], e.status_code, e.message)
            await conn.execute(
                f"""
                UPDATE {table}
                SET push_status = 'blocked_auth',
                    push_last_response = $1,
                    push_next_retry_at = NULL,
                    push_attempt_count = push_attempt_count + 1
                WHERE id = $2
                """,
                json.dumps({"error": f"HTTP {e.status_code}: {e.message}"}),
                row["id"],
            )
            return "blocked_auth"
        # 400/413 — the data/image of THIS row is the problem. Never retry. push_next_retry_at
        # stays NULL forever, which is what keeps process_due_pushes()'s
        # WHERE clause from ever picking this row up again.
        await conn.execute(
            f"""
            UPDATE {table}
            SET push_status = 'failed_permanent',
                push_last_response = $1,
                push_next_retry_at = NULL,
                push_attempt_count = push_attempt_count + 1
            WHERE id = $2
            """,
            json.dumps({"error": f"HTTP {e.status_code}: {e.message}"}),
            row["id"],
        )
        return "failed_permanent"

    except ExternalPushServerError as e:
        new_attempt_count = row["push_attempt_count"] + 1
        delay = _next_retry_delay_seconds(new_attempt_count)
        next_retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)
        await conn.execute(
            f"""
            UPDATE {table}
            SET push_status = 'failed_retryable',
                push_last_response = $1,
                push_attempt_count = $2,
                push_next_retry_at = $3
            WHERE id = $4
            """,
            json.dumps({"error": e.message}),
            new_attempt_count,
            next_retry_at,
            row["id"],
        )
        return "failed_retryable"

    except Exception as e:
        # Confirmed fix (same audit): any OTHER unexpected exception
        # (a bug, a corrupt row, a library edge case) must be contained
        # to THIS row, never allowed to abort the caller's whole batch.
        # Treated as retryable-with-backoff (not permanent) since we
        # can't know it's unrecoverable, and the message lands in
        # push_last_error so an admin can see what happened.
        logger.exception("unexpected error pushing %s.id=%s", table, row.get("id"))
        new_attempt_count = row["push_attempt_count"] + 1
        next_retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=_next_retry_delay_seconds(new_attempt_count)
        )
        await conn.execute(
            f"""
            UPDATE {table}
            SET push_status = 'failed_retryable',
                push_last_response = $1,
                push_attempt_count = $2,
                push_next_retry_at = $3
            WHERE id = $4
            """,
            json.dumps({"error": f"Unexpected error: {type(e).__name__}: {e}"}),
            new_attempt_count,
            next_retry_at,
            row["id"],
        )
        return "failed_retryable"


async def process_due_pushes(conn, base_url: str, max_batch: int = 50) -> dict[str, int]:
    """
    Confirmed request (ข้อ 19) — finds every ocr_meter row (never
    ocr_meter_test — see PUSHABLE_TABLE) that needs a push attempt right now (never pushed, or a retryable
    failure whose scheduled retry time has arrived) and processes each
    one via attempt_push_for_row().

    Deliberately NOT wired up to run on any schedule/trigger by itself —
    confirmed out of scope for this piece (that's ข้อ 21, "auto-push
    อัตโนมัติหลัง OCR เสร็จ", not done yet) — this function does one
    batch and returns; something else (a cron job, a manual admin
    button — ข้อ 17) has to actually call it repeatedly for retries to
    happen over time.

    Rows with no active API key configured for their meter_id are
    skipped (not an error — that meter just isn't set up for CFO
    Platform push yet) rather than attempted and failed.

    max_batch — confirmed default 50, a reasonable-sized batch per
    table per call — not from the spec (which says nothing about batch
    sizing), a practical choice to avoid one call trying to push an
    unbounded backlog all at once.
    """
    _require_https(base_url)  # misconfiguration: fail the whole tick loudly, once, before touching any row
    summary = {
        "success": 0,
        "failed_retryable": 0,
        "failed_permanent": 0,
        "blocked_auth": 0,
        "skipped_no_key": 0,
        "skipped_auth_blocked": 0,
    }

    for table in (PUSHABLE_TABLE,):
        rows = await conn.fetch(
            f"""
            SELECT m.*, k.api_key_encrypted, k.auth_failed_at
            FROM {table} m
            LEFT JOIN external_api_keys k ON k.meter_id = m.meter_id AND k.is_active = true
            WHERE m.push_status IN ('not_pushed', 'failed_retryable', 'blocked_auth')
              AND (m.push_next_retry_at IS NULL OR m.push_next_retry_at <= now())
              AND m.external_ref IS NOT NULL
            ORDER BY m.id
            LIMIT $1
            """,
            max_batch,
        )
        blocked_this_batch: set[str] = set()
        for row in rows:
            row = dict(row)
            if row["api_key_encrypted"] is None:
                summary["skipped_no_key"] += 1
                continue
            if row["auth_failed_at"] is not None or row["meter_id"] in blocked_this_batch:
                # Spec 4.3 (401: หยุดส่ง) — key is known-bad, don't keep
                # hitting CFO Platform with it. Row stays queued.
                summary["skipped_auth_blocked"] += 1
                continue
            api_key = decrypt_api_key(row["api_key_encrypted"])
            outcome = await attempt_push_for_row(conn, row, table, api_key, base_url)
            summary[outcome] += 1
            if outcome == "blocked_auth":
                blocked_this_batch.add(row["meter_id"])

    return summary


async def external_push_sweep_loop() -> None:
    """
    Confirmed request (ข้อ 18) — runs until cancelled (app shutdown),
    calling process_due_pushes() every external_push_sweep_interval_seconds.
    Same pattern as app/grouping.py::group_sweep_loop(), confirmed
    reused deliberately for consistency: errors are logged and
    swallowed, never allowed to kill the loop — one bad tick (e.g. CFO
    Platform briefly unreachable for an entire batch) must not mean
    every future retry silently stops happening too.

    Deliberately imports app.db/app.config here, not at module level —
    avoids this module (already imported by app/routers/meters.py and
    app/routers/device_config.py for the manual-push/API-key endpoints)
    needing pool()/get_settings() at all for callers that only use the
    pure functions or push_meter_reading()/verify_device() directly.

    Confirmed: does nothing (not even attempt a DB query) on a tick
    where external_api_base_url is still blank — matches this
    project's established pattern of a blank config value being a
    loud, deliberate "not configured yet" signal rather than a
    misleading error every 30s before real credentials exist.
    """
    from app.config import get_settings
    from app.db import pool

    settings = get_settings()
    while True:
        try:
            if settings.external_api_base_url:
                async with pool().acquire() as conn:
                    summary = await process_due_pushes(conn, settings.external_api_base_url)
                if any(summary.values()):
                    logger.info("external push sweep: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external push sweep failed — will retry next tick")
        await asyncio.sleep(settings.external_push_sweep_interval_seconds)


async def push_immediately_if_configured(meter_id: str, table: str, row_id: int, base_url: str) -> None:
    """
    Confirmed fix — "เอาตาม spec": spec section 1.1's own flow shows a
    device POSTing "รูป + ตัวเลข" as soon as a reading is captured, not
    batched on a fixed internal timer — the "ตั้งรอบการส่งตามที่ตกลง"
    language (section 3, install step 4) is about the physical
    capture/meter-reading cycle (daily, per meter-reading round), not
    about an artificial delay this server adds on top of a reading
    that's already sitting here ready to go. Attempts a push right
    away when a reading first becomes available, instead of relying
    solely on external_push_sweep_loop's next tick (up to
    external_push_sweep_interval_seconds later, confirmed too slow to
    call "ตาม spec").

    external_push_sweep_loop remains the safety net for retries (the
    30s/2min/10min/1hr schedule genuinely IS spec-defined, for retrying
    an already-FAILED attempt — see RETRY_SCHEDULE_SECONDS) and for
    anything this immediate attempt misses (e.g. the app restarting
    between OCR completing and this task actually running).

    Fire-and-forget by design — confirmed: the caller
    (app/routers/ocr_jobs.py::_submit_ocr_result) wraps this in
    asyncio.create_task() and does not await it, so a slow or failed
    CFO Platform call never delays the OCR Worker's own response.
    Opens its own DB connection since the caller's own
    connection/transaction is already closed and returned to the pool
    by the time this runs (it starts right after that transaction
    commits, not inside it).

    Silently does nothing (not an error) if external_api_base_url is
    blank, or if no active API key is configured for this meter yet —
    same as process_due_pushes() skips these rows too; the reading
    just sits as push_status='not_pushed' until both exist, then the
    next sweep tick picks it up normally, no reading ever gets lost by
    this early-return.
    """
    if table != PUSHABLE_TABLE:
        return  # test data never goes to CFO Platform — see PUSHABLE_TABLE's comment
    if not base_url:
        return
    from app.db import pool

    try:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1", row_id)
            if row is None or row["external_ref"] is None:
                return  # confirmed: should not normally happen — see attempt_push_for_row's own note on this
            key_row = await conn.fetchrow(
                "SELECT api_key_encrypted, auth_failed_at FROM external_api_keys WHERE meter_id = $1 AND is_active = true",
                meter_id,
            )
            if key_row is None:
                return  # not configured for CFO Platform push yet — not an error, see docstring
            if key_row["auth_failed_at"] is not None:
                # Key is paused after a 401/403 (spec 4.3: หยุดส่ง) — leave the
                # row queued; the sweep sends it once the key is fixed.
                return
            api_key = decrypt_api_key(key_row["api_key_encrypted"])
            await attempt_push_for_row(conn, dict(row), table, api_key, base_url)
    except Exception:
        # Confirmed: never let an immediate-push failure propagate back
        # into _submit_ocr_result's caller (the OCR Worker's own
        # request) — this is a background task, and
        # external_push_sweep_loop is the safety net that will retry
        # this same row on its next tick regardless of what went wrong
        # here.
        logger.exception("immediate push attempt failed for %s.id=%s — scheduler will retry", table, row_id)
