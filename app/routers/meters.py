from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import CurrentUser, get_admin_or_service
from app.db import pool, table_for_meter_id
from app.schemas import MeterHistoryEntry, OcrMeterEntry

router = APIRouter(prefix="/admin/meters", tags=["default"])


async def _list_ocr_meter_rows(table: str, meter_id: str | None, limit: int, offset: int) -> list[OcrMeterEntry]:
    """
    Shared query behind GET /admin/meters/ocr-meter and .../ocr-meter-test
    — only the table name differs, everything else (optional meter_id
    filter, ordering, pagination) is identical.
    """
    clauses, params = [], []
    if meter_id:
        params.append(meter_id.strip().upper())  # same normalization as app/filename.py
        clauses.append(f"meter_id = ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = await pool().fetch(
        f"""
        SELECT * FROM {table}
        {where}
        ORDER BY capture_date DESC, capture_time DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [OcrMeterEntry(**dict(r)) for r in rows]


@router.get("/{meter_id}/history", response_model=list[MeterHistoryEntry], summary="Admin Get Meter History")
async def admin_get_meter_history(
    meter_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    meter_id = meter_id.strip().upper()  # meter_id is always stored uppercase — see app/filename.py
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
            WHERE group_id = img.group_id
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
    only_successful: bool = Query(
        default=False,
        description=(
            "When true, only returns error_type=0 (successful) rows — for building "
            "trustworthy consumption history without error rows mixed in."
        ),
    ),
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    NOT part of the original confirmed endpoint list — added specifically
    so the OCR client can pull a meter's ocr_meter history itself (most
    recent reading first) for its own use (e.g. building a consumption
    baseline). Queries ocr_meter only — never joins back to
    images_*/ocr_jobs, per that table's design.
    """
    meter_id = meter_id.strip().upper()  # same normalization as app/filename.py

    clauses, params = ["meter_id = $1"], [meter_id]
    if only_successful:
        clauses.append("error_type = 0")
    where = " AND ".join(clauses)
    params.extend([limit, offset])

    rows = await pool().fetch(
        f"""
        SELECT * FROM ocr_meter
        WHERE {where}
        ORDER BY capture_date DESC, capture_time DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [OcrMeterEntry(**dict(r)) for r in rows]


@router.get(
    "/ocr-meter",
    response_model=list[OcrMeterEntry],
    summary="Admin List Ocr Meter Results",
)
async def admin_list_ocr_meter(
    meter_id: str | None = Query(default=None, description="Optional — omit to list across every meter"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    NOT in either original spec — added for explicit separation from
    ocr_meter_test (confirmed request). Unlike GET .../{meter_id}/ocr-readings
    above (scoped to one meter, exists specifically for the OCR client's
    own anomaly-check history), this lists across ALL meters by default —
    meter_id narrows it to one when given. Only ever returns rows from
    the real ocr_meter table (on-schedule results — see
    app/filename.py::is_test_filename()) — never mixes in
    ocr_meter_test, no matter what.
    """
    return await _list_ocr_meter_rows("ocr_meter", meter_id, limit, offset)


@router.get(
    "/ocr-meter-test",
    response_model=list[OcrMeterEntry],
    summary="Admin List Ocr Meter Test Results",
)
async def admin_list_ocr_meter_test(
    meter_id: str | None = Query(default=None, description="Optional — omit to list across every meter"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    """
    Mirror of GET .../ocr-meter directly above — identical shape,
    identical filters, but reads ocr_meter_test instead: results from
    groups the server determined were off-schedule (filename carries a
    "_Test" suffix — see app/filename.py::is_test_filename() and
    app/schedule_match.py). Completely separate table, no FK between the
    two, no query anywhere joins them together — a meter's test results
    and its real results never mix in either endpoint's response.
    """
    return await _list_ocr_meter_rows("ocr_meter_test", meter_id, limit, offset)
