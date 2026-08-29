"""
images_electric / images_water / images_gas share a single id sequence
(images_id_seq), so a given image id lives in exactly one of the three
tables. These helpers look it up without the caller needing to know which
table that is.
"""
from __future__ import annotations

import asyncpg

from app.db import METER_TABLES, meter_type_for_table, pool
from app.schemas import ImageOut

ALL_IMAGE_TABLES = list(METER_TABLES.values())


async def find_image_table(image_id: int) -> str | None:
    """Return the table name ('images_electric' | 'images_water' | 'images_gas')
    that contains this image id, or None if it doesn't exist anywhere."""
    for table in ALL_IMAGE_TABLES:
        exists = await pool().fetchval(f"SELECT 1 FROM {table} WHERE id = $1", image_id)
        if exists:
            return table
    return None


async def get_image_row(image_id: int) -> tuple[str, asyncpg.Record] | tuple[None, None]:
    table = await find_image_table(image_id)
    if table is None:
        return None, None
    row = await pool().fetchrow(f"SELECT * FROM {table} WHERE id = $1", image_id)
    return table, row


def image_out(table: str, row: asyncpg.Record) -> ImageOut:
    return ImageOut(
        id=row["id"],
        meter_type=meter_type_for_table(table),
        meter_id=row["meter_id"],
        original_filename=row["original_filename"],
        device_timestamp=row["device_timestamp"],
        ocr_status=row["ocr_status"],
        group_id=row["group_id"],
        received_at=row["received_at"],
    )


async def get_group_images(table: str, group_id: str) -> list[asyncpg.Record]:
    """All images sharing this group_id (the burst group, e.g. "E1"),
    including the anchor image itself (is_anchor = true) — see
    db/init.sql."""
    return await pool().fetch(
        f"SELECT * FROM {table} WHERE group_id = $1 ORDER BY id ASC",
        group_id,
    )
