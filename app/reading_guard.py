"""
กันค่าทศนิยมลดลงจากแสงหรือล้อทศนิยมที่กำลังหมุน (เฉพาะผลลัพธ์จริง ocr_meter)

ตัวอย่าง: ค่าก่อนหน้า 100.7 ภาพครั้งนี้หน้าปัดไม่เปลี่ยน แต่แสงต่างกัน
ระบบ OCR อ่านได้ 100.4 — จำนวนเต็มเท่ากัน (100) แต่ทศนิยมน้อยกว่า
มิเตอร์สะสมค่าลดลงไม่ได้ จึงถือว่าไม่มีการใช้งาน: ใช้ค่าก่อนหน้า (100.7)
และบันทึกเป็นอ่านสำเร็จ (error_type 0)

- ค่าดิบ (ocr_reading_raw) ไม่เปลี่ยน ตรวจย้อนหลังได้เสมอว่า OCR อ่านได้เท่าไร
- ไม่แสดงบนหน้าเว็บ บันทึกใน log ของเซิร์ฟเวอร์ (logger ocr_meter_store.reading_guard)
- เปิด/ปิดด้วย READING_DECIMAL_DROP_GUARD_ENABLED
- ค่าก่อนหน้า = ค่าล่าสุดของมิเตอร์เดียวกันที่ถ่ายก่อนครั้งนี้ และมีตัวเลข (error_type 0 หรือ 3)
- จำนวนเต็มต่างกัน (เช่น 100.9 -> 99.5 หรือ 100.7 -> 101.2) ไม่แตะ ให้ OCR/เจ้าหน้าที่ตัดสินตามเดิม
"""
import datetime as dt
import decimal
import logging

from app.config import get_settings

logger = logging.getLogger("ocr_meter_store.reading_guard")


def _dec(v) -> decimal.Decimal:
    # ค่าใน DB เป็น float — ปัดที่ 6 ตำแหน่งกันเศษทศนิยมของ float
    return decimal.Decimal(str(round(float(v), 6)))


def is_decimal_drop(previous, current) -> bool:
    """จำนวนเต็มเท่ากัน แต่ค่าปัจจุบันน้อยกว่าค่าก่อนหน้า"""
    if previous is None or current is None:
        return False
    p, c = _dec(previous), _dec(current)
    return c < p and int(p) == int(c)


async def apply_decimal_drop_guard(
    conn,
    meter_id: str,
    capture_date: dt.date,
    capture_time: dt.time,
    reading: float | None,
    error_type: int,
) -> tuple[float | None, int]:
    """คืน (ค่าที่จะบันทึก, error_type) — ถ้าไม่เข้าเงื่อนไขคืนค่าเดิม"""
    if not get_settings().reading_decimal_drop_guard_enabled:
        return reading, error_type
    if reading is None or error_type not in (0, 3):
        return reading, error_type
    prev = await conn.fetchval(
        """
        SELECT ocr_reading FROM ocr_meter
        WHERE meter_id = $1
          AND ocr_reading IS NOT NULL
          AND error_type IN (0, 3)
          AND (capture_date, capture_time) < ($2, $3)
        ORDER BY capture_date DESC, capture_time DESC, id DESC
        LIMIT 1
        """,
        meter_id, capture_date, capture_time,
    )
    if not is_decimal_drop(prev, reading):
        return reading, error_type
    adjusted = float(_dec(prev))
    logger.warning(
        "decimal drop guard: %s %s %s read %s < previous %s (same integer) -> store %s as success (was error_type %s)",
        meter_id, capture_date, capture_time, reading, prev, adjusted, error_type,
    )
    return adjusted, 0
