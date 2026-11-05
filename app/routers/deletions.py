"""
การลบข้อมูลจากหน้าเว็บผู้ดูแลระบบ + บันทึกการลบ (deletion_log)

- ลบค่าที่อ่านได้ทีละรายการ (ผลจริง / ผลทดสอบ)
- ล้างค่าที่อ่านได้ทั้งหมดของมิเตอร์ (ผลจริงและผลทดสอบ)
- ลบมิเตอร์ทั้งตัว: การตั้งค่า, API Key, token ของอุปกรณ์, ค่าที่อ่านได้,
  งาน OCR, รายการภาพ และบันทึกการอัปโหลด

ข้อตกลง (ยืนยันกับผู้ดูแลระบบแล้ว):
- ทำได้ทั้ง admin และ super admin (ผ่าน JWT เท่านั้น — กุญแจอุปกรณ์/OCR ใช้ไม่ได้)
- ไม่ลบข้อมูลที่ส่งไป CFO Platform แล้ว (ค่าที่ส่งไปแล้วยังอยู่ที่ CFO)
- ไม่ลบไฟล์ภาพในดิสก์ (ลบเฉพาะแถวในฐานข้อมูล)
- ค่าย้อนหลังที่ส่งให้ Rule Base ดึงจาก ocr_meter ทุกครั้ง ค่าที่ลบจึงไม่ถูกนำไปเทียบอีก
- ทุกการลบบันทึกใน deletion_log ว่าใครลบอะไรเมื่อไร ดูได้เฉพาะ super admin
"""
import decimal
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import CurrentUser, get_current_admin, get_current_super_admin
from app.db import pool, utility_type_for_meter_id

logger = logging.getLogger("ocr_meter_store.deletions")
router = APIRouter(tags=["default"])


def _norm_meter(meter_id: str) -> str:
    m = meter_id.strip().upper()
    try:
        utility_type_for_meter_id(m)
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid meter_id")
    return m


async def _log(conn, user: CurrentUser, action: str, meter_id: str, target_id: int | None, detail: dict) -> None:
    await conn.execute(
        """
        INSERT INTO deletion_log (actor_id, actor_username, action, meter_id, target_id, detail)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        user.id, user.username, action, meter_id, target_id,
        json.dumps(detail, ensure_ascii=False, default=lambda v: float(v) if isinstance(v, decimal.Decimal) else str(v)),
    )
    logger.warning("deletion by %s: %s %s target=%s %s", user.username, action, meter_id, target_id, detail)


@router.delete(
    "/admin/meters/{meter_id}/readings/{entry_id}",
    summary="Admin Delete One Reading",
)
async def delete_one_reading(
    meter_id: str,
    entry_id: int,
    test: bool = Query(False, description="true = ลบจากผลทดสอบ (ocr_meter_test)"),
    user: CurrentUser = Depends(get_current_admin),
):
    meter_id = _norm_meter(meter_id)
    table = "ocr_meter_test" if test else "ocr_meter"
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"DELETE FROM {table} WHERE id = $1 AND meter_id = $2 RETURNING *",
                entry_id, meter_id,
            )
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reading not found")
            detail = {
                "table": table,
                "capture_date": row["capture_date"],
                "capture_time": row["capture_time"],
                "ocr_reading": row["ocr_reading"],
                "error_type": row["error_type"],
            }
            for k in ("ocr_reading_raw", "push_status", "external_ref"):
                if k in row.keys():
                    detail[k] = row[k]
            await _log(conn, user, "delete_reading_test" if test else "delete_reading", meter_id, entry_id, detail)
    return {"deleted": 1, "meter_id": meter_id, "id": entry_id, "table": table}


@router.delete(
    "/admin/meters/{meter_id}/readings",
    summary="Admin Clear All Readings Of Meter",
)
async def clear_readings(meter_id: str, user: CurrentUser = Depends(get_current_admin)):
    meter_id = _norm_meter(meter_id)
    async with pool().acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM device_config WHERE meter_id = $1", meter_id)
            if not exists:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meter not found")
            real = await conn.fetchval("WITH d AS (DELETE FROM ocr_meter WHERE meter_id = $1 RETURNING 1) SELECT count(*) FROM d", meter_id)
            test = await conn.fetchval("WITH d AS (DELETE FROM ocr_meter_test WHERE meter_id = $1 RETURNING 1) SELECT count(*) FROM d", meter_id)
            await _log(conn, user, "clear_readings", meter_id, None, {"ocr_meter": real, "ocr_meter_test": test})
    return {"meter_id": meter_id, "deleted_real": real, "deleted_test": test}


@router.delete(
    "/admin/meters/{meter_id}",
    summary="Admin Delete Meter",
)
async def delete_meter(meter_id: str, user: CurrentUser = Depends(get_current_admin)):
    meter_id = _norm_meter(meter_id)
    async with pool().acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM device_config WHERE meter_id = $1 FOR UPDATE", meter_id)
            if not exists:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meter not found")
            counts = {}
            # ลำดับตาม foreign key: images อ้าง ocr_jobs, ทุกตารางอ้าง device_config
            for table in ("ocr_meter", "ocr_meter_test", "images", "ocr_jobs",
                          "esp32_upload_log", "external_api_keys", "device_tokens"):
                counts[table] = await conn.fetchval(
                    f"WITH d AS (DELETE FROM {table} WHERE meter_id = $1 RETURNING 1) SELECT count(*) FROM d",
                    meter_id,
                )
            await conn.execute("DELETE FROM device_config WHERE meter_id = $1", meter_id)
            await _log(conn, user, "delete_meter", meter_id, None, counts)
    return {"meter_id": meter_id, "deleted": counts}


@router.get(
    "/admin/deletion-log",
    summary="Super Admin Get Deletion Log",
)
async def get_deletion_log(
    limit: int = Query(100, ge=1, le=1000),
    meter_id: str | None = None,
    _: CurrentUser = Depends(get_current_super_admin),
):
    params: list = []
    where = ""
    if meter_id:
        params.append(meter_id.strip().upper())
        where = "WHERE meter_id = $1"
    params.append(limit)
    rows = await pool().fetch(
        f"""
        SELECT id, deleted_at, actor_username, action, meter_id, target_id, detail
        FROM deletion_log {where}
        ORDER BY deleted_at DESC, id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [
        {**dict(r), "detail": json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"]}
        for r in rows
    ]
