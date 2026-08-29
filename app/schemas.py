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
# Written by POST /admin/images/ocr/{job_id}/result. error_type is always
# present (0/1/2/3 — see db/init.sql's error_type lookup table for what
# each code means). group_id is copied from the job (E1/W3/G12 — see
# db/init.sql). reading_date/reading_time are the ESP32's capture time
# (job.device_timestamp), not when OCR ran. image_error is only ever set
# when error_type != 0 — the SAME filename the group's anchor image was
# already stored under at upload time (no separate file, no re-upload —
# the OCR client no longer attaches anything here at all), for a human
# to review; never set on a clean successful read (error_type=0).
# (Column used to be called ocr_image_filename.)
# --------------------------------------------------------------------------
class OcrMeterEntry(BaseModel):
    id: int
    meter_id: str
    group_id: str
    reading_date: dt.date
    reading_time: dt.time
    ocr_reading: float | None
    error_type: OcrErrorType
    image_error: str | None

