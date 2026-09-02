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
    this, no separate column anywhere) that was finalized into ocr_jobs
    today — "today" meaning the current Bangkok calendar day, resetting
    at Bangkok midnight (confirmed design). Confirmed rule: at most ONE
    normal group per meter per day may ever be QUEUED for OCR — a
    second normal group the same day still gets an ocr_jobs row
    (confirmed design, added after an earlier version that skipped the
    INSERT entirely caused a real bug: the group would silently get
    queued a day late once midnight rolled over and stopped counting
    yesterday's winner), just with status='dropped' rather than
    'queued' — see app/grouping.py::finalize_group()'s status param and
    JobStatus in app/schemas.py. This function itself doesn't care
    which status an existing row has, only that one exists — so once a
    group gets EITHER a 'queued' or a 'dropped' row, it permanently
    stops being reconsidered by anything. images_* rows for a dropped
    group are left exactly as they are, is_anchor=true, ocr_status
    stays 'pending' forever — confirmed NOT deleted. Test groups are
    exempt from this limit entirely — they can produce as many jobs as
    they like on any given day, confirmed. The regex here mirrors
    is_test_filename() exactly (case-insensitive "_test" right before
    .jpg/.jpeg) — keep the two in sync if either changes.
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


async def finalize_group(conn: asyncpg.Connection, anchor_row: asyncpg.Record, status: str = "queued") -> int:
    """
    Creates the one ocr_jobs row for a group, given its anchor row's data
    (id, meter_id, group_id, original_filename, device_timestamp — any
    images_electric/water/gas row has all of these). Shared by both
    the immediate fast path (app/routers/images.py, right after a group
    reaches its target photo count) and this module's time-based sweep —
    so the INSERT itself only needs to be written once.

    original_filename carries whether this group is a test group (the
    "_Test" suffix — see app/filename.py::is_test_filename()) straight
    through into ocr_jobs unchanged; no separate is_test column exists
    to copy.

    status defaults to 'queued' (the normal case — a real job for the
    OCR client to pick up) but callers pass 'dropped' for a same-day
    duplicate normal group instead (confirmed design) — inserting a row
    either way, never skipping the INSERT entirely, is what makes the
    NOT EXISTS(ocr_jobs WHERE group_id=...) check in both the fast path
    and the sweep permanently true for this group from here on, so it's
    never reconsidered again — including after Bangkok midnight rolls
    over has_normal_group_today() to a new day. See JobStatus in
    app/schemas.py for the bug this fixes.

    Caller is responsible for the transaction, the FOR UPDATE lock on
    anchor_row, checking NOT EXISTS(ocr_jobs WHERE group_id = ...) first,
    and (for non-test groups) checking has_normal_group_today() first —
    this function does none of that itself, it just inserts. Returns the
    new ocr_jobs row's id.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO ocr_jobs (group_id, meter_id, original_filename, device_timestamp, status)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        anchor_row["group_id"],
        anchor_row["meter_id"],
        anchor_row["original_filename"],
        anchor_row["device_timestamp"],
        status,
    )
    return row["id"]


async def finalize_expired_groups() -> int:
    """
    Finds every burst group whose window has closed and that doesn't have
    an ocr_jobs row yet, and creates one ocr_jobs row per group via
    finalize_group() — status='queued' normally, or status='dropped'
    (still a real row, confirmed design — see JobStatus in
    app/schemas.py for why: a truly-skipped INSERT let a dropped group
    get silently re-queued a day late once Bangkok midnight rolled over)
    for a non-test duplicate when a meter already has a normal group
    today, per has_normal_group_today(). Whatever images arrived within
    the window join the group; nothing waits for an exact count here —
    groups that DID already reach their target count were already
    finalized immediately by the upload handler and won't show up in
    this query at all (they already have an ocr_jobs row, regardless of
    its status). Returns how many jobs were created with status='queued'
    (used for logging/tests only — does not count dropped ones).
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
                      AND received_at <= now() - ($1 * interval '1 second')
                      AND NOT EXISTS (SELECT 1 FROM ocr_jobs WHERE group_id = {table}.group_id)
                    FOR UPDATE
                    """,
                    settings.image_group_window_seconds,
                )
                for row in rows:
                    is_test = is_test_filename(row["original_filename"])
                    if not is_test and await has_normal_group_today(conn, table, row["meter_id"]):
                        await finalize_group(conn, row, status="dropped")
                        dropped += 1
                        logger.info(
                            "dropped duplicate normal group %s for meter %s — already has one today "
                            "(ocr_jobs row created with status='dropped', not skipped)",
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
