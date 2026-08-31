from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import CurrentUser, get_admin_or_service, get_current_admin, get_uploader
from app.db import pool
from app.schemas import DeviceConfigOut, DeviceConfigSetRequest

router = APIRouter(tags=["default"])

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
        is_default=False,
    )


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
    yet gets DEFAULT_CONFIG back — this NEVER 404s, by design.
    """
    meter_id = meter_id.strip().upper()
    row = await pool().fetchrow("SELECT * FROM device_config WHERE meter_id = $1", meter_id)
    if row is None:
        return DeviceConfigOut(meter_id=meter_id, is_default=True, **DEFAULT_CONFIG)
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
    """
    meter_id = meter_id.strip().upper()
    row = await pool().fetchrow(
        """
        INSERT INTO device_config (meter_id, schedule_mode, date1, date2, photo_count, photo_delay)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (meter_id) DO UPDATE SET
            schedule_mode = EXCLUDED.schedule_mode,
            date1 = EXCLUDED.date1,
            date2 = EXCLUDED.date2,
            photo_count = EXCLUDED.photo_count,
            photo_delay = EXCLUDED.photo_delay
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
    default" button. Removes the meter's row entirely — after this,
    GET /devices/config (and the two admin GETs above) go back to
    returning DEFAULT_CONFIG with is_default=true for this meter_id,
    same as a meter that was never configured at all. Not an error if
    the meter had no row to begin with (idempotent — deleting an
    already-default meter is a no-op, not a 404).
    """
    meter_id = meter_id.strip().upper()
    await pool().execute("DELETE FROM device_config WHERE meter_id = $1", meter_id)
