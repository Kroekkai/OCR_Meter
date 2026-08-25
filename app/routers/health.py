from fastapi import APIRouter

from app.db import pool

router = APIRouter(tags=["default"])


@router.get("/health", summary="Health")
async def health():
    try:
        await pool().fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}
