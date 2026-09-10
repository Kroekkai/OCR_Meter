import datetime as dt
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from app.auth import CurrentUser, get_admin_or_service, get_current_admin, get_uploader
from app.config import get_settings
from app.db import GROUP_ID_INFO, PREFIX_FOR_UTILITY_TYPE, pool, utility_type_for_meter_id
from app.filename import BANGKOK_TZ, FilenameParseError, is_test_filename, parse_upload_filename
from app.grouping import finalize_group, has_normal_group_today, mark_group_dropped
from app.repo import get_image_row, image_out
from app.routers.device_config import get_or_create_device_config
from app.schemas import ImageOut, ImageUploadResponse, MeterType, OcrManualEditRequest, OcrStatus
from app import storage

logger = logging.getLogger("ocr_meter_store.images")

router = APIRouter(tags=["default"])


def _stored_filename(original: str, is_test: bool) -> str:
    """
    Appends "_Test" right before the extension when this capture is a
    test capture — confirmed decided by ESP32's own wakeup_reason query
    param now (Project Carbon firmware update), NOT by comparing
    device_timestamp against device_config's schedule (that comparison,
    app/schedule_match.py, is removed — see the upload handler's
    docstring below for the full reasoning). E.g.
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


# PLMN (MCC+MNC) -> ผู้ให้บริการเครือข่ายไทย — ยืนยันตามตารางที่ให้มา.
# ESP32 ส่ง carrier มาเป็นรหัส PLMN ดิบ (เช่น "52003") ไม่ใช่ชื่อ
# เครือข่ายสำเร็จรูป — ต้องแปลงเป็นชื่ออ่านง่ายก่อนเก็บลง
# esp32_upload_log.carrier เสมอ ผ่าน _normalize_carrier() ด้านล่าง
PLMN_CARRIER_MAP = {
    "52003": "AIS (AWN)",
    "52001": "AIS (AWN)",
    "52000": "TrueMove H / my by NT",
    "52004": "TrueMove H / my by NT",
    "52099": "TrueMove H / my by NT",
    "52005": "dtac (TriNet)",
    "52018": "dtac (TriNet)",
    "52015": "NT Mobile (TOT)",
}


def _normalize_carrier(raw: str | None) -> str | None:
    """
    Confirmed request: map the raw PLMN code ESP32 sends in the
    `carrier` query param to a human-readable carrier name, per
    PLMN_CARRIER_MAP above, before it gets logged to
    esp32_upload_log.carrier. Anything not in that table — "-" (the
    documented value when net_mode is WiFi, meaning no cellular
    network at all), None (older firmware that doesn't send this
    param), or a PLMN code not yet in the mapping — passes through
    completely unchanged rather than erroring or being blanked out, so
    a not-yet-mapped code is still visible in the log for someone to
    notice and add, rather than silently lost.
    """
    if raw is None:
        return None
    return PLMN_CARRIER_MAP.get(raw, raw)


async def _insert_dataset_row(conn, stored_filename: str) -> int:
    """
    Confirmed request (hand-drawn diagram) — a new `dataset` table
    consolidating the file path of every image, so "where is this image
    on disk" can be answered from one table. Called once per image
    insert, in both branches of the upload handler below — every images
    row gets its own dataset row (1:1, not shared across a group).

    The `0` passed as image_id here is a deliberate throwaway —
    storage.original_path()/_stem_and_suffix() only ever fall back to
    using image_id when original_filename is None (a path-construction
    edge case for rows inserted outside this API entirely), which can't
    happen here since stored_filename is always a real string by this
    point — so the path is fully determined by the filename alone,
    computable before the images_* row (and its real id) even exists.
    This is what lets dataset_id be included directly in the images_*
    INSERT below instead of needing a second UPDATE after the fact.
    """
    row = await conn.fetchrow(
        "INSERT INTO dataset (path) VALUES ($1) RETURNING id",
        str(storage.original_path(0, stored_filename)),
    )
    return row["id"]


@router.post(
    "/images/upload",
    response_model=ImageUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload Image",
)
async def upload_image(
    file: UploadFile = File(...),
    net_mode: str | None = Query(default=None, description='"4G" or "WiFi" — logged only, no behavior tied to it.'),
    carrier: str | None = Query(
        default=None, description='Carrier name on 4G, or "-" on WiFi — logged only, no behavior tied to it.'
    ),
    wakeup_reason: str | None = Query(
        default=None,
        description=(
            '"timer" (woke on its own RTC schedule) or "manual" (plugged in / a technician pressed the '
            "test button). Confirmed: this is now the SOLE source of truth for is_test — see "
            "_stored_filename() and the docstring below."
        ),
    ),
    device: CurrentUser = Depends(get_uploader),
):
    """
    meter_id and device_timestamp are NOT sent as separate fields — the
    ESP32-CAM encodes both in the filename itself:
    {meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg (e.g. E101_20260818_151230_01.jpg).
    A filename that doesn't match this, or whose meter_id doesn't start
    with e/w/g, is rejected with 400.

    net_mode / carrier / wakeup_reason (Project Carbon firmware update,
    confirmed) arrive as URL query params alongside the multipart file —
    NOT form fields, NOT part of the filename. All three are optional
    (older, not-yet-updated firmware sends none of them at all — see
    below for what happens then).

    **is_test determination — confirmed: wakeup_reason REPLACES the
    old schedule-vs-device_config comparison entirely** (that logic,
    app/schedule_match.py, no longer exists — device_config's own
    schedule_mode/date1/date2 are untouched and still serve their
    original purpose, telling the ESP32 when to wake up in the first
    place; only the SERVER re-verifying that timing server-side is
    gone). `wakeup_reason == "manual"` → test; `"timer"` → real;
    anything else (including a request from firmware old enough not to
    send this param at all) → **test**, confirmed default — deliberately
    the safer side to fail on, since ocr_meter_test is exactly the
    right home for "not confidently real" data, whereas ocr_meter is
    meant to be trustworthy. This is a one-way trust of whatever the
    device claims — the server does not attempt to cross-check
    wakeup_reason against anything.

    net_mode/carrier are logged only (see esp32_upload_log below) —
    neither affects grouping, is_test, or anything else here.

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
        utility_type = utility_type_for_meter_id(meter_id)
    except (FilenameParseError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large")
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    settings = get_settings()
    group_prefix, group_seq = GROUP_ID_INFO[utility_type]

    async with pool().acquire() as conn:
        async with conn.transaction():
            # Confirmed request, round 2: device_config should have a
            # real row for every meter_id ever seen — not tied to any
            # FK/referential-integrity concern this time (device_config
            # still stands completely alone, see
            # get_or_create_device_config()'s own docstring in
            # app/routers/device_config.py), purely so the table itself
            # keeps an accurate record. Same transaction as the INSERT
            # below, so both commit or roll back together.
            await get_or_create_device_config(conn, meter_id)

            # FOR UPDATE here is what makes this race-safe against the
            # background sweep finalizing this exact group at the same
            # instant — see app/grouping.py's matching FOR UPDATE. Locks
            # the ANCHOR row (is_anchor=true) representing the group,
            # not any old numeric self-reference.
            #
            # ocr_status = 'pending' matters just as much as the
            # NOT EXISTS(ocr_jobs...) check now — a dropped group never
            # gets an ocr_jobs row at all (see
            # app/grouping.py::mark_group_dropped()), so NOT EXISTS alone
            # would still be true for it and a new image could wrongly
            # think it's still "open" and try to join it. Excluding
            # anything not 'pending' (i.e. already 'done' or 'dropped')
            # closes that gap.
            #
            # No utility_type filter here (confirmed request, round 2 —
            # that column is gone from images entirely) — meter_id alone
            # already pins this to exactly one utility type, since a
            # given meter_id can only ever belong to one (its own first
            # letter decides it, permanently) — filtering by both was
            # always redundant, never actually narrowed anything further.
            open_anchor = await conn.fetchrow(
                """
                SELECT id, group_id, original_filename FROM images
                WHERE meter_id = $1
                  AND is_anchor = true
                  AND ocr_status = 'pending'
                  AND received_at > now() - ($2 * interval '1 second')
                  AND NOT EXISTS (SELECT 1 FROM ocr_jobs WHERE group_id = images.group_id)
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                meter_id,
                settings.image_group_window_seconds,
            )

            if open_anchor is not None:
                # Joins the still-open group — this image is NOT the
                # anchor, so it just matches whether the anchor's own
                # stored filename got "_Test" appended (rather than
                # recomputing the schedule check itself) — a single
                # burst is always entirely on-schedule or entirely test,
                # never a mix (confirmed). is_test_filename() is the
                # SOLE source of truth for this now — no separate
                # column anywhere — see app/filename.py.
                anchor_is_test = is_test_filename(open_anchor["original_filename"])
                stored_filename = _stored_filename(file.filename, anchor_is_test)
                dataset_id = await _insert_dataset_row(conn, stored_filename)
                image_row = await conn.fetchrow(
                    """
                    INSERT INTO images (meter_id, original_filename, device_timestamp, ocr_status, group_id, is_anchor, dataset_id)
                    VALUES ($1, $2, $3, 'pending', $4, false, $5)
                    RETURNING *
                    """,
                    meter_id,
                    stored_filename,
                    device_timestamp,
                    open_anchor["group_id"],
                    dataset_id,
                )
            else:
                # No open group — this image starts a new one as its own
                # anchor. group_id pulls from the per-type sequence (E1,
                # E2, ... / W1, W2, ... / G1, G2, ...) — human-readable,
                # unlike the raw row id which jumps around since all 3
                # tables share images_id_seq. The is_test check (now
                # wakeup_reason, confirmed — see this function's
                # docstring) happens ONCE here, per new group — the
                # RESULT is baked directly into the stored filename via
                # _stored_filename() ("_Test" appended or not) rather
                # than kept in a separate column; every later image
                # joining this group re-derives the same answer from
                # THIS row's filename (see the open_anchor branch
                # above), never rechecks wakeup_reason itself (which
                # wouldn't even be available to check — later images in
                # the same burst may report a different value, and it's
                # deliberately ignored for them).
                new_seq_n = await conn.fetchval(f"SELECT nextval('{group_seq}')")
                new_group_id = f"{group_prefix}{new_seq_n}"
                # Confirmed: wakeup_reason is now the SOLE determinant —
                # "manual" -> test, "timer" -> real, anything else
                # (including absent, from firmware that hasn't been
                # updated yet) -> test, deliberately the safer default.
                is_test = wakeup_reason != "timer"
                stored_filename = _stored_filename(file.filename, is_test)
                dataset_id = await _insert_dataset_row(conn, stored_filename)
                image_row = await conn.fetchrow(
                    """
                    INSERT INTO images (meter_id, original_filename, device_timestamp, ocr_status, group_id, is_anchor, dataset_id)
                    VALUES ($1, $2, $3, 'pending', $4, true, $5)
                    RETURNING *
                    """,
                    meter_id,
                    stored_filename,
                    device_timestamp,
                    new_group_id,
                    dataset_id,
                )

                # esp32_upload_log — confirmed: one row per GROUP (this
                # branch only runs when a NEW group is opened, i.e. once
                # per burst), not one row per image, since
                # net_mode/carrier/wakeup_reason are identical across
                # every image in a burst (all come from the same
                # wake-up event) — logging per-image would just be 3x
                # redundant rows for no benefit. log_date/log_time use
                # device_timestamp's Bangkok-local date/time, matching
                # how capture_date/capture_time are derived everywhere
                # else in this codebase — not received_at, so a burst
                # uploaded just after Bangkok midnight still logs under
                # the date/time it was actually captured. Columns are
                # net_mode/carrier/wakeup_reason (confirmed naming — an
                # earlier version briefly used data1/data2/data3,
                # renamed) — all TEXT, all nullable (old firmware sends
                # none of them). carrier specifically goes through
                # _normalize_carrier() first — see that function's own
                # docstring for why (ESP32 sends a raw PLMN code, not a
                # carrier name).
                local_dt = device_timestamp.astimezone(BANGKOK_TZ)
                await conn.execute(
                    """
                    INSERT INTO esp32_upload_log (log_date, log_time, meter_id, net_mode, carrier, wakeup_reason)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    local_dt.date(),
                    local_dt.time(),
                    meter_id,
                    net_mode,
                    _normalize_carrier(carrier),
                    wakeup_reason,
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
                "SELECT COUNT(*) FROM images WHERE group_id = $1",
                image_row["group_id"],
            )
            if group_count >= target_count:
                # Re-fetch the anchor row locked — need its
                # original_filename/device_timestamp to copy into
                # ocr_jobs, and FOR UPDATE + the checks right after are
                # what prevent two uploads that both push the count over
                # the line at nearly the same instant from both acting
                # on the same group twice (mirrors the same race-safety
                # the sweep already has).
                anchor_row = await conn.fetchrow(
                    "SELECT * FROM images WHERE group_id = $1 AND is_anchor = true FOR UPDATE",
                    image_row["group_id"],
                )
                already_has_job = await conn.fetchval(
                    "SELECT 1 FROM ocr_jobs WHERE group_id = $1", image_row["group_id"]
                )
                # anchor_row["ocr_status"] != "pending" catches the case
                # where a concurrent transaction already called
                # mark_group_dropped() on this exact group between our
                # SELECT above and now — belt-and-suspenders alongside
                # already_has_job, since a dropped group has no ocr_jobs
                # row to catch it via that check alone.
                if anchor_row is not None and not already_has_job and anchor_row["ocr_status"] == "pending":
                    anchor_is_test = is_test_filename(anchor_row["original_filename"])
                    if not anchor_is_test and await has_normal_group_today(conn, meter_id):
                        # Confirmed rule: at most one normal (non-test)
                        # group per meter per day may be QUEUED for OCR —
                        # this meter already has one today, so this
                        # group is dropped instead: ocr_status='dropped'
                        # on every image sharing this group_id, and NO
                        # ocr_jobs row at all (confirmed design — see
                        # app/grouping.py::mark_group_dropped()). ocr_job_id
                        # stays null in THIS response either way — a
                        # dropped group was never meant to be picked up
                        # by anyone, so there's nothing meaningful to
                        # hand back here. Test groups skip this check
                        # entirely — no daily limit for them.
                        await mark_group_dropped(conn, image_row["group_id"])
                        logger.info(
                            "dropped duplicate normal group %s for meter %s — already has one today "
                            "(ocr_status='dropped' on images_*, no ocr_jobs row created)",
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
        image=image_out(image_row),
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
    if meter_id:
        meter_id = meter_id.strip().upper()  # meter_id is always stored uppercase — see app/filename.py

    clauses, params = [], []
    # Confirmed request (round 2): images carries no utility_type column
    # anymore — this filter now matches meter_id's own first letter
    # directly instead (LEFT(meter_id, 1)), via PREFIX_FOR_UTILITY_TYPE
    # (app/db.py) to go from "electric"/"water"/"gas" back to "E"/"W"/"G".
    if meter_type:
        params.append(PREFIX_FOR_UTILITY_TYPE[meter_type])
        clauses.append(f"LEFT(meter_id, 1) = ${len(params)}")
    if meter_id:
        params.append(meter_id)
        clauses.append(f"meter_id = ${len(params)}")
    if ocr_status:
        params.append(ocr_status)
        clauses.append(f"ocr_status = ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = await pool().fetch(
        f"SELECT * FROM images {where} ORDER BY id DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
        *params,
    )
    return [image_out(r) for r in rows]


@router.get("/admin/images/{item_id}", response_model=ImageOut, summary="Admin Get Image")
async def admin_get_image(item_id: int, _: CurrentUser = Depends(get_admin_or_service)):
    row = await get_image_row(item_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return image_out(row)


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
    row = await get_image_row(item_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    async with pool().acquire() as conn:
        async with conn.transaction():
            if row["is_anchor"]:
                # job_id must be cleared BEFORE the ocr_jobs row is
                # deleted, not after — images.job_id has a real FK to
                # ocr_jobs(id) now (confirmed request), so deleting the
                # ocr_jobs row first would violate that FK for every
                # OTHER image in the same group still pointing at it.
                await conn.execute("UPDATE images SET job_id = NULL WHERE group_id = $1", row["group_id"])
                await conn.execute("DELETE FROM ocr_jobs WHERE group_id = $1", row["group_id"])
            await conn.execute("DELETE FROM images WHERE id = $1", item_id)

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
    row = await get_image_row(item_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    group_id = row["group_id"]
    anchor = await pool().fetchrow("SELECT * FROM images WHERE group_id = $1 AND is_anchor = true", group_id)
    if anchor is None:
        # Anchor was deleted separately (see admin_delete_image's note) —
        # fall back to this image's own data so reprocess still works.
        anchor = row

    async with pool().acquire() as conn:
        async with conn.transaction():
            await finalize_group(conn, anchor)
            await conn.execute("UPDATE images SET ocr_status = 'pending' WHERE group_id = $1", group_id)
            updated = await conn.fetchrow("SELECT * FROM images WHERE id = $1", item_id)
    return image_out(updated)


@router.get("/admin/images/{item_id}/file", summary="Admin Get Image File")
async def admin_get_image_file(item_id: int, _: CurrentUser = Depends(get_admin_or_service)):
    row = await get_image_row(item_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    path = storage.original_path(item_id, row["original_filename"])
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image file missing on disk")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/admin/images-by-filename/{filename}/file", summary="Admin Get Image File By Filename")
async def admin_get_image_file_by_filename(filename: str, _: CurrentUser = Depends(get_admin_or_service)):
    """
    NOT in either original spec — added for the dashboard's test-results
    view (app/static/device_config_ui.html), which only has
    OcrMeterTestEntry.anchor_image_path (a full disk path, e.g.
    "/data/images/E101_..._Test.jpg" — see app/schemas.py) to work with,
    not an images_*.id the way GET /admin/images/{item_id}/file needs.
    The dashboard extracts just the filename from that path client-side
    and calls this instead.

    filename is validated to reject any path separator or ".." before
    ever touching the filesystem — confirmed necessary: this endpoint
    takes a caller-supplied string and joins it onto settings.upload_dir,
    so without this check a crafted filename could walk outside that
    directory (e.g. "../../etc/passwd"). Deliberately stricter than
    storage.original_path() (used by the item_id-based endpoint above),
    which only ever receives filenames already validated at upload time
    by app/filename.py's regex — this one has no such guarantee, since
    it comes straight from the request path.
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")
    path = Path(get_settings().upload_dir) / filename
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image file missing on disk")
    return FileResponse(path, media_type="image/jpeg")


# GET .../ocr-result-file removed — there's no separate "OCR result"
# file anymore (see app/routers/ocr_jobs.py's /result docstring). For
# the image on any row (success or error alike, confirmed request), use
# this same endpoint (/admin/images/{item_id}/file) — ocr_meter.image
# names exactly this file, nothing new was ever written.


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
    image_row = await get_image_row(item_id)
    if image_row is None:
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
            await conn.execute("UPDATE images SET ocr_status = 'done' WHERE group_id = $1", group_id)

    return dict(updated_job)
