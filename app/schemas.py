import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

MeterType = Literal["electric", "water", "gas"]
OcrStatus = Literal["pending", "done", "failed"]
JobStatus = Literal["queued", "processing", "done", "failed"]
# 0 = อ่านสำเร็จ, 1 = อ่านเลขมิเตอร์ไม่ได้, 2 = หาตัวเลข/มิเตอร์ไม่เจอเลย,
# 3 = อ่านได้ค่าแต่ผิดปกติ (รวม reading_decreased/usage_anomaly เดิม) —
# ความหมายเต็มอยู่ที่ตาราง error_type ใน DB (single source of truth)
OcrErrorType = Literal[0, 1, 2, 3]


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
    ocr_reading: float


# --------------------------------------------------------------------------
# ocr_meter — clean, standalone OCR results table (no FK back to
# images_*/ocr_jobs on purpose). One row per *finished* OCR attempt.
# Written by POST /admin/images/ocr/{job_id}/result. Deliberately just
# these 6 fields (confirmed) — no group_id here (that's an
# images_*/ocr_jobs-internal concern only, never copied into this
# output table, even though an earlier revision briefly did). error_type
# is always present (0/1/2/3 — see db/init.sql's error_type lookup table
# for what each code means). capture_date/capture_time are the ESP32's
# capture time (job.device_timestamp), not when OCR ran — column used to
# be called reading_date/reading_time. image_error is only ever set when
# error_type != 0 — the FULL disk path to the group's anchor image (e.g.
# "/data/images/E101_20260829_100000_01.jpg"), computed by
# storage.original_path() — same file already stored at upload time, no
# separate file, no re-upload (the OCR client no longer attaches
# anything here at all), for a human to review; never set on a clean
# successful read (error_type=0). (Column used to be called
# ocr_image_filename, and before that stored just the bare filename
# rather than the full path.)
# --------------------------------------------------------------------------
class OcrMeterEntry(BaseModel):
    id: int
    meter_id: str
    capture_date: dt.date
    capture_time: dt.time
    ocr_reading: float | None
    error_type: OcrErrorType
    image_error: str | None


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
