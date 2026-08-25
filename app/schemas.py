import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

MeterType = Literal["electric", "water", "gas"]
OcrStatus = Literal["pending", "done", "failed"]
JobStatus = Literal["queued", "processing", "done", "failed"]
OcrErrorType = Literal["no_digits_found", "image_unreadable", "reading_decreased", "usage_anomaly"]


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
    group_id: int
    received_at: dt.datetime


class ImageUploadResponse(BaseModel):
    """
    ocr_job_id is always null here now — with time-based grouping, a job
    is never created synchronously at upload time. It's created later by
    the background sweep once the group's window closes (see
    app/grouping.py). Check GET /admin/images/{id} afterward, or poll
    GET /admin/images/ocr?meter_id=... to see the job once it exists.
    """
    image: ImageOut
    group_id: int
    ocr_job_id: int | None = None


class MeterHistoryEntry(BaseModel):
    image_id: int
    device_timestamp: dt.datetime | None
    ocr_status: OcrStatus
    group_id: int
    latest_ocr_reading: float | None
    latest_job_status: JobStatus | None


# --------------------------------------------------------------------------
# OCR jobs
# --------------------------------------------------------------------------
class OcrJobOut(BaseModel):
    id: int
    image_id: int
    meter_id: str
    original_filename: str | None
    device_timestamp: dt.datetime | None
    ocr_reading: float | None
    status: JobStatus
    attempts: int
    last_error: str | None
    admin_reason: str | None


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
    admin_reason: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------
# ocr_meter — clean, standalone OCR results table (no FK back to
# images_*/ocr_jobs on purpose). One row per *finished* OCR attempt —
# either a real reading, or one of the 3 known error_type cases. Written
# by POST /admin/images/ocr/{job_id}/result. See db/init.sql for the
# full column-by-column rationale.
# --------------------------------------------------------------------------
class OcrMeterEntry(BaseModel):
    id: int
    meter_id: str
    reading_date: dt.date
    reading_time: dt.time
    ocr_reading: float | None
    error_type: OcrErrorType | None
    error_detail: str | None
    ocr_image_filename: str | None

