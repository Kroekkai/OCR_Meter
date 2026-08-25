import datetime as dt

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from app.auth import CurrentUser, get_admin_or_service, get_current_admin, get_uploader
from app.config import get_settings
from app.db import pool, table_for_meter_id
from app.filename import FilenameParseError, parse_upload_filename
from app.repo import get_image_row, image_out
from app.schemas import ImageOut, ImageUploadResponse, MeterType, OcrManualEditRequest, OcrStatus
from app import storage

router = APIRouter(tags=["default"])


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
    {meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg (e.g. e101_20260818_151230_01.jpg).
    A filename that doesn't match this, or whose meter_id doesn't start
    with e/w/g, is rejected with 400.

    Does NOT create an ocr_jobs row directly — the device sends multiple
    photos per reading (a "burst"), and this joins the image into
    whichever burst group for this meter_id is still open (within
    settings.image_group_window_seconds of its first image), or starts a
    new group if none is open. A background sweep (app/grouping.py)
    creates exactly one ocr_jobs row per group once its window closes —
    see ImageUploadResponse's docstring.
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

    window_seconds = get_settings().image_group_window_seconds

    async with pool().acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE here is what makes this race-safe against the
            # background sweep finalizing this exact group at the same
            # instant — see app/grouping.py's matching FOR UPDATE.
            open_anchor = await conn.fetchrow(
                f"""
                SELECT id FROM {table}
                WHERE meter_id = $1
                  AND group_id = id
                  AND received_at > now() - ($2 * interval '1 second')
                  AND NOT EXISTS (SELECT 1 FROM ocr_jobs WHERE image_id = {table}.id)
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                meter_id,
                window_seconds,
            )

            if open_anchor is not None:
                # Joins the still-open group — this image is NOT the anchor.
                image_row = await conn.fetchrow(
                    f"""
                    INSERT INTO {table} (meter_id, original_filename, device_timestamp, ocr_status, group_id)
                    VALUES ($1, $2, $3, 'pending', $4)
                    RETURNING *
                    """,
                    meter_id,
                    file.filename,
                    device_timestamp,
                    open_anchor["id"],
                )
            else:
                # No open group — this image starts a new one as its own
                # anchor (group_id = its own id). Pulls the id from the
                # sequence explicitly so both columns can be set in one
                # INSERT instead of insert-then-update.
                new_id = await conn.fetchval("SELECT nextval('images_id_seq')")
                image_row = await conn.fetchrow(
                    f"""
                    INSERT INTO {table} (id, meter_id, original_filename, device_timestamp, ocr_status, group_id)
                    VALUES ($1, $2, $3, $4, 'pending', $1)
                    RETURNING *
                    """,
                    new_id,
                    meter_id,
                    file.filename,
                    device_timestamp,
                )

    await storage.save_upload(image_row["id"], file.filename, data)

    return ImageUploadResponse(image=image_out(table, image_row), group_id=image_row["group_id"])


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
    Deletes just this one image (+ its own ocr_jobs row, if it happens to
    be a group anchor with one). NOTE: if item_id is a group anchor with
    other images still in the same burst group, those siblings are left
    behind with group_id pointing at a now-gone row — they're not
    auto-deleted or re-anchored. Harmless (group_id is a plain value, not
    an enforced FK) but worth knowing before deleting an anchor.
    """
    table, row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM ocr_jobs WHERE image_id = $1", item_id)
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
    anchor = await pool().fetchrow(f"SELECT * FROM {table} WHERE id = $1", group_id)
    if anchor is None:
        # Anchor was deleted separately (see admin_delete_image's note) —
        # fall back to this image's own data so reprocess still works.
        anchor = row

    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO ocr_jobs (image_id, meter_id, original_filename, device_timestamp, status)
                VALUES ($1, $2, $3, $4, 'queued')
                """,
                group_id,
                anchor["meter_id"],
                anchor["original_filename"],
                anchor["device_timestamp"],
            )
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


@router.get("/admin/images/{item_id}/ocr-result-file", summary="Admin Get Image Ocr Result File")
async def admin_get_ocr_result_file(item_id: int, _: CurrentUser = Depends(get_admin_or_service)):
    table, row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    path = storage.ocr_result_path(item_id, row["original_filename"])
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No OCR result image for this item yet")
    return FileResponse(path, media_type="image/jpeg")


@router.put("/admin/images/{item_id}/ocr-manual", summary="Admin Edit Ocr Manually")
async def admin_edit_ocr_manually(
    item_id: int,
    body: OcrManualEditRequest,
    admin: CurrentUser = Depends(get_current_admin),
):
    """
    Overwrites ocr_reading on this image's group's most recent ocr_jobs
    row and records why, in admin_reason. ocr_jobs.image_id always points
    at a group's anchor (see db/init.sql), never at an arbitrary member
    image directly — so this looks the job up via item_id's group_id, not
    item_id itself. Per db/init.sql there is no column that preserves the
    OCR-produced value once an admin overwrites it here.
    """
    table, image_row = await get_image_row(item_id)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    group_id = image_row["group_id"]
    latest_job = await pool().fetchrow(
        "SELECT id FROM ocr_jobs WHERE image_id = $1 ORDER BY id DESC LIMIT 1",
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
                SET ocr_reading = $1, admin_reason = $2, status = 'done'
                WHERE id = $3
                RETURNING *
                """,
                body.ocr_reading,
                body.admin_reason,
                latest_job["id"],
            )
            await conn.execute(f"UPDATE {table} SET ocr_status = 'done' WHERE group_id = $1", group_id)

    return dict(updated_job)
