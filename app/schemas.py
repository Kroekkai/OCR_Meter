import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

MeterType = Literal["electric", "water", "gas"]
OcrStatus = Literal["pending", "done", "failed", "dropped"]
"""
"dropped" (added later, confirmed request — moved here FROM ocr_jobs,
see JobStatus below): a normal group finalized on a day its meter
already has one. Set on every image sharing the group's group_id (same
pattern as how "done" gets set on every image in a group after
POST .../result — see app/grouping.py::mark_group_dropped()), never
just the anchor. No ocr_jobs row is ever created for a dropped group —
confirmed request: dropped status shows up only here, in images_*,
never in ocr_jobs at all (OCR client polls ocr_jobs and shouldn't see
dropped groups cluttering that list).
"""
JobStatus = Literal["queued", "processing", "done", "failed"]
"""
Confirmed: "dropped" does NOT belong here (a brief earlier version put
it here — reverted). A dropped group never gets an ocr_jobs row at
all — see OcrStatus above instead, where "dropped" is set on the
group's images_* rows directly. ocr_jobs only ever contains real work
items an OCR client might see via GET .../ocr?job_status=... — keeping
dropped groups out of it entirely, not just filtered by status, is the
point.
"""
# 0 = อ่านสำเร็จ, 1 = อ่านเลขมิเตอร์ไม่ได้, 2 = หาตัวเลข/มิเตอร์ไม่เจอเลย,
# 3 = อ่านได้ค่าแต่ผิดปกติ (รวม reading_decreased/usage_anomaly เดิม) —
# ความหมายเต็มอยู่ที่ตาราง error_type ใน DB (single source of truth)
OcrErrorType = Literal[0, 1, 2, 3]

# ระบบคะแนนสะสม (Summation Score System) — ยืนยันรอบ 4 จากทีม Worker
# (ยืนยันครั้งที่ 2), แทนที่ lookup เดิม (1=LOCAL, 2=GEMINI) ทั้งหมด
# คำนวณจาก YOLO หากล่องตัวเลขไม่ครบ +1000, Local OCR/CNN อ่านไม่ออก +100,
# Gemini (fallback) กู้สำเร็จ +20 / กู้ไม่ผ่าน +10 — **ยืนยันชัดเจนแล้วว่า
# มีแค่ 5 ค่านี้เท่านั้นจริงๆ ไม่มีค่าอื่นอีกเลย**: 0, 120, 1120
# (auto-approve), 110, 1110 (ส่งคนตรวจ) — round 3 เคยเข้าใจผิดว่าค่าอื่น
# ตามสูตร (10, 20, 100, 1000, 1010, 1020, 1100) อาจถูกส่งมาด้วย เลยเปลี่ยน
# เป็น int ทั่วไปไปก่อน ตอนนี้กลับมาเป็น Literal ของ 5 ค่าจริงได้แล้ว —
# ปลอดภัยบน RESPONSE model แบบเดียวกับ OcrErrorType ด้านบน (มาจาก int จริง
# ที่ DB คืนมา ไม่ใช่ raw multipart string ที่มีปัญหาเรื่อง type coercion)
# มี FK ไป ocr_engine_meaning(code) ใน DB แล้วด้วย (db/init.sql) — Literal
# ตรงนี้เป็นการบังคับซ้ำอีกชั้นในระดับ API/response, ไม่ใช่ตัวเดียวที่คุม
OcrEngineType = Literal[0, 110, 120, 1110, 1120]

VALID_OCR_ENGINE_CODES = (0, 110, 120, 1110, 1120)
"""
Confirmed request (round 4) — the same 5 values as OcrEngineType above,
kept as a plain tuple too for app/routers/ocr_jobs.py to validate a raw
Form(...) int against (mirrors error_type's own VALID_CODES pattern
there — a Literal type annotation directly on a Form() field doesn't
reliably coerce the string "0"/"110"/etc. that multipart/form-data
always sends, and rejects it with a confusing 422 instead of the
clearer hand-written error message this enables).
"""


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    is_device: bool
    created_at: dt.datetime


class AdminCreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    is_admin: bool = False
    is_device: bool = False


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
class ImageOut(BaseModel):
    id: int
    meter_type: MeterType
    meter_id: str
    original_filename: str | None
    device_timestamp: dt.datetime | None
    ocr_status: OcrStatus
    group_id: str
    received_at: dt.datetime


class ImageUploadResponse(BaseModel):
    """
    ocr_job_id is non-null only when this upload was the one that
    completed the group (settings.image_group_size images reached) —
    the fast path in app/routers/images.py finalizes into ocr_jobs
    immediately in that case, no waiting for the background sweep. If
    the group isn't complete yet, ocr_job_id stays null and the job gets
    created later by the time-based fallback (app/grouping.py) once the
    window closes — whichever comes first.
    """
    image: ImageOut
    group_id: str
    ocr_job_id: int | None = None


class MeterHistoryEntry(BaseModel):
    image_id: int
    device_timestamp: dt.datetime | None
    ocr_status: OcrStatus
    group_id: str
    latest_ocr_reading: float | None
    latest_job_status: JobStatus | None


# --------------------------------------------------------------------------
# OCR jobs
# --------------------------------------------------------------------------
class OcrJobOut(BaseModel):
    id: int
    group_id: str
    meter_id: str
    # Confirmed: whether this job is a "test" capture is NOT a separate
    # field anywhere in this response (there briefly was an is_test bool
    # here — removed). Check original_filename for a "_Test" suffix
    # right before the extension instead (case-insensitive) — the exact
    # same signal the server itself uses to decide ocr_meter vs
    # ocr_meter_test at /result time (app/filename.py::is_test_filename()).
    # The OCR client is expected to apply the identical check if it
    # needs to tell test jobs apart itself.
    original_filename: str | None
    device_timestamp: dt.datetime | None
    ocr_reading: float | None
    status: JobStatus
    attempts: int


class OcrClaimResponse(BaseModel):
    """
    image_file_urls is plural and can contain more than one entry — a job
    now represents a whole burst group (see images_*.group_id in
    db/init.sql), not a single image. Download all of them, run OCR on
    each, and submit only the single best result via /result — see that
    endpoint's docstring.
    """
    job: OcrJobOut
    image_file_urls: list[str]


class OcrFailRequest(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


class OcrManualEditRequest(BaseModel):
    ocr_reading: float = Field(ge=0, description="A meter's cumulative reading can't be negative — spec's own aiValue field is documented as \"number ≥0\".")


# --------------------------------------------------------------------------
# ocr_meter — clean, standalone OCR results table (no FK back to
# images/ocr_jobs on purpose). One row per *finished* OCR attempt.
# Written by POST /admin/images/ocr/{job_id}/result. Deliberately just
# these 6 fields (confirmed) — no group_id here (that's an
# images/ocr_jobs-internal concern only, never copied into this
# output table, even though an earlier revision briefly did). error_type
# is always present (0/1/2/3 — see db/init.sql's error_type lookup table
# for what each code means). capture_date/capture_time are the ESP32's
# capture time (job.device_timestamp), not when OCR ran — column used to
# be called reading_date/reading_time. image (confirmed request, renamed
# from image_error — see that field's own docstring below for the full
# story) is now the FULL disk path to the group's anchor image on EVERY
# row, success or error alike (e.g.
# "/data/images/E101_20260829_100000_01.jpg"), computed by
# storage.original_path() — same file already stored at upload time, no
# separate file, no re-upload (the OCR client no longer attaches
# anything here at all). (Column used to be called ocr_image_filename,
# then image_error, and before that stored just the bare filename
# rather than the full path.)
# --------------------------------------------------------------------------
class OcrMeterEntry(BaseModel):
    id: int
    meter_id: str
    capture_date: dt.date
    capture_time: dt.time
    ocr_reading: float | None
    error_type: OcrErrorType
    image: str | None
    """
    Confirmed request — renamed from `image_error`, and now populated on
    EVERY row (success or error alike), not only when `error_type != 0`.
    Always the FULL disk path to the group's anchor image (e.g.
    "/data/images/E101_20260829_100000_01.jpg"), computed by
    storage.original_path() — same file already stored at upload time,
    no separate file, no re-upload. The old name/behavior (only set on
    error, meant for a human to review what went wrong) is gone — this
    is now just "the image for this reading," full stop, useful for a
    successful read too.
    """
    ocr_engine: OcrEngineType
    """
    Confirmed request (round 3, Worker team) — back IN to the plain
    OcrMeterEntry shape again, and now REQUIRED (not optional) — a brief
    round 2 had removed it from here and split it into a separate
    OcrMeterEntryWithEngine class, only used by .../result-test's own
    response, specifically because .../result didn't accept it as input
    at the time. That's reversed now: the Worker's new scoring system
    (see OcrEngineType's own comment above) is required on BOTH
    POST .../result and .../result-test, and is core routing information
    for their dashboard workflow (0/120/1120 auto-approve, 110 -> manual
    entry queue, 1110 -> flag for possible meter damage) — not just
    incidental stats anymore, so it belongs in the "clean" ocr_meter
    surface (this class, used by GET /admin/meters/ocr-meter too) same
    as every other field here. OcrMeterEntryWithEngine is gone — this
    class alone now covers every place ocr_engine needs to show up.
    """
    confidence: float | None
    """
    Confirmed request (CFO Platform integration) — the Worker reports
    this directly (0-100), NOT something derived/calculated from
    ocr_engine on our end — the two are separate, independently-reported
    values (ocr_engine is our own internal routing score; confidence is
    what gets forwarded to CFO Platform's own aiValue/confidence field
    largely as-is). Optional: a completely failed read may have no
    meaningful confidence to report, and Worker deployments from before
    this field existed still work without sending it at all.
    """
    external_ref: str | None
    """
    Confirmed decision ("แบบ A"): generated and persisted at OCR-result
    time (right here, in _submit_ocr_result()), not deferred until the
    first push attempt — format <SN>-<YYYYMMDD>-<HHmm>, see
    app/external_push.py::generate_external_ref(). Exposed here mainly
    for visibility/debugging (so this value is visible immediately via
    the API rather than only queryable straight from the DB) — nothing
    about pushing to CFO Platform reads it back through this schema.
    """


# --------------------------------------------------------------------------
# OcrMeterTestEntry — NOT a DB table shape (confirmed: no schema change
# on ocr_meter_test for this — an earlier version added a stored
# anchor_image_path column there, reverted). anchor_image_path here is
# computed at read time only, by
# app/routers/meters.py::_list_ocr_meter_test_rows() — a LEFT JOIN
# against every images_* table's is_anchor=true rows, matched on
# meter_id + device_timestamp (reconstructed from this row's own
# capture_date/capture_time). Optional because the join can miss (the
# anchor image was deleted, or in some future edge case) — the row still
# comes back rather than silently disappearing from the list, just
# without a picture. Used only by GET /admin/meters/ocr-meter-test;
# POST .../result-test itself returns OcrMeterEntry directly (no
# anchor_image_path/group_id/net_mode/etc — those three are a
# listing-view-only concern, see this class) — extends OcrMeterEntry
# directly now (round 3 removed the OcrMeterEntryWithEngine
# intermediate class entirely, since ocr_engine lives in OcrMeterEntry
# itself again).
# --------------------------------------------------------------------------
class OcrMeterTestEntry(OcrMeterEntry):
    anchor_image_path: str | None
    group_id: str | None
    """
    E1/W3/G12-style — rides along on the same LEFT JOIN as
    anchor_image_path (see app/routers/meters.py), so it's null under
    the exact same condition (the anchor image row couldn't be matched).
    Confirmed request: lets the dashboard's per-meter test-results
    section show which burst each card came from.
    """
    net_mode: str | None
    carrier: str | None
    wakeup_reason: str | None
    """
    Confirmed request: shown directly on each test-result card instead
    of a separate log table/section. A second LEFT JOIN against
    esp32_upload_log (see app/routers/meters.py) — null when there's no
    matching log row (older test results from before this logging
    existed, or firmware that doesn't send these query params).
    """


# --------------------------------------------------------------------------
# esp32_upload_log — confirmed request: expose this table via the
# dashboard (it's existed since Project Carbon, but had no read endpoint
# at all until now — see app/routers/meters.py::admin_list_esp32_upload_log()).
# Field names match the table's own columns exactly (see db/init.sql) —
# net_mode/carrier/wakeup_reason, not the earlier data1/data2/data3.
# --------------------------------------------------------------------------
class Esp32UploadLogEntry(BaseModel):
    id: int
    log_date: dt.date
    log_time: dt.time
    meter_id: str
    net_mode: str | None
    carrier: str | None
    wakeup_reason: str | None


# --------------------------------------------------------------------------
# ocr_engine_meaning — confirmed request: a reference table mirroring
# error_type's role (code + human-readable description), but NOT a FK
# target for ocr_meter/ocr_meter_test.ocr_engine — that column must stay
# able to accept any integer (see OcrEngineType's own comment), not just
# the 5 the Worker team has documented so far. Purely for lookup/display
# — see app/routers/meters.py::admin_list_ocr_engine_meaning().
# --------------------------------------------------------------------------
class OcrEngineMeaningEntry(BaseModel):
    code: int
    meaning: str


# --------------------------------------------------------------------------
# PushExternalResult — confirmed request (ข้อ 17): response shape for
# POST /admin/meters/ocr-meter/{id}/push-external, the manual/one-off
# push endpoint. Mirrors ocr_meter's own push_* columns directly (see
# db/init.sql) rather than reusing OcrMeterEntry, since this response
# is specifically ABOUT the push attempt's outcome, not the reading
# itself — outcome is the one field not stored anywhere, computed
# fresh by attempt_push_for_row() each call.
class PushExternalResult(BaseModel):
    outcome: str  # "success" | "failed_retryable" | "failed_permanent" — this attempt's own result, see app/external_push.py::attempt_push_for_row()
    push_status: str
    push_attempt_count: int
    # Confirmed consolidation — was 3 separate fields (push_last_error/
    # push_response_id/push_cfo_status), none of which any code ever
    # queried or branched on individually — merged into ocr_meter/
    # ocr_meter_test's own push_last_response JSONB column (see its
    # comment in db/init.sql for the exact shape). str, not dict —
    # confirmed: asyncpg returns JSONB columns as a raw JSON string,
    # never auto-parsed, without an explicit type codec registered
    # (not done anywhere in this codebase) — verified directly against
    # a live connection, not assumed. The caller (or the dashboard JS)
    # parses it if it needs the structure; most callers just display
    # it as-is.
    push_last_response: str | None
    push_meter_matched: bool | None
    """
    Confirmed request — result_status/next_action columns removed
    (unused). This single field now carries the score's origin,
    approval status, and next action together as one description, per
    db/init.sql's ocr_engine_meaning table.
    """


# --------------------------------------------------------------------------
# device_config — NOT part of the original confirmed spec. From a
# separate ESP32 "device configuration" API spec doc another team sent
# (GET /devices/config) — see app/routers/device_config.py.
# --------------------------------------------------------------------------
class DeviceConfigOut(BaseModel):
    meter_id: str
    schedule_mode: int  # 0 = program/daily mode, 1 = fix-date mode
    date1: list[int]  # [Day, Month, Year, Hour, Minute] — primary schedule
    date2: list[int]  # same shape — secondary schedule, [0,0,0,0,0] if unused
    photo_count: int
    photo_delay: int
    # True เมื่อ meter_id นี้ยังไม่เคยถูกตั้งค่าเองเลย (ไม่มีแถวใน
    # device_config จริง) — ค่าที่เห็นคือ DEFAULT_CONFIG ล้วนๆ ไม่ใช่ค่าที่
    # เคยบันทึกไว้ — ไม่ได้อยู่ใน spec เดิม เพิ่มเองให้ dashboard แยกแยะได้
    # ว่า "กำลังโชว์ค่า default" กับ "มีการตั้งค่าเองแล้ว" — ESP32 ไม่สนใจ
    # field นี้เลย (แค่ไม่ได้ใช้ ไม่ทำให้ parse พัง)
    is_default: bool = False


class DeviceConfigSetRequest(BaseModel):
    """
    NOT in the spec doc at all — my own addition, since the doc only
    describes ESP32 reading its config, never how one gets set in the
    first place. See app/routers/device_config.py's docstring.
    """
    schedule_mode: int = Field(ge=0, le=1)
    date1: list[int] = Field(min_length=5, max_length=5)
    date2: list[int] = Field(min_length=5, max_length=5)
    photo_count: int = Field(ge=1, le=10)
    photo_delay: int = Field(ge=1, le=60)


# --------------------------------------------------------------------------
# External API key management (ข้อ 18) — NOT in the spec doc at all,
# same reasoning as DeviceConfigSetRequest above: the spec only
# describes what a device DOES with its key (send it in X-API-Key),
# never how an admin gets it INTO this system in the first place.
class ExternalApiKeySetRequest(BaseModel):
    api_key: str = Field(min_length=1, description="Plaintext API key as issued by CFO Platform — encrypted before storage, see app/crypto.py.")


class ExternalApiKeyStatus(BaseModel):
    """
    Confirmed: deliberately NEVER includes the plaintext key, or even
    the ciphertext — same principle the spec itself states for CFO
    Platform's own side ("ระบบเก็บกุญแจแบบแฮช เปิดดูย้อนหลังไม่ได้",
    section 2.1) even though our own storage is technically reversible
    (see app/crypto.py) unlike a hash. Just enough for an admin UI to
    show "configured, added 3 days ago" without ever re-displaying the
    secret itself.
    """
    meter_id: str
    has_key: bool
    is_active: bool | None = None
    created_at: dt.datetime | None = None
    rotated_at: dt.datetime | None = None


# --------------------------------------------------------------------------
# VerifyExternalResult — confirmed request (ข้อ 22): response shape for
# POST /admin/device-config/{meter_id}/verify-external — mirrors
# device_config's own external_* columns (see db/init.sql) since this
# response is specifically about what CFO Platform's GET /device
# returned and what got saved from it.
class VerifyExternalResult(BaseModel):
    meter_id: str
    device_sn: str
    device_type: str
    is_active: bool
    install_location: str | None
    meter_no: str
    meter_type: str
    tenant_name: str | None
    location_name: str | None


# --------------------------------------------------------------------------
# PushIssueEntry — confirmed request (ข้อ 21): the closest thing this
# system has to an "admin notification" for push_meter_matched=false —
# no email/SMS infrastructure exists here, so surfacing this as a
# dashboard-visible list (GET /admin/meters/push-issues,
# app/routers/meters.py) an admin actually checks is the confirmed
# mechanism, not a push notification. Also surfaces failed_permanent
# rows (400/401/403/413 — never retried automatically, so an admin is
# the only way these ever get noticed and fixed) — both cases share
# the same shape: "a human needs to look at this row".
class PushIssueEntry(BaseModel):
    id: int
    is_test: bool
    meter_id: str
    capture_date: dt.date
    capture_time: dt.time
    push_status: str
    # str, not dict — see PushExternalResult's own comment on
    # push_last_response for why (asyncpg returns JSONB as raw string).
    push_last_response: str | None
    push_meter_matched: bool | None
