"""
Server-side check: does a captured image's timestamp fall on this meter's
configured capture schedule (device_config), or not?

Confirmed design: this is a SERVER-SIDE comparison against device_config —
never based on anything the ESP32 puts in the filename itself. The server
doesn't trust the device to self-report "am I on schedule", it checks
independently every time, using the same schedule GET /devices/config
would have told that meter to follow (falling back to DEFAULT_CONFIG for
an unconfigured meter, exactly like that endpoint does).

Used once per NEW group (the anchor image only) in app/routers/images.py's
upload handler — every other image joining that group inherits the
anchor's is_test rather than being checked independently, so a single
burst is always entirely on-schedule or entirely test, never a mix.
"""
import datetime as dt

import asyncpg

from app.config import get_settings
from app.routers.device_config import DEFAULT_CONFIG


def _minutes_since_midnight(hour: int, minute: int) -> int:
    return hour * 60 + minute


def _slot_matches(device_timestamp: dt.datetime, schedule_mode: int, slot: list[int], tolerance_minutes: int) -> bool:
    day, month, year, hour, minute = slot
    if schedule_mode == 1:
        # Fix-date mode: the slot needs a real calendar date, and
        # device_timestamp's own date must match it exactly — a slot
        # with day/month/year still at 0 (never configured for a real
        # date) can never match anything.
        if not (day and month and year):
            return False
        if not (device_timestamp.day == day and device_timestamp.month == month and device_timestamp.year == year):
            return False
    # schedule_mode == 0 (daily): only Hour/Minute matter, any calendar
    # day counts — matches the same semantics GET /devices/config and
    # the dashboard UI already use elsewhere for daily mode.
    actual = _minutes_since_midnight(device_timestamp.hour, device_timestamp.minute)
    scheduled = _minutes_since_midnight(hour, minute)
    diff = abs(actual - scheduled)
    diff = min(diff, 1440 - diff)  # wraparound, e.g. scheduled 23:55 vs actual 00:05 -> 10 min apart, not 1430
    return diff <= tolerance_minutes


async def is_on_schedule(conn: asyncpg.Connection, meter_id: str, device_timestamp: dt.datetime) -> bool:
    """
    True if device_timestamp (Bangkok local — same convention as
    everywhere else this value is used, e.g. app/filename.py) falls
    within settings.schedule_match_tolerance_minutes of EITHER
    configured slot (date1 or date2) in this meter's device_config.
    Checks DEFAULT_CONFIG's slot instead if the meter has no row yet.
    """
    row = await conn.fetchrow(
        "SELECT schedule_mode, date1, date2 FROM device_config WHERE meter_id = $1", meter_id
    )
    if row is not None:
        schedule_mode = row["schedule_mode"]
        date1 = list(row["date1"])
        date2 = list(row["date2"])
    else:
        schedule_mode = DEFAULT_CONFIG["schedule_mode"]
        date1 = DEFAULT_CONFIG["date1"]
        date2 = DEFAULT_CONFIG["date2"]

    tolerance = get_settings().schedule_match_tolerance_minutes

    if _slot_matches(device_timestamp, schedule_mode, date1, tolerance):
        return True
    if any(v != 0 for v in date2) and _slot_matches(device_timestamp, schedule_mode, date2, tolerance):
        return True
    return False
