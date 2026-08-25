import datetime as dt

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.auth import CurrentUser, get_admin_or_service, get_ocr_client
from app.config import get_settings
from app.db import pool
from app.repo import find_image_table, get_group_images
from app.schemas import JobStatus, OcrClaimResponse, OcrErrorType, OcrFailRequest, OcrJobOut, OcrMeterEntry
from app import storage

router = APIRouter(prefix="/admin/images/ocr", tags=["default"])


def _job_out(row) -> OcrJobOut:
    return OcrJobOut(**dict(row))


def _meter_out(row) -> OcrMeterEntry:
    return OcrMeterEntry(**dict(row))


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
        params.append(meter_id)
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

    image_file_urls lists EVERY image in the job's burst group (job.image_id
    is the group's anchor — see db/init.sql) — download and OCR all of
    them, then submit only the single best result via /result.
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

    table = await find_image_table(updated["image_id"])
    urls = [f"/admin/images/{updated['image_id']}/file"]
    if table:
        group_images = await get_group_images(table, updated["image_id"])
        urls = [f"/admin/images/{img['id']}/file" for img in group_images]

    return OcrClaimResponse(job=_job_out(updated), image_file_urls=urls)


@router.post("/{job_id}/result", response_model=OcrMeterEntry, summary="Admin Submit Ocr Result")
async def admin_submit_ocr_result(
    job_id: int,
    reading_date: dt.date = Form(..., description="Date OCR performed this reading"),
    reading_time: dt.time = Form(..., description="Time OCR performed this reading"),
    ocr_reading: float | None = Form(
        default=None,
        description="Required for success and for error_type in (reading_decreased, usage_anomaly). Must be omitted for the other two error types.",
    ),
    error_type: OcrErrorType | None = Form(
        default=None,
        description="Omit entirely for a successful read. One of no_digits_found / image_unreadable / reading_decreased / usage_anomaly otherwise.",
    ),
    error_detail: str | None = Form(default=None, max_length=2000),
    result_image: UploadFile | None = File(
        default=None, description="Optional OCR-annotated image, stored alongside the original upload."
    ),
    _: CurrentUser = Depends(get_ocr_client),
):
    """
    processing -> done (ocr_jobs), plus one new row in ocr_meter — the
    clean, standalone results table with no FK back to images_*/ocr_jobs.

    This single endpoint covers BOTH a successful read and the 4 known
    business-error cases (no_digits_found, image_unreadable,
    reading_decreased, usage_anomaly) — all four are *definitive*
    outcomes, not retryable technical failures, so they belong here
    rather than in /fail. reading_decreased and usage_anomaly are both
    decided by the OCR client itself: it pulls this meter's history
    (GET /admin/meters/{meter_id}/ocr-readings, ?only_successful=true for
    usage_anomaly specifically — see that endpoint's docstring) and
    compares before calling this endpoint — the server does not compute
    either comparison. Both carry the current (newest) reading as
    ocr_reading, same as a normal success — only error_type marks it
    as anomalous.

    Only allowed on a job this client actually claimed (status='processing') —
    same state-machine guard as /claim and /fail.
    """
    NO_READING_ERRORS = ("no_digits_found", "image_unreadable")
    ANOMALOUS_READING_ERRORS = ("reading_decreased", "usage_anomaly")

    if error_type is None and ocr_reading is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ocr_reading is required when error_type is not set (success case)",
        )
    if error_type in NO_READING_ERRORS and ocr_reading is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ocr_reading must be omitted when error_type={error_type!r} — there is no reading to report",
        )
    if error_type in ANOMALOUS_READING_ERRORS and ocr_reading is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ocr_reading is required when error_type={error_type!r} — it's the anomalous reading itself",
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

            ocr_image_filename = None
            if result_image is not None:
                data = await result_image.read()
                if data:
                    path = await storage.save_upload(
                        job["image_id"], job["original_filename"], data, is_ocr_result=True
                    )
                    ocr_image_filename = path.name

            meter_row = await conn.fetchrow(
                """
                INSERT INTO ocr_meter (meter_id, reading_date, reading_time, ocr_reading, error_type, error_detail, ocr_image_filename)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                job["meter_id"],
                reading_date,
                reading_time,
                ocr_reading,
                error_type,
                error_detail,
                ocr_image_filename,
            )

            await conn.execute(
                "UPDATE ocr_jobs SET status = 'done', ocr_reading = $1 WHERE id = $2",
                ocr_reading,
                job_id,
            )
            table = await find_image_table(job["image_id"])
            if table:
                # job["image_id"] is the group's anchor — mark every image
                # in the burst group 'done', not just the anchor, since
                # OCR considered (and picked from) all of them.
                await conn.execute(f"UPDATE {table} SET ocr_status = 'done' WHERE group_id = $1", job["image_id"])

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
    a definitive outcome. Does NOT write to ocr_meter; ocr_jobs.last_error
    is purely internal-queue bookkeeping. A definitive business outcome —
    including "no digits found" or "image unreadable" — goes through
    /result instead (with the matching error_type), not here.

    This is the fix for jobs like the ones seen with hundreds/thousands of
    attempts and a config error: previously nothing stopped a job that was
    already 'failed' from being reported failed again and again, so
    attempts climbed forever on an already-dead job. Now /fail (like
    /claim and /result) only accepts a job that's currently 'processing' —
    once it's terminal, this returns 409 instead of silently incrementing
    attempts. A human has to explicitly re-queue it via
    POST /admin/images/{item_id}/reprocess, which starts a fresh job row
    at attempts=0.
    """
    settings = get_settings()
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
            updated = await conn.fetchrow(
                "UPDATE ocr_jobs SET status = 'failed', last_error = $1 WHERE id = $2 RETURNING *",
                body.error,
                job_id,
            )
            table = await find_image_table(updated["image_id"])
            if table:
                # Same group-wide update as /result — see that endpoint's
                # comment. A technical failure applies to the whole
                # attempt (all images in the group), not just the anchor.
                await conn.execute(
                    f"UPDATE {table} SET ocr_status = 'failed' WHERE group_id = $1", updated["image_id"]
                )

            if updated["attempts"] >= settings.max_ocr_attempts:
                # Already terminal (status='failed' above) — this branch is
                # just here so the response can tell the caller clearly
                # that it's given up, rather than the caller inferring it.
                pass
    return _job_out(updated)
