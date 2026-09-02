"""
Background sweep that turns "burst" upload groups into ocr_jobs rows once
their time window closes — the FALLBACK path for groups that never reach
settings.image_group_size images. See app/routers/images.py's upload
handler for the FAST path (finalizes immediately once a group hits that
count, no waiting for the window at all).

See db/init.sql for the group_id/is_anchor/received_at design and
app.config.image_group_window_seconds for the window length.

Runs as an asyncio task started in app/main.py's lifespan — NOT triggered
by any HTTP request. This is what makes the wait actually happen even if
nobody polls the API in the meantime.
"""
import asyncio
import datetime as dt
import logging

import asyncpg

from app.config import get_settings
from app.db import METER_TABLES, pool
from app.filename import BANGKOK_TZ, is_test_filename

logger = logging.getLogger("ocr_meter_store.grouping")


async def has_normal_group_today(conn: asyncpg.Connection, table: str, meter_id: str) -> bool:
    """
    True if this meter already has a NON-test group (its anchor's
    original_filename does NOT carry the "_Test" suffix — see
    app/filename.py::is_test_filename(), the sole source of truth for
    this, no separate column anywhere) that was QUEUED into ocr_jobs
    today — "today" meaning the current Bangkok calendar day, resetting
    at Bangkok midnight (confirmed design). Confirmed rule: at most ONE
    normal group per meter per day may ever be QUEUED for OCR — a
    second normal group the same day is dropped instead
    (mark_group_dropped() right below — sets ocr_status='dropped' on its
    images_*, creates no ocr_jobs row at all, confirmed design). Since
    this function only ever JOINs to ocr_jobs, it naturally only ever
    sees real queued groups — dropped ones, having no ocr_jobs row,
    never affect its answer, which is exactly right: only the day's
    actual winner should count toward "already has one today". Test
    groups are exempt from this limit entirely — they can produce as
    many jobs as they like on any given day, confirmed. The regex here
    mirrors is_test_filename() exactly (case-insensitive "_test" right
    before .jpg/.jpeg) — keep the two in sync if either changes.
    """
    today_midnight_bangkok = dt.datetime.now(BANGKOK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    row = await conn.fetchval(
        f"""
        SELECT 1 FROM {table} img
        JOIN ocr_jobs j ON j.group_id = img.group_id
        WHERE img.meter_id = $1 AND img.is_anchor = true
          AND img.original_filename !~* '_test\\.jpe?g$'
          AND img.received_at >= $2
        LIMIT 1
        """,
        meter_id,
        today_midnight_bangkok,
    )
    return row is not None


async def finalize_group(conn: asyncpg.Connection, anchor_row: asyncpg.Record) -> int:
    """
    Creates the one ocr_jobs row for a group that's actually being
    queued for OCR, given its anchor row's data (id, meter_id, group_id,
    original_filename, device_timestamp — any images_electric/water/gas
    row has all of these). Shared by both the immediate fast path
    (app/routers/images.py, right after a group reaches its target
    photo count) and this module's time-based sweep — so the INSERT
    itself only needs to be written once. Always status='queued' — a
    group that's being dropped instead never calls this at all, see
    mark_group_dropped() below.

    original_filename carries whether this group is a test group (the
    "_Test" suffix — see app/filename.py::is_test_filename()) straight
    through into ocr_jobs unchanged; no separate is_test column exists
    to copy.

    Caller is responsible for the transaction, the FOR UPDATE lock on
    anchor_row, checking NOT EXISTS(ocr_jobs WHERE group_id = ...) first,
    and (for non-test groups) checking has_normal_group_today() first —
    this function does none of that itself, it just inserts. Returns the
    new ocr_jobs row's id.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO ocr_jobs (group_id, meter_id, original_filename, device_timestamp, status)
        VALUES ($1, $2, $3, $4, 'queued')
        RETURNING id
        """,
        anchor_row["group_id"],
        anchor_row["meter_id"],
        anchor_row["original_filename"],
        anchor_row["device_timestamp"],
    )
    return row["id"]


async def mark_group_dropped(conn: asyncpg.Connection, table: str, group_id: str) -> None:
    """
    Marks a same-day duplicate normal group as dropped — confirmed
    design, moved here from an earlier version that instead inserted an
    ocr_jobs row with status='dropped'. Sets ocr_status='dropped' on
    EVERY image sharing this group_id (same pattern POST .../result
    already uses to mark a whole group 'done', not just the anchor —
    see app/routers/ocr_jobs.py), and creates NO ocr_jobs row at all —
    confirmed request: dropped status is visible only in images_*, never
    in ocr_jobs (OCR client polls ocr_jobs and shouldn't see dropped
    groups cluttering that list).

    Since there's no ocr_jobs row to rely on this time, EVERY query that
    decides "is this group still open / a sweep candidate" now filters
    ocr_status = 'pending' explicitly too, not just
    NOT EXISTS(ocr_jobs...) — see the open-group-to-join query in
    app/routers/images.py and finalize_expired_groups()'s candidate
    query right below. This is what closes the same bug a
    status='dropped' ocr_jobs row used to close: without it, a dropped
    group's is_anchor row would keep satisfying "no job yet, not
    dropped" forever, and the very next Bangkok midnight would make
    has_normal_group_today() stop counting yesterday's winner — so the
    dropped group would silently get queued a day late otherwise.
    """
    await conn.execute(f"UPDATE {table} SET ocr_status = 'dropped' WHERE group_id = $1", group_id)


async def finalize_expired_groups() -> int:
    """
    Finds every burst group whose window has closed, is still pending
    (ocr_status='pending' — excludes groups already marked 'dropped' by
    an earlier sweep tick or by the fast path), and doesn't have an
    ocr_jobs row yet, then either queues it via finalize_group() or, for
    a non-test duplicate when its meter already has a normal group
    today (per has_normal_group_today()), drops it via
    mark_group_dropped() instead (no ocr_jobs row created either way
    for a dropped group — confirmed design, see that function).
    Whatever images arrived within the window join the group; nothing
    waits for an exact count here — groups that DID already reach their
    target count were already finalized immediately by the upload
    handler and won't show up in this query at all (queued ones already
    have an ocr_jobs row; dropped ones are already ocr_status='dropped').
    Returns how many jobs were created with status='queued' (used for
    logging/tests only — does not count dropped ones).
    """
    settings = get_settings()
    created = 0
    dropped = 0

    for table in METER_TABLES.values():
        async with pool().acquire() as conn:
            async with conn.transaction():
                # FOR UPDATE is what makes this race-safe against a
                # concurrent upload trying to join (or immediately
                # finalize) this exact group at the same instant — see
                # the matching SELECT ... FOR UPDATE in
                # app/routers/images.py. Whichever transaction gets there
                # first wins; the other sees a consistent, already-decided
                # state.
                rows = await conn.fetch(
                    f"""
                    SELECT id, meter_id, group_id, original_filename, device_timestamp
                    FROM {table}
                    WHERE is_anchor = true
                      AND ocr_status = 'pending'
                      AND received_at <= now() - ($1 * interval '1 second')
                      AND NOT EXISTS (SELECT 1 FROM ocr_jobs WHERE group_id = {table}.group_id)
                    FOR UPDATE
                    """,
                    settings.image_group_window_seconds,
                )
                for row in rows:
                    is_test = is_test_filename(row["original_filename"])
                    if not is_test and await has_normal_group_today(conn, table, row["meter_id"]):
                        await mark_group_dropped(conn, table, row["group_id"])
                        dropped += 1
                        logger.info(
                            "dropped duplicate normal group %s for meter %s — already has one today "
                            "(ocr_status='dropped' on images_*, no ocr_jobs row created)",
                            row["group_id"],
                            row["meter_id"],
                        )
                        continue
                    await finalize_group(conn, row)
                    created += 1

    if created or dropped:
        logger.info(
            "sweep: finalized %d group(s) into ocr_jobs, dropped %d duplicate normal group(s)",
            created,
            dropped,
        )
    return created


async def group_sweep_loop() -> None:
    """Runs until cancelled (app shutdown), checking for expired groups
    every group_sweep_interval_seconds. Errors are logged and swallowed —
    one bad tick should never kill the loop, or new uploads would pile up
    with no job ever created for them."""
    settings = get_settings()
    while True:
        try:
            await finalize_expired_groups()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("image group sweep failed — will retry next tick")
        await asyncio.sleep(settings.group_sweep_interval_seconds)
