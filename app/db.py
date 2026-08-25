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


def meter_type_for_table(table: str) -> str:
    return {"images_electric": "electric", "images_water": "water", "images_gas": "gas"}[table]
