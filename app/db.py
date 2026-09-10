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


# Utility type ("electric" | "water" | "gas") is chosen from the first
# letter of meter_id at upload time, per db/init.sql's comment:
#   E -> electric, W -> water, G -> gas
#
# Confirmed request: images_electric/water/gas were merged into one
# "images" table — extending to a new meter type (e.g. a future "steam")
# now means adding one entry here (plus the matching entry in
# GROUP_ID_INFO below) instead of a new table + sequence + FK + index
# set + updating every place that used to loop over METER_TABLES.values().
#
# Confirmed request (round 2): images itself carries NO utility_type
# column at all — it's derived from meter_id via this dict every time
# it's needed (app/repo.py::image_out(), the group_id prefix lookup
# below), rather than stored redundantly. meter_id's first letter
# determines it permanently and unambiguously, so there's no scenario
# where a stored copy could ever legitimately disagree with this.
UTILITY_TYPES = {
    "e": "electric",
    "w": "water",
    "g": "gas",
}

# Reverse of UTILITY_TYPES — confirmed request (round 2): needed by
# GET /admin/images' meter_type filter, which used to just match the
# stored utility_type column directly (WHERE utility_type = ANY(...)) —
# now that the column is gone, that filter instead matches meter_id's
# own first letter, and needs this to go from the query param's
# "electric"/"water"/"gas" back to the "E"/"W"/"G" prefix to match
# against.
PREFIX_FOR_UTILITY_TYPE = {v: k.upper() for k, v in UTILITY_TYPES.items()}


def utility_type_for_meter_id(meter_id: str) -> str:
    prefix = meter_id.strip()[:1].lower()
    utility_type = UTILITY_TYPES.get(prefix)
    if utility_type is None:
        raise ValueError(
            f"meter_id {meter_id!r} does not start with e/w/g "
            "(electric/water/gas) — cannot route it to a utility_type"
        )
    return utility_type


def utility_type_for_group_id(group_id: str) -> str:
    """
    group_id is always {E,W,G}{n} (e.g. "E1", "W23") — same first-letter
    convention as meter_id, so this just reuses utility_type_for_meter_id
    directly rather than duplicating the routing logic.
    """
    return utility_type_for_meter_id(group_id)


# Per-type prefix + sequence for the human-readable group_id (e.g. "E1",
# "W3", "G12") — see db/init.sql for the sequences themselves. Keyed by
# utility_type now (not table name — there's only one images table now).
GROUP_ID_INFO = {
    "electric": ("E", "electric_group_seq"),
    "water": ("W", "water_group_seq"),
    "gas": ("G", "gas_group_seq"),
}
