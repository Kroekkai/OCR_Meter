import datetime as dt
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from app.auth import CurrentUser, get_admin_or_service, get_current_admin, get_uploader
from app.config import get_settings
from app.db import GROUP_ID_INFO, pool, table_for_meter_id
from app.filename import FilenameParseError, parse_upload_filename
from app.grouping import finalize_group, has_normal_group_today
from app.repo import get_image_row, image_out
from app.schedule_match import is_on_schedule
from app.schemas import ImageOut, ImageUploadResponse, MeterType, OcrManualEditRequest, OcrStatus
from app import storage

logger = logging.getLogger("ocr_meter_store.images")

router = APIRouter(tags=["default"])


def _stored_filename(original: str, is_test: bool) -> str:
    """
    Appends "_Test" right before the extension when this capture was
    determined off-schedule (server-side, via schedule_match.is_on_schedule
    — never based on anything ESP32 sent) — e.g.
    "E101_20260901_130000_1.jpg" becomes "E101_20260901_130000_1_Test.jpg".
    Applied identically to the file saved on disk and to
    images_*.original_filename, so the two can never disagree — whatever
    filename a human sees browsing /data/images is exactly what's in the
    DB, and vice versa. Every image in a test group gets this treatment,
    not just the anchor (called with that group's shared is_test for
    every insert, both the anchor-creating branch and the join branch).
    """
    if not is_test:
        return original
    p = Path(original)
    return f"{p.stem}_Test{p.suffix}"


@router.post(
    "/images/upload",
    response_model=ImageUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload Image",
)
async def upload_image(
    file: UploadFile = File(...),
    device: CurrentUser = Depends(get_uploader),
):
    """
    meter_id and device_timestamp are NOT sent as separate fields — the
    ESP32-CAM encodes both in the filename itself:
    {meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg (e.g. E101_20260818_151230_01.jpg).
    A filename that doesn't match this, or whose meter_id doesn't start
    with e/w/g, is rejected with 400.

    Joins the image into whichever burst group for this meter_id is
    still open (within settings.image_group_window_seconds of its first
    image), or starts a new group if none is open. Two ways a group
    turns into exactly one ocr_jobs row:
      1. FAST PATH (this function, immediately): once the group reaches
         this meter's target photo count, this upload finalizes it into
         ocr_jobs right here in the same request — no waiting at all.
         Target count is device_config.photo_count for THIS meter_id if
         it has been configured, else settings.image_group_size (the
         system-wide fallback — same one GET /devices/config itself
         falls back to via DEFAULT_CONFIG). Different meters can use
         different burst sizes this way.
         ImageUploadResponse.ocr_job_id is non-null on exactly the
         request that completed the group.
      2. FALLBACK (app/grouping.py's background sweep): for groups that
         never reach that count, once image_group_window_seconds elapses
         since the first image, the sweep finalizes with whatever
         arrived. Whichever happens first wins — a group that hits the
         count never waits for the sweep, and a group that hits the
         window first never waits for more images.
    """
    try:
        meter_id, device_timestamp = parse_upload_filename(file.filename)
        table = table_for_meter_id(meter_id)
    except (FilenameParseError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large")
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    settings = get_settings()
    group_prefix, group_seq = GROUP_ID_INFO[table]

    async with pool().acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE here is what makes this race-safe against the
            # background sweep finalizing this exact group at the same
            # instant — see app/grouping.py's matching FOR UPDATE. Locks
            # the ANCHOR row (is_anchor=true) representing the group,
            # not any old numeric self-reference.
            open_anchor = await conn.fetchrow(
                f"""
                SELECT id, group_id, is_test FROM {table}
                WHERE meter_id = $1
                  AND is_anchor = true
                  AND received_at > now() - ($2 * interval '1 second')
                  AND NOT EXISTS (SELECT 1 FROM ocr_jobs WHERE group_id = {table}.group_id)
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                meter_id,
                settings.image_group_window_seconds,
            )

            if open_anchor is not None:
                # Joins the still-open group — this image is NOT the
                # anchor, so it just copies the anchor's existing
                # group_id (and is_test) rather than pulling a new
                # group_id or recomputing the schedule check — a single
                # burst is always entirely on-schedule or entirely test,
                # never a mix (confirmed). Stored filename gets "_Test"
                # too, matching the anchor — see _stored_filename().
                stored_filename = _stored_filename(file.filename, open_anchor["is_test"])
                image_row = await conn.fetchrow(
                    f"""
                    INSERT INTO {table} (meter_id, original_filename, device_timestamp, ocr_status, group_id, is_anchor, is_test)
                    VALUES ($1, $2, $3, 'pending', $4, false, $5)
                    RETURNING *
                    """,
                    meter_id,
                    stored_filename,
                    device_timestamp,
                    open_anchor["group_id"],
                    open_anchor["is_test"],
                )
            else:
                # No open group — this image starts a new one as its own
                # anchor. group_id pulls from the per-type sequence (E1,
                # E2, ... / W1, W2, ... / G1, G2, ...) — human-readable,
                # unlike the raw row id which jumps around since all 3
                # tables share images_id_seq. is_test is computed ONCE
                # here (server-side comparison against device_config —
                # confirmed NOT based on the filename at all) and
                # inherited by every later image that joins this group.
                # Stored filename gets "_Test" appended when off-schedule
                # — see _stored_filename().
                new_seq_n = await conn.fetchval(f"SELECT nextval('{group_seq}')")
                new_group_id = f"{group_prefix}{new_seq_n}"
                is_test = not await is_on_schedule(conn, meter_id, device_timestamp)
                stored_filename = _stored_filename(file.filename, is_test)
                image_row = await conn.fetchrow(
                    f"""
                    INSERT INTO {table} (meter_id, original_filename, device_timestamp, ocr_status, group_id, is_anchor, is_test)
                    VALUES ($1, $2, $3, 'pending', $4, true, $5)
                    RETURNING *
                    """,
                    meter_id,
                    stored_filename,
                    device_timestamp,
                    new_group_id,
                    is_test,
                )

            # --- Fast path: group just reached this meter's target count? ---
            # Finalize into ocr_jobs immediately, right here, instead of
            # waiting for the sweep to notice on its next tick (up to
            # group_sweep_interval_seconds later) or for the window to
            # close (up to image_group_window_seconds later). The target
            # count is THIS meter's own device_config.photo_count if it
            # has been configured — falling back to the system-wide
            # settings.image_group_size only for a meter that's never
            # been configured (same fallback GET /devices/config itself
            # uses via DEFAULT_CONFIG). Different meters can have
            # different burst sizes this way.
            ocr_job_id = None
            target_count = await conn.fetchval(
                "SELECT photo_count FROM device_config WHERE meter_id = $1", meter_id
            )
            if target_count is None:
                target_count = settings.image_group_size
            group_count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE group_id = $1",
                image_row["group_id"],
            )
            if group_count >= target_count:
                # Re-fetch the anchor row locked — need its
                # original_filename/device_timestamp to copy into
                # ocr_jobs, and FOR UPDATE + the NOT EXISTS check right
                # after is what prevents two uploads that both push the
                # count over the line at nearly the same instant from
                # both creating a job for the same group (mirrors the
                # same race-safety the sweep already has).
                anchor_row = await conn.fetchrow(
                    f"SELECT * FROM {table} WHERE group_id = $1 AND is_anchor = true FOR UPDATE",
                    image_row["group_id"],
                )
                already_has_job = await conn.fetchval(
                    "SELECT 1 FROM ocr_jobs WHERE group_id = $1", image_row["group_id"]
                )
                if anchor_row is not None and not already_has_job:
                    if not anchor_row["is_test"] and await has_normal_group_today(conn, table, meter_id):
                        # Confirmed rule: at most one normal (non-test)
                        # group per meter per day may reach ocr_jobs —
                        # this meter already has one today, so drop this
                        # group silently. No job, no error, ocr_job_id
                        # just stays null — looks identical to a group
                        # that simply hasn't finished yet, and the
                        # images_* rows are left exactly as they are
                        # (confirmed: never deleted, ocr_status stays
                        # 'pending' forever). Test groups skip this
                        # check entirely — no daily limit for them.
                        logger.info(
                            "dropped duplicate normal group %s for meter %s — already has one today",
                            image_row["group_id"],
                            meter_id,
                        )
                    else:
                        ocr_job_id = await finalize_group(conn, anchor_row)

    # Save under the STORED filename (image_row["original_filename"]),
    # not the raw file.filename ESP32 sent — these now differ whenever
    # is_test appended "_Test" above. Using the stored one keeps the disk
    # file and the DB row in exact agreement always.
    await storage.save_upload(image_row["id"], image_row["original_filename"], data)

    return ImageUploadResponse(
        image=image_out(table, image_row),
        group_id=image_row["group_id"],
        ocr_job_id=ocr_job_id,
    )


@router.get("/admin/images", response_model=list[ImageOut], summary="Admin List Images")
async def admin_list_images(
    meter_type: MeterType | None = Query(default=None),
    meter_id: str | None = Query(default=None),
    ocr_status: OcrStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    tables = [f"images_{meter_type}"] if meter_type else ["images_electric", "images_water", "images_gas"]
    if meter_id:
        meter_id = meter_id.strip().upper()  # meter_id is always stored uppercase — see app/filename.py

    results: list[ImageOut] = []
    for table in tables:
        clauses, params = [], []
        if meter_id:
            params.append(meter_id)
            clauses.append(f"meter_id = ${len(params)}")
        if ocr_status:
            params.append(ocr_status)
            clauses.append(f"ocr_status = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await pool().fetch(f"SELECT * FROM {table} {where} ORDER BY id DESC", *params)
        results.extend(image_out(table, r) for r in rows)

    results.sort(key=lambda i: i.id, reverse=True)
    return results[offset : offset + limit]


@router.get("/admin/images/{item_id}", response_model=ImageOut, summary="Admin Get Image")
async def admin_get_image(item_id: int, _: CurrentUser = Depends(get_admin_or_service)):
    table, row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return image_out(table, row)


@router.delete("/admin/images/{item_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Admin Delete Image")
async def admin_delete_image(item_id: int, _: CurrentUser = Depends(get_current_admin)):
    """
    Deletes just this one image. If item_id happens to be a group's
    anchor (is_anchor=true), its ocr_jobs row(s) are cleaned up too —
    same as before, just keyed off is_anchor now instead of the old
    "group_id equals my own id" self-reference. NOTE: other images still
    in the same burst group are left behind with group_id pointing at a
    group whose anchor is now gone — they're not auto-deleted or
    re-anchored. Harmless (group_id is a plain value, not an enforced
    FK) but worth knowing before deleting an anchor.
    """
    table, row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    async with pool().acquire() as conn:
        async with conn.transaction():
            if row["is_anchor"]:
                await conn.execute("DELETE FROM ocr_jobs WHERE group_id = $1", row["group_id"])
            await conn.execute(f"DELETE FROM {table} WHERE id = $1", item_id)

    storage.delete_files(item_id, row["original_filename"])


@router.post(
    "/admin/images/{item_id}/reprocess",
    response_model=ImageOut,
    summary="Admin Reprocess Image",
)
async def admin_reprocess_image(item_id: int, _: CurrentUser = Depends(get_current_admin)):
    """
    Re-queues item_id's WHOLE burst group (not just this one image) —
    creates a brand-new ocr_jobs row referencing the group's anchor, per
    db/init.sql: "รูปเดียว reprocess ได้หลายรอบ แต่ละรอบสร้างแถว job ใหม่
    ไม่ทับของเดิม" — reprocessing never overwrites the previous job row,
    so OCR history for the group is preserved. Every image in the group
    (including item_id itself) gets ocr_status reset to 'pending'.
    """
    table, row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    group_id = row["group_id"]
    anchor = await pool().fetchrow(f"SELECT * FROM {table} WHERE group_id = $1 AND is_anchor = true", group_id)
    if anchor is None:
        # Anchor was deleted separately (see admin_delete_image's note) —
        # fall back to this image's own data so reprocess still works.
        anchor = row

    async with pool().acquire() as conn:
        async with conn.transaction():
            await finalize_group(conn, anchor)
            await conn.execute(f"UPDATE {table} SET ocr_status = 'pending' WHERE group_id = $1", group_id)
            updated = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1", item_id)
    return image_out(table, updated)


@router.get("/admin/images/{item_id}/file", summary="Admin Get Image File")
async def admin_get_image_file(item_id: int, _: CurrentUser = Depends(get_admin_or_service)):
    table, row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    path = storage.original_path(item_id, row["original_filename"])
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image file missing on disk")
    return FileResponse(path, media_type="image/jpeg")


# GET .../ocr-result-file removed — there's no separate "OCR result"
# file anymore (see app/routers/ocr_jobs.py's /result docstring). For
# the same image on an error/anomaly row, use this same endpoint
# (/admin/images/{item_id}/file) — ocr_meter.image_error names exactly
# this file, nothing new was ever written.


@router.put("/admin/images/{item_id}/ocr-manual", summary="Admin Edit Ocr Manually")
async def admin_edit_ocr_manually(
    item_id: int,
    body: OcrManualEditRequest,
    admin: CurrentUser = Depends(get_current_admin),
):
    """
    Overwrites ocr_reading on this image's group's most recent ocr_jobs
    row. Looks the job up via item_id's group_id (works the same whether
    item_id is the anchor or any other image in the group, since every
    image in a group shares one group_id value now — unlike the old
    numeric scheme, where only the anchor's own id equaled group_id).
    Per db/init.sql there is no column that preserves the OCR-produced
    value once an admin overwrites it here, and no column that records
    why (admin_reason was removed).
    """
    table, image_row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    group_id = image_row["group_id"]
    latest_job = await pool().fetchrow(
        "SELECT id FROM ocr_jobs WHERE group_id = $1 ORDER BY id DESC LIMIT 1",
        group_id,
    )
    if latest_job is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This image's group has no ocr_jobs row yet — upload/reprocess first",
        )

    async with pool().acquire() as conn:
        async with conn.transaction():
            updated_job = await conn.fetchrow(
                """
                UPDATE ocr_jobs
                SET ocr_reading = $1, status = 'done'
                WHERE id = $2
                RETURNING *
                """,
                body.ocr_reading,
                latest_job["id"],
            )
            await conn.execute(f"UPDATE {table} SET ocr_status = 'done' WHERE group_id = $1", group_id)

    return dict(updated_job)
