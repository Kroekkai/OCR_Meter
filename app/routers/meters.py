from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import CurrentUser, get_admin_or_service
from app.db import pool, table_for_meter_id
from app.schemas import MeterHistoryEntry, OcrMeterEntry

router = APIRouter(prefix="/admin/meters", tags=["default"])


@router.get("/{meter_id}/history", response_model=list[MeterHistoryEntry], summary="Admin Get Meter History")
async def admin_get_meter_history(
    meter_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    try:
        table = table_for_meter_id(meter_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    rows = await pool().fetch(
        f"""
        SELECT
            img.id AS image_id,
            img.device_timestamp,
            img.ocr_status,
            img.group_id,
            latest.ocr_reading AS latest_ocr_reading,
            latest.status AS latest_job_status
        FROM {table} img
        LEFT JOIN LATERAL (
            SELECT ocr_reading, status
            FROM ocr_jobs
            WHERE image_id = img.group_id
            ORDER BY id DESC
            LIMIT 1
        ) latest ON true
        WHERE img.meter_id = $1
        ORDER BY img.id DESC
        LIMIT $2 OFFSET $3
        """,
        meter_id,
        limit,
        offset,
    )
    return [MeterHistoryEntry(**dict(r)) for r in rows]


@router.get(
    "/{meter_id}/ocr-readings",
    response_model=list[OcrMeterEntry],
    summary="Admin Get Meter Ocr Readings",
)
async def admin_get_meter_ocr_readings(
    meter_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    NOT part of the original confirmed endpoint list — added specifically
    so the OCR client can pull a meter's ocr_meter history itself (most
    recent reading first) to do its own month-over-month comparison
    before calling POST /admin/images/ocr/{job_id}/result with
    error_type='reading_decreased' when needed. Queries ocr_meter only —
    never joins back to images_*/ocr_jobs, per that table's design.
    """
    meter_id = meter_id.strip().lower()  # same normalization as app/filename.py
    rows = await pool().fetch(
        """
        SELECT * FROM ocr_meter
        WHERE meter_id = $1
        ORDER BY reading_date DESC, reading_time DESC
        LIMIT $2 OFFSET $3
        """,
        meter_id,
        limit,
        offset,
    )
    return [OcrMeterEntry(**dict(r)) for r in rows]
