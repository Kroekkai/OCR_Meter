from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser, get_admin_or_service, get_current_admin, get_uploader
from app.config import get_settings
from app.crypto import decrypt_api_key, encrypt_api_key
from app.db import pool
from app.external_push import ExternalPushClientError, ExternalPushServerError, verify_device
from app.schemas import (
    DeviceConfigOut,
    DeviceConfigSetRequest,
    ExternalApiKeySetRequest,
    ExternalApiKeyStatus,
    VerifyExternalResult,
)

router = APIRouter(tags=["default"])

_UI_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "device_config_ui.html"


@router.get("/admin/device-config-ui", response_class=HTMLResponse, summary="Admin Device Config Dashboard")
async def admin_device_config_ui():
    """
    NOT in the spec doc — a small standalone dashboard for humans to
    browse/edit device_config without curl or Swagger. No FastAPI-side
    auth on this route itself (it's just static HTML/CSS/JS — there's
    nothing here to protect); the page's own JS calls /login and stores
    the JWT in the browser's localStorage, then sends it as a normal
    Authorization: Bearer header on every /admin/device-config* call —
    same auth path as any other admin client, just from a browser
    instead of curl. All API calls in the page use relative URLs (e.g.
    fetch("device-config"), fetch("../login")) resolved against this
    page's own path, so it works unmodified behind BASE_PATH_PREFIX
    (production) and without it (local) — no hardcoded domain anywhere.
    """
    return HTMLResponse(content=_UI_HTML_PATH.read_text(encoding="utf-8"))


# Per the spec doc's example 1 (Fix Date mode, day 26 at 08:00) — used
# whenever a meter has no device_config row yet, per the doc's own rule:
# "หากเป็น Meter ใหม่...ตอบกลับ Default Config...พร้อม HTTP Status 200 OK".
DEFAULT_CONFIG = {
    "schedule_mode": 1,
    "date1": [26, 0, 0, 8, 0],
    "date2": [0, 0, 0, 0, 0],
    "photo_count": 3,
    "photo_delay": 5,
}


def _config_out(row) -> DeviceConfigOut:
    return DeviceConfigOut(
        meter_id=row["meter_id"],
        schedule_mode=row["schedule_mode"],
        date1=list(row["date1"]),
        date2=list(row["date2"]),
        photo_count=row["photo_count"],
        photo_delay=row["photo_delay"],
        is_default=row["is_default"],
    )


async def get_or_create_device_config(conn, meter_id: str):
    """
    Get-or-create — confirmed request, round 2. The first version of
    this existed to satisfy a real FK that other tables briefly had
    pointing at device_config(meter_id) — that FK is gone now (see
    "device_config stands alone" further down in README.md), but the
    auto-provisioning behavior itself is still wanted on its own merits:
    device_config should have a real row for every meter_id that's ever
    been seen, confirmed, independent of any referential-integrity
    concern.

    Selects the row if it exists; if not, inserts one with
    DEFAULT_CONFIG values and is_default=true. Takes a connection
    rather than acquiring its own, so a caller already inside a
    transaction (the upload handler) gets this insert folded into that
    same transaction.

    ON CONFLICT (meter_id) DO NOTHING + a follow-up SELECT (rather than
    trusting RETURNING on the INSERT itself) is what makes this safe
    against two requests racing for the same brand-new meter_id at
    once — e.g. two images from the same first-ever burst, or a
    GET /devices/config landing at the same instant as the first
    upload. Whichever INSERT wins, the loser's SELECT still finds a
    real row instead of erroring on the PK collision.

    New rows get is_default=true — this function never sets it false;
    only PUT /admin/device-config/{meter_id} does.
    """
    row = await conn.fetchrow("SELECT * FROM device_config WHERE meter_id = $1", meter_id)
    if row is not None:
        return row
    await conn.execute(
        """
        INSERT INTO device_config (meter_id, schedule_mode, date1, date2, photo_count, photo_delay, is_default)
        VALUES ($1, $2, $3, $4, $5, $6, true)
        ON CONFLICT (meter_id) DO NOTHING
        """,
        meter_id,
        DEFAULT_CONFIG["schedule_mode"],
        DEFAULT_CONFIG["date1"],
        DEFAULT_CONFIG["date2"],
        DEFAULT_CONFIG["photo_count"],
        DEFAULT_CONFIG["photo_delay"],
    )
    return await conn.fetchrow("SELECT * FROM device_config WHERE meter_id = $1", meter_id)


@router.get("/devices/config", response_model=DeviceConfigOut, summary="Get Device Config")
async def get_device_config(
    meter_id: str = Query(..., max_length=16),
    _: CurrentUser = Depends(get_uploader),
):
    """
    NOT part of the original confirmed spec — added from a separate API
    spec doc another team sent, for ESP32 to fetch its own capture
    schedule (schedule_mode/date1/date2/photo_count/photo_delay).

    Auth reuses get_uploader (X-Device-Key, same static key as
    /images/upload — or an admin JWT). The spec doc's own example only
    showed a generic "Authorization: Bearer <token>" header without
    saying which mechanism issues or validates that token — this reuses
    the ESP32 auth this API already has, rather than inventing a second,
    separate one. Flag if a dedicated key/token for this endpoint is
    wanted instead.

    meter_id normalized to uppercase before lookup, same convention as
    every other meter_id in this API (see app/filename.py).

    Per the spec ("หากเป็น Meter ใหม่ที่ยังไม่มีใน Database...ตอบกลับ
    Default Config...พร้อม HTTP Status 200 OK"): a meter_id with no row
    yet still gets DEFAULT_CONFIG back and 200 OK — this NEVER 404s, by
    design, exactly as before.

    **Confirmed, round 2: writes a real row now (get_or_create_device_config()),
    is_default=true** — the response body a caller sees is identical
    either way, but a row now exists afterward. NOT tied to any FK
    concern this time (device_config still stands completely alone, no
    FK from/to any other table) — purely so device_config keeps a real
    record of every meter_id ever seen, confirmed request.
    """
    meter_id = meter_id.strip().upper()
    async with pool().acquire() as conn:
        row = await get_or_create_device_config(conn, meter_id)
    return _config_out(row)


@router.get(
    "/admin/device-config",
    response_model=list[DeviceConfigOut],
    summary="Admin List Device Configs",
)
async def admin_list_device_configs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    NOT in the spec doc — my own addition, needed for a dashboard to show
    "which meters have a custom config" as a list to pick from. Only
    lists meters that actually have a device_config ROW — meters running
    on DEFAULT_CONFIG (never customized) don't show up here at all, since
    there's nothing in this table for them yet. If the dashboard needs
    "every meter, with defaults filled in for the ones missing a row",
    that needs a join against images_*/ocr_jobs for the full set of known
    meter_ids — flag if you want that instead of this simpler version.
    """
    rows = await pool().fetch(
        "SELECT * FROM device_config ORDER BY meter_id LIMIT $1 OFFSET $2",
        limit,
        offset,
    )
    return [_config_out(r) for r in rows]


@router.get(
    "/admin/device-config/{meter_id}",
    response_model=DeviceConfigOut,
    summary="Admin Get Device Config",
)
async def admin_get_device_config(
    meter_id: str,
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    NOT in the spec doc — my own addition, so a dashboard's edit form can
    pre-fill with the meter's CURRENT config before the admin changes
    anything. Falls back to DEFAULT_CONFIG (is_default=true) the same way
    the ESP32-facing endpoint does, rather than 404ing — a dashboard
    opening "edit E999" for a never-configured meter should still see
    sensible starting values to edit from, not an empty form.
    """
    meter_id = meter_id.strip().upper()
    row = await pool().fetchrow("SELECT * FROM device_config WHERE meter_id = $1", meter_id)
    if row is None:
        return DeviceConfigOut(meter_id=meter_id, is_default=True, **DEFAULT_CONFIG)
    return _config_out(row)


@router.put(
    "/admin/device-config/{meter_id}",
    response_model=DeviceConfigOut,
    summary="Admin Set Device Config",
)
async def admin_set_device_config(
    meter_id: str,
    body: DeviceConfigSetRequest,
    _: CurrentUser = Depends(get_current_admin),
):
    """
    NOT in the spec doc at all — my own addition. Without some way to
    actually set a meter's config, GET /devices/config could only ever
    return the same hardcoded DEFAULT_CONFIG for every meter, forever —
    the spec doc describes ESP32 reading config, never how one gets
    written in the first place. Upserts by meter_id (uppercase, same
    convention as everywhere else) — creates the row if new, overwrites
    every field if it already exists (not a partial patch — the
    dashboard is expected to submit the whole form every time, same
    pattern as PUT /admin/images/{item_id}/ocr-manual).

    Flag if you'd rather this not exist at all (e.g. config is meant to
    be seeded directly in the DB, not through the API), or if it should
    require a different credential than a full admin JWT.

    Always writes is_default=false (confirmed) — this is the ONLY place
    that ever does. A deliberate PUT here is exactly what distinguishes
    a real, admin-set config from a row get_or_create_device_config()
    auto-provisioned elsewhere (GET /devices/config above, or the
    upload handler) just from a meter_id showing up — see that
    function's own docstring and is_default's comment in db/init.sql.
    """
    meter_id = meter_id.strip().upper()
    row = await pool().fetchrow(
        """
        INSERT INTO device_config (meter_id, schedule_mode, date1, date2, photo_count, photo_delay, is_default)
        VALUES ($1, $2, $3, $4, $5, $6, false)
        ON CONFLICT (meter_id) DO UPDATE SET
            schedule_mode = EXCLUDED.schedule_mode,
            date1 = EXCLUDED.date1,
            date2 = EXCLUDED.date2,
            photo_count = EXCLUDED.photo_count,
            photo_delay = EXCLUDED.photo_delay,
            is_default = false
        RETURNING *
        """,
        meter_id,
        body.schedule_mode,
        body.date1,
        body.date2,
        body.photo_count,
        body.photo_delay,
    )
    return _config_out(row)


@router.delete(
    "/admin/device-config/{meter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Admin Reset Device Config",
)
async def admin_delete_device_config(
    meter_id: str,
    _: CurrentUser = Depends(get_current_admin),
):
    """
    NOT in the spec doc — my own addition, for a dashboard "reset to
    default" button.

    **Confirmed, round 3: back to an upsert, not a real DELETE.**
    device_config now has real FKs pointing IN from images_*/ocr_jobs/
    ocr_meter/ocr_meter_test/esp32_upload_log again (confirmed,
    deliberately accepting this tradeoff) — an outright DELETE would
    fail (FK violation) for any meter that's ever uploaded a single
    image, which in practice is nearly every meter with a row here to
    reset in the first place. Rewritten as an UPSERT back to
    DEFAULT_CONFIG values with is_default=true instead — same
    externally-visible effect as a real delete would have (every GET
    above goes back to reporting is_default=true for that meter_id,
    indistinguishable from a meter that was never configured), but the
    row itself stays in place so every FK still pointing at it keeps
    working. Idempotent either way — resetting an already-default
    meter just re-writes the same values.
    """
    meter_id = meter_id.strip().upper()
    await pool().execute(
        """
        INSERT INTO device_config (meter_id, schedule_mode, date1, date2, photo_count, photo_delay, is_default)
        VALUES ($1, $2, $3, $4, $5, $6, true)
        ON CONFLICT (meter_id) DO UPDATE SET
            schedule_mode = EXCLUDED.schedule_mode,
            date1 = EXCLUDED.date1,
            date2 = EXCLUDED.date2,
            photo_count = EXCLUDED.photo_count,
            photo_delay = EXCLUDED.photo_delay,
            is_default = true
        """,
        meter_id,
        DEFAULT_CONFIG["schedule_mode"],
        DEFAULT_CONFIG["date1"],
        DEFAULT_CONFIG["date2"],
        DEFAULT_CONFIG["photo_count"],
        DEFAULT_CONFIG["photo_delay"],
    )


@router.put(
    "/admin/device-config/{meter_id}/external-api-key",
    response_model=ExternalApiKeyStatus,
    summary="Admin Set/Rotate External API Key",
)
async def admin_set_external_api_key(
    meter_id: str,
    body: ExternalApiKeySetRequest,
    _: CurrentUser = Depends(get_current_admin),
):
    """
    Confirmed request (ข้อ 18) — an admin pastes in the plaintext API
    key CFO Platform issued for this meter (per the spec's own flow:
    "ผู้ดูแลระบบออก SN และ API key เพิ่มได้ที่เมนู...ระบบจะแสดงกุญแจ
    เพียงครั้งเดียว" — that's CFO Platform's admin UI showing it to a
    human once; this endpoint is where that same human enters it into
    OUR system afterward). Encrypted via app/crypto.py before storage —
    never held in plaintext anywhere past this request handler.

    Upsert, confirmed: calling this again for a meter_id that already
    has a key REPLACES it (rotated_at gets set, is_active forced back
    to true) — matches the spec's own "ออก API key ใหม่ กุญแจเก่าใช้
    ไม่ได้ทันที" flow on CFO Platform's side; this is our system's
    equivalent action when an admin has a new key to enter (old key
    lost, or rotated on CFO Platform's end).

    get_current_admin, not get_admin_or_service — confirmed: entering a
    secret is a sensitive write, deliberately requires a real admin JWT
    login, not a static service key.

    meter_id must already exist in device_config (FK, see db/init.sql)
    — confirmed: raises 404 rather than silently creating a
    device_config row as a side effect, since an admin should set up
    the meter itself first.
    """
    meter_id = meter_id.strip().upper()
    async with pool().acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM device_config WHERE meter_id = $1", meter_id)
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No device_config row for meter_id={meter_id!r} yet — set up the meter itself first.",
            )
        encrypted = encrypt_api_key(body.api_key)
        row = await conn.fetchrow(
            """
            INSERT INTO external_api_keys (meter_id, api_key_encrypted, is_active, rotated_at)
            VALUES ($1, $2, true, now())
            ON CONFLICT (meter_id) DO UPDATE SET
                api_key_encrypted = EXCLUDED.api_key_encrypted,
                is_active = true,
                rotated_at = now(),
                -- new key -> lift any 401/403 pause; queued blocked_auth
                -- rows go out on the next sweep (spec 2.1: ออก key ใหม่)
                auth_failed_at = NULL,
                auth_failed_info = NULL
            RETURNING meter_id, is_active, created_at, rotated_at, auth_failed_at, auth_failed_info
            """,
            meter_id,
            encrypted,
        )
    return ExternalApiKeyStatus(
        meter_id=row["meter_id"],
        has_key=True,
        is_active=row["is_active"],
        created_at=row["created_at"],
        rotated_at=row["rotated_at"],
        auth_failed_at=row["auth_failed_at"],
        auth_failed_info=row["auth_failed_info"],
    )


@router.get(
    "/admin/device-config/{meter_id}/external-api-key",
    response_model=ExternalApiKeyStatus,
    summary="Admin Get External API Key Status",
)
async def admin_get_external_api_key_status(
    meter_id: str,
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    Confirmed: status only — has_key/is_active/created_at/rotated_at —
    never the key itself, plaintext or encrypted (see
    ExternalApiKeyStatus's own docstring for why). get_admin_or_service
    here (not get_current_admin like the PUT above) since this is a
    read with no secret exposed, consistent with this router's other
    GET endpoints.

    Confirmed: has_key=False (not a 404) when no row exists yet — a
    meter with no key configured is a normal, expected state (not yet
    set up for CFO Platform push), not an error.
    """
    meter_id = meter_id.strip().upper()
    row = await pool().fetchrow(
        "SELECT meter_id, is_active, created_at, rotated_at, auth_failed_at, auth_failed_info"
        " FROM external_api_keys WHERE meter_id = $1",
        meter_id,
    )
    if row is None:
        return ExternalApiKeyStatus(meter_id=meter_id, has_key=False)
    return ExternalApiKeyStatus(
        meter_id=row["meter_id"],
        has_key=True,
        is_active=row["is_active"],
        created_at=row["created_at"],
        rotated_at=row["rotated_at"],
        auth_failed_at=row["auth_failed_at"],
        auth_failed_info=row["auth_failed_info"],
    )


@router.post(
    "/admin/device-config/{meter_id}/verify-external",
    response_model=VerifyExternalResult,
    summary="Admin Verify Device Against CFO Platform",
)
async def admin_verify_external_device(
    meter_id: str,
    _: CurrentUser = Depends(get_current_admin),
):
    """
    Confirmed request (ข้อ 22, and spec section 3's own install
    checklist step 2: "เรียก GET /device ตรวจสอบว่าจับคู่ถูกต้อง") —
    calls GET /external/v1/device and saves the descriptive metadata
    it returns (installLocation/meter.meterNo/meter.tenantName/
    meter.locationName) into device_config.external_* — confirmed
    display-only, nothing else in this codebase reads these back to
    make a decision (see VerifyExternalResult's own docstring).

    Confirmed: 400 if no active API key is configured for this meter
    yet (same check as the manual-push endpoint) — verify_device()
    needs a real key to call CFO Platform with, there's nothing to
    verify without one.
    """
    meter_id = meter_id.strip().upper()
    settings = get_settings()
    if not settings.external_api_base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="EXTERNAL_API_BASE_URL is not configured — nothing to verify against yet.",
        )

    key_row = await pool().fetchrow(
        "SELECT api_key_encrypted FROM external_api_keys WHERE meter_id = $1 AND is_active = true",
        meter_id,
    )
    if key_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No active external API key configured for meter_id={meter_id!r}.",
        )
    api_key = decrypt_api_key(key_row["api_key_encrypted"])

    try:
        data = await verify_device(base_url=settings.external_api_base_url, api_key=api_key)
    except ExternalPushClientError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except ExternalPushServerError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=e.message) from e
    except ValueError as e:
        # Configuration problem (e.g. EXTERNAL_API_BASE_URL isn't https).
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e

    # Confirmed fix — spec section 3, install step 2 states this as a
    # requirement, not an optional check: "เรียก GET /device ... ต้อง
    # ได้ SN และเลขที่มิเตอร์ตรงกับตารางในภาคผนวก ก". Without this, an
    # API key pasted into the wrong meter_id's slot in the dashboard
    # (e.g. ELE-000002's key saved under ELE-000001) was silently
    # accepted — every future reading from ELE-000001 would then push
    # to CFO Platform under a key that identifies it as ELE-000002,
    # corrupting the OTHER meter's history with no error anywhere.
    # Case-insensitive on purpose: SNs are uppercased everywhere else
    # in this codebase (see app/filename.py's own meter_id parsing), so
    # a case difference alone shouldn't be treated as a real mismatch.
    # Nothing is written to device_config if this fails — verified
    # below by returning before the UPDATE.
    if data.deviceSn.strip().upper() != meter_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"API key mismatch: this key belongs to device SN {data.deviceSn!r}, "
                f"not {meter_id!r}. Not saving — check which key was pasted into this meter's slot."
            ),
        )

    # Verify passed with this key (spec 3 step 2) — so the key works again
    # (e.g. CFO Platform re-enabled the device / fixed the IP allowlist
    # without issuing a new key). Lift any 401/403 pause so queued
    # blocked_auth rows resume on the next sweep.
    await pool().execute(
        """
        UPDATE external_api_keys
        SET auth_failed_at = NULL, auth_failed_info = NULL
        WHERE meter_id = $1 AND auth_failed_at IS NOT NULL
        """,
        meter_id,
    )

    row = await pool().fetchrow(
        """
        UPDATE device_config
        SET external_install_location = $1,
            external_meter_no = $2,
            external_tenant_name = $3,
            external_location_name = $4
        WHERE meter_id = $5
        RETURNING meter_id
        """,
        data.installLocation,
        data.meter.meterNo,
        data.meter.tenantName,
        data.meter.locationName,
        meter_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No device_config row for meter_id={meter_id!r} yet — set up the meter itself first.",
        )

    return VerifyExternalResult(
        meter_id=meter_id,
        device_sn=data.deviceSn,
        device_type=data.deviceType,
        is_active=data.isActive,
        install_location=data.installLocation,
        meter_no=data.meter.meterNo,
        meter_type=data.meter.type,
        tenant_name=data.meter.tenantName,
        location_name=data.meter.locationName,
    )
