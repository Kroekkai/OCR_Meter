"""
คืนงาน OCR ที่ถูกจองแล้วค้าง (status = 'processing') กลับเข้าคิว

กรณี: ระบบ OCR จองงานและดาวน์โหลดภาพไปแล้ว แต่ไม่ส่งผล (/result, /result-test)
หรือแจ้งล้มเหลว (/fail) กลับมา เช่น โปรแกรมของระบบ OCR หยุดทำงานกลางทาง
เดิมงานจะค้างเป็น processing ตลอดไป เพราะจองซ้ำไม่ได้ (409)

- งานเบื้องหลังตรวจทุก OCR_REQUEUE_CHECK_SECONDS วินาที
- งานที่ claimed_at เก่ากว่า OCR_CLAIM_TIMEOUT_MINUTES นาที กลับเป็น queued
  (งานที่ค้างก่อนเพิ่มคอลัมน์ claimed_at — ค่าเป็น NULL — ถูกคืนในรอบแรก)
- คืนเข้าคิวต่อไปเรื่อย ๆ ไม่จำกัดจำนวนครั้ง จำนวนครั้งที่จองยังดูได้จาก attempts
- ถ้าระบบ OCR ตัวเดิมส่งผลมาช้าหลังงานถูกคืน: งานที่กลับเป็น queued รับผลไม่ได้ (409)
  และถ้ามี OCR อีกตัวจองไปแล้ว ผลที่มาถึงก่อนเท่านั้นที่ถูกบันทึก จึงไม่เกิดค่าซ้ำ
- เปิด/ปิดด้วย OCR_REQUEUE_ENABLED
"""
import asyncio
import logging

from app.config import get_settings
from app.db import pool

logger = logging.getLogger("ocr_meter_store.ocr_requeue")


async def requeue_stale_jobs() -> list:
    """คืนงานที่ค้างเกินเวลาเข้าคิว คืนรายการ (id, meter_id, attempts) ที่ถูกคืน"""
    settings = get_settings()
    rows = await pool().fetch(
        """
        UPDATE ocr_jobs
        SET status = 'queued', claimed_at = NULL
        WHERE status = 'processing'
          AND (claimed_at IS NULL OR claimed_at < now() - make_interval(mins => $1))
        RETURNING id, meter_id, attempts
        """,
        settings.ocr_claim_timeout_minutes,
    )
    for r in rows:
        logger.warning(
            "ocr requeue: job %s (%s) claimed but no result within %s min -> queued again (attempts so far %s)",
            r["id"], r["meter_id"], settings.ocr_claim_timeout_minutes, r["attempts"],
        )
    return rows


async def ocr_requeue_loop() -> None:
    """ทำงานจนแอปปิด ข้อผิดพลาดในรอบหนึ่งไม่ทำให้ loop หยุด"""
    settings = get_settings()
    if not settings.ocr_requeue_enabled:
        logger.info("ocr requeue disabled (OCR_REQUEUE_ENABLED=false)")
        return
    while True:
        try:
            await requeue_stale_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ocr requeue sweep failed — will retry next tick")
        await asyncio.sleep(settings.ocr_requeue_check_seconds)
