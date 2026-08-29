"""
asyncpg connection pool.

We talk to Postgres with raw SQL (asyncpg) rather than an ORM: the schema
is small, fixed, and already defined in db/init.sql — an ORM would just
add indirection here.
"""
import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def connect() -> None:
    global _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call db.connect() first")
    return _pool


# Meter type ("electric" | "water" | "gas") is chosen from the first
# letter of meter_id at upload time, per db/init.sql's comment:
#   E -> electric, W -> water, G -> gas
METER_TABLES = {
    "e": "images_electric",
    "w": "images_water",
    "g": "images_gas",
}


def table_for_meter_id(meter_id: str) -> str:
    prefix = meter_id.strip()[:1].lower()
    table = METER_TABLES.get(prefix)
    if table is None:
        raise ValueError(
            f"meter_id {meter_id!r} does not start with e/w/g "
            "(electric/water/gas) — cannot route it to an images_* table"
        )
    return table


def table_for_group_id(group_id: str) -> str:
    """
    group_id is always {E,W,G}{n} (e.g. "E1", "W23") — same first-letter
    convention as meter_id, so this just reuses table_for_meter_id
    directly rather than duplicating the routing logic. No DB round-trip
    needed at all (unlike the old numeric group_id, which required a
    lookup across all 3 tables to find which one had that row id).
    """
    return table_for_meter_id(group_id)


def meter_type_for_table(table: str) -> str:
    return {"images_electric": "electric", "images_water": "water", "images_gas": "gas"}[table]


# Per-type prefix + sequence for the human-readable group_id (e.g. "E1",
# "W3", "G12") — see db/init.sql for the sequences themselves.
GROUP_ID_INFO = {
    "images_electric": ("E", "electric_group_seq"),
    "images_water": ("W", "water_group_seq"),
    "images_gas": ("G", "gas_group_seq"),
}
