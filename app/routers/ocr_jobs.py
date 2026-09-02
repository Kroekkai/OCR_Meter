import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status

from app.auth import CurrentUser, get_admin_or_service, get_ocr_client
from app.db import pool, table_for_group_id
from app.filename import BANGKOK_TZ
from app.repo import get_group_images
from app.schemas import JobStatus, OcrClaimResponse, OcrFailRequest, OcrJobOut, OcrMeterEntry
from app import storage

logger = logging.getLogger("ocr_meter_store.ocr_jobs")

router = APIRouter(prefix="/admin/images/ocr", tags=["default"])


def _job_out(row) -> OcrJobOut:
    return OcrJobOut(**dict(row))


def _meter_out(row) -> OcrMeterEntry:
    return OcrMeterEntry(**dict(row))


def _capture_date_time_from_device_timestamp(device_timestamp: dt.datetime | None) -> tuple[dt.date, dt.time]:
    """
    capture_date/capture_time mean "when ESP32 captured the photo", not
    "when OCR ran" — pulled from the job's own device_timestamp (already
    stored, denormalized from the anchor image) rather than anything the
    OCR client sends. device_timestamp comes back from asyncpg as a
    UTC-aware datetime (Postgres stores TIMESTAMPTZ as UTC internally) —
    convert back to Bangkok local time first, or the date could be off
    by a day near midnight, and the time would be wrong by 7 hours.
    (Function/columns used to be called reading_date/reading_time.)

    device_timestamp is nullable in the schema — falls back to the
    current server time (Bangkok) in the rare case it's missing, so this
    never fails outright.
    """
    if device_timestamp is not None:
        local = device_timestamp.astimezone(BANGKOK_TZ)
    else:
        local = dt.datetime.now(BANGKOK_TZ)
    return local.date(), local.time()


@router.get("", response_model=list[OcrJobOut], summary="Admin List Ocr Jobs")
async def admin_list_ocr_jobs(
    job_status: JobStatus | None = Query(default=None),
    meter_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_admin_or_service),
):
    clauses, params = [], []
    if job_status:
        params.append(job_status)
        clauses.append(f"status = ${len(params)}")
    if meter_id:
        params.append(meter_id.strip().upper())  # meter_id is always stored uppercase — see app/filename.py
        clauses.append(f"meter_id = ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = await pool().fetch(
        f"SELECT * FROM ocr_jobs {where} ORDER BY id ASC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
        *params,
    )
    return [_job_out(r) for r in rows]


@router.post("/{job_id}/claim", response_model=OcrClaimResponse, summary="Admin Claim Ocr Job")
async def admin_claim_ocr_job(job_id: int, _: CurrentUser = Depends(get_ocr_client)):
    """
    queued -> processing, attempts += 1.

    Only allowed while the job is still 'queued'. This is the first half
    of the state-machine guard: a job that's already being processed (or
    already terminal) can't be claimed again, so two OCR client instances
    can't double-process the same job, and a client can't re-claim
    something it already finished.

    image_file_urls lists EVERY image sharing the job's group_id (E1/W3/G12
    — see db/init.sql) — download and OCR all of them, then submit only
    the single best result via /result.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM ocr_jobs WHERE id = $1 FOR UPDATE", job_id)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
            if row["status"] != "queued":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Job {job_id} is '{row['status']}', not 'queued' — cannot claim",
                )
            updated = await conn.fetchrow(
                """
                UPDATE ocr_jobs SET status = 'processing', attempts = attempts + 1
                WHERE id = $1
                RETURNING *
                """,
                job_id,
            )

    table = table_for_group_id(updated["group_id"])
    group_images = await get_group_images(table, updated["group_id"])
    urls = [f"/admin/images/{img['id']}/file" for img in group_images]

    return OcrClaimResponse(job=_job_out(updated), image_file_urls=urls)


@router.post("/{job_id}/result", response_model=OcrMeterEntry, summary="Admin Submit Ocr Result")
async def admin_submit_ocr_result(
    job_id: int,
    ocr_reading: float | None = Form(
        default=None,
        description="Required when error_type is 0 or 3. Must be omitted for error_type 1 or 2 — there is no reading to report.",
    ),
    error_type: int = Form(
        ...,
        description=(
            "Always required. 0 = read successfully, 1 = found the meter but couldn't read the "
            "digits, 2 = couldn't find any digits/meter in the image at all, 3 = read a value "
            "successfully but it's anomalous (decreased from last time, or usage spike — the OCR "
            "client checks this itself against GET .../ocr-readings history, server doesn't compute "
            "it). Full definitions live server-side in the error_type table — the OCR client just "
            "reports which code applies. Must be 0, 1, 2, or 3 — validated by hand in the function "
            "body rather than via a Literal[0,1,2,3] type: that type annotation on a Form() field "
            "doesn't reliably coerce the string \"0\"/\"1\"/etc. that multipart/form-data always sends "
            "into the matching int, and rejects it outright with a confusing 422 instead — confirmed "
            "against a real request while testing, not just theoretical."
        ),
    ),
    _: CurrentUser = Depends(get_ocr_client),
):
    """
    processing -> done (ocr_jobs), plus one new row in ocr_meter — the
    clean, standalone results table with no FK back to images_*/ocr_jobs.

    error_type is always required now (0/1/2/3, not free-form business
    error strings) — see db/init.sql's error_type lookup table for what
    each code means; the server owns those definitions, the OCR client
    only ever reports which one applies. Case 3 covers what used to be
    two separate cases (reading_decreased/usage_anomaly) — the OCR
    client is still the one that checks history and decides, server just
    stores whichever code it reports.

    capture_date/capture_time are no longer client-supplied — they're
    derived from the job's own device_timestamp (when ESP32 captured the
    photo), not from anything in this request. ocr_meter does NOT carry
    group_id (confirmed) — that's an internal images_*/ocr_jobs concern
    only; ocr_meter stays just the 6 confirmed fields.

    No file upload here at all anymore — plain form fields, not
    multipart. ocr_meter.image_error (only set when error_type != 0) is
    the FULL disk path (via storage.original_path()) to the job's own
    original_filename — the SAME file the anchor image was already
    stored under at upload time, e.g. "/data/images/E101_..._01.jpg", not
    just the bare filename. The OCR client has no image of its own to
    contribute here; it never captured anything, ESP32 did, and that
    file is already on disk. An earlier version of this endpoint
    accepted a re-uploaded copy via a result_image field —
    removed, since it added nothing (the OCR client can only ever
    legitimately attach one of the group's own already-stored photos
    anyway) and could silently overwrite a *different* image in the same
    group on disk (the save path was always computed from the anchor's
    filename, regardless of which photo's bytes were actually attached).

    Only allowed on a job this client actually claimed (status='processing') —
    same state-machine guard as /claim and /fail.
    """
    VALID_CODES = (0, 1, 2, 3)
    NO_READING_CODES = (1, 2)
    HAS_READING_CODES = (0, 3)

    if error_type not in VALID_CODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"error_type must be 0, 1, 2, or 3 — got {error_type!r}",
        )
    if error_type in HAS_READING_CODES and ocr_reading is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ocr_reading is required when error_type={error_type}",
        )
    if error_type in NO_READING_CODES and ocr_reading is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ocr_reading must be omitted when error_type={error_type} — there is no reading to report",
        )

    async with pool().acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT * FROM ocr_jobs WHERE id = $1 FOR UPDATE", job_id)
            if job is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
            if job["status"] != "processing":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Job {job_id} is '{job['status']}', not 'processing' — call /claim first",
                )

            capture_date, capture_time = _capture_date_time_from_device_timestamp(job["device_timestamp"])

            # No file write at all — just reference the anchor's filename,
            # already sitting on disk since the original ESP32 upload.
            # image_error stores the FULL disk path now (e.g.
            # "/data/images/E101_20260829_100000_01.jpg"), not just the
            # bare filename — storage.original_path() is the single place
            # that computes this path (same helper used to actually save/
            # serve the file), so this is guaranteed to match reality.
            # The filename itself (last path segment) is unchanged — still
            # exactly the ESP32's original filename.
            image_error = (
                str(storage.original_path(job["group_id"], job["original_filename"]))
                if error_type != 0
                else None
            )

            target_table = "ocr_meter_test" if job["is_test"] else "ocr_meter"
            meter_row = await conn.fetchrow(
                f"""
                INSERT INTO {target_table}
                    (meter_id, capture_date, capture_time, ocr_reading, error_type, image_error)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                job["meter_id"],
                capture_date,
                capture_time,
                ocr_reading,
                error_type,
                image_error,
            )

            await conn.execute(
                "UPDATE ocr_jobs SET status = 'done', ocr_reading = $1 WHERE id = $2",
                ocr_reading,
                job_id,
            )
            table = table_for_group_id(job["group_id"])
            # job["group_id"] is shared by every image in the burst group
            # — mark all of them 'done', not just the anchor, since OCR
            # considered (and picked from) all of them.
            await conn.execute(f"UPDATE {table} SET ocr_status = 'done' WHERE group_id = $1", job["group_id"])

    return _meter_out(meter_row)


@router.post("/{job_id}/fail", response_model=OcrJobOut, summary="Admin Report Ocr Failure")
async def admin_report_ocr_failure(
    job_id: int,
    body: OcrFailRequest,
    _: CurrentUser = Depends(get_ocr_client),
):
    """
    processing -> failed (terminal). For *transient/technical* failures
    only (network error, OCR_API_URL not configured, download failed,
    etc.) — anything where the OCR process never got far enough to reach
    a definitive outcome. Does NOT write to ocr_meter. A definitive
    business outcome — including "couldn't read the digits" or "no
    digits found at all" — goes through /result instead (error_type 1/2),
    not here.

    body.error is logged server-side (see logger.warning below) but NOT
    persisted anywhere — ocr_jobs.last_error was removed. If you need
    that failure reason preserved for later debugging, it only lives in
    the server's own logs now, not queryable via the API.

    Same state-machine guard as /claim and /result: only accepts a job
    that's currently 'processing' — once it's terminal, this returns 409
    instead of silently incrementing attempts (the original fix for jobs
    with hundreds/thousands of attempts on an already-dead config error).
    A human has to explicitly re-queue it via
    POST /admin/images/{item_id}/reprocess, which starts a fresh job row
    at attempts=0.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM ocr_jobs WHERE id = $1 FOR UPDATE", job_id)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
            if row["status"] != "processing":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Job {job_id} is '{row['status']}', not 'processing' — "
                        "it cannot be failed again. Re-queue it via /reprocess if it needs another attempt."
                    ),
                )
            logger.warning("ocr_jobs %s reported failed: %s", job_id, body.error)
            updated = await conn.fetchrow(
                "UPDATE ocr_jobs SET status = 'failed' WHERE id = $1 RETURNING *",
                job_id,
            )
            table = table_for_group_id(updated["group_id"])
            # Same group-wide update as /result — see that endpoint's
            # comment. A technical failure applies to the whole attempt
            # (all images in the group), not just the anchor.
            await conn.execute(
                f"UPDATE {table} SET ocr_status = 'failed' WHERE group_id = $1", updated["group_id"]
            )
    return _job_out(updated)
