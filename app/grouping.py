"""
Background sweep that turns "burst" upload groups into ocr_jobs rows once
their time window closes. See db/init.sql for the group_id/received_at
design and app.config.image_group_window_seconds for the window length.

Runs as an asyncio task started in app/main.py's lifespan — NOT triggered
by any HTTP request. This is what makes the wait actually happen even if
nobody polls the API in the meantime.
"""
import asyncio
import logging

from app.config import get_settings
from app.db import METER_TABLES, pool

logger = logging.getLogger("ocr_meter_store.grouping")


async def finalize_expired_groups() -> int:
    """
    Finds every burst group whose window has closed and that doesn't have
    an ocr_jobs row yet, and creates exactly one ocr_jobs row per group
    (referencing the group's anchor image — the first image uploaded for
    that meter_id in the burst). Whatever images arrived within the
    window join the group; nothing waits for an exact count. Returns how
    many jobs were created (used for logging/tests only).
    """
    settings = get_settings()
    created = 0

    for table in METER_TABLES.values():
        async with pool().acquire() as conn:
            async with conn.transaction():
                # FOR UPDATE is what makes this race-safe against a
                # concurrent upload trying to join this exact group at the
                # same instant — see the matching SELECT ... FOR UPDATE in
                # app/routers/images.py when an upload looks for an open
                # group to join. Whichever transaction gets there first
                # wins; the other sees a consistent, already-decided state.
                rows = await conn.fetch(
                    f"""
                    SELECT id, meter_id, original_filename, device_timestamp
                    FROM {table}
                    WHERE group_id = id
                      AND received_at <= now() - ($1 * interval '1 second')
                      AND NOT EXISTS (SELECT 1 FROM ocr_jobs WHERE image_id = {table}.id)
                    FOR UPDATE
                    """,
                    settings.image_group_window_seconds,
                )
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO ocr_jobs (image_id, meter_id, original_filename, device_timestamp, status)
                        VALUES ($1, $2, $3, $4, 'queued')
                        """,
                        row["id"],
                        row["meter_id"],
                        row["original_filename"],
                        row["device_timestamp"],
                    )
                    created += 1

    if created:
        logger.info("finalized %d image group(s) into ocr_jobs", created)
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
