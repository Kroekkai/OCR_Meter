"""
Confirmed request: images_electric/water/gas were merged into one
"images" table (utility_type column distinguishes electric/water/gas
now, instead of which table a row lives in) — these helpers used to need
to search across all three tables for a given image id; now it's a
direct lookup, no searching needed at all.
"""
from __future__ import annotations

import asyncpg

from app.db import pool
from app.schemas import ImageOut


async def get_image_row(image_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM images WHERE id = $1", image_id)


def image_out(row: asyncpg.Record) -> ImageOut:
    return ImageOut(
        id=row["id"],
        meter_type=row["utility_type"],
        meter_id=row["meter_id"],
        original_filename=row["original_filename"],
        device_timestamp=row["device_timestamp"],
        ocr_status=row["ocr_status"],
        group_id=row["group_id"],
        received_at=row["received_at"],
    )


async def get_group_images(group_id: str) -> list[asyncpg.Record]:
    """All images sharing this group_id (the burst group, e.g. "E1"),
    including the anchor image itself (is_anchor = true) — see
    db/init.sql."""
    return await pool().fetch(
        "SELECT * FROM images WHERE group_id = $1 ORDER BY id ASC",
        group_id,
    )
