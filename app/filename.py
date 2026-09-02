"""
The ESP32-CAM names every uploaded file itself, encoding both meter_id and
capture time in the filename — the server derives both from that, it does
not receive them as separate form fields:

    {meterId}_{YYYYMMDD}_{HHMMSS}_{ลำดับการถ่าย}.jpg
    e.g. E101_20260818_151230_01.jpg

The timestamp is always Thailand local time (UTC+7) — the device syncs its
clock via NTP — so it's parsed as tz-aware +07:00, not naive/UTC.

A filename that doesn't match this shape, or whose meter_id doesn't start
with e/w/g, is rejected with HTTP 400 (not 422 — this is what the upload
spec calls for).

meter_id is normalized to UPPERCASE here (once, at the source) regardless
of how the device sent it — e.g. "E101" and "e101" both come out as
"E101". Without this, the same physical meter could end up split across
two different meter_id values in the DB depending on which case a given
upload used, which would silently fragment GET /admin/meters/{meter_id}/history
(it's an exact-match lookup) and any meter_id filter.
"""
import datetime as dt
import re

BANGKOK_TZ = dt.timezone(dt.timedelta(hours=7))

_FILENAME_RE = re.compile(
    r"^(?P<meter_id>[A-Za-z][A-Za-z0-9-]*)_"
    r"(?P<date>\d{8})_"
    r"(?P<time>\d{6})_"
    r"(?P<seq>\d+)"
    r"\.jpe?g$",
    re.IGNORECASE,
)


class FilenameParseError(ValueError):
    pass


def parse_upload_filename(filename: str | None) -> tuple[str, dt.datetime]:
    """Returns (meter_id, device_timestamp). Raises FilenameParseError on any
    mismatch — caller is responsible for turning that into an HTTP 400."""
    if not filename:
        raise FilenameParseError("No filename provided")

    match = _FILENAME_RE.match(filename.strip())
    if not match:
        raise FilenameParseError(
            f"Filename {filename!r} does not match the required "
            "{meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg pattern"
        )

    meter_id = match.group("meter_id").upper()
    date_str = match.group("date")
    time_str = match.group("time")

    try:
        device_timestamp = dt.datetime(
            year=int(date_str[0:4]),
            month=int(date_str[4:6]),
            day=int(date_str[6:8]),
            hour=int(time_str[0:2]),
            minute=int(time_str[2:4]),
            second=int(time_str[4:6]),
            tzinfo=BANGKOK_TZ,
        )
    except ValueError as exc:
        raise FilenameParseError(f"Filename {filename!r} has an invalid date/time: {exc}") from exc

    return meter_id, device_timestamp


_TEST_FILENAME_RE = re.compile(r"_test\.jpe?g$", re.IGNORECASE)


def is_test_filename(filename: str | None) -> bool:
    """
    True if this filename carries the "_Test" suffix that
    app/routers/images.py's _stored_filename() appends when a group's
    device_timestamp fell outside that meter's device_config schedule
    (app/schedule_match.py). Confirmed: this is now the SOLE source of
    truth for "is this a test capture" end to end — there is no separate
    is_test column/field anywhere anymore (there briefly was, on
    images_*/ocr_jobs/OcrJobOut — removed). Both the server (deciding
    ocr_meter vs ocr_meter_test at /result, and the "one normal group
    per meter per day" limit) and the OCR client itself (if it wants to
    tell test jobs apart) are expected to check the filename the same
    way, via this exact function — matches "_Test" right before the
    extension only, case-insensitively, not "test" appearing anywhere
    else in the filename (a meter_id could theoretically contain those
    letters otherwise).
    """
    if not filename:
        return False
    return bool(_TEST_FILENAME_RE.search(filename))
