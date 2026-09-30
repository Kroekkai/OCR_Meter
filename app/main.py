import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.config import get_settings
from app.external_push import external_push_sweep_loop
from app.grouping import group_sweep_loop
from app.prefix_middleware import StripPathPrefixMiddleware
from app.routers import admin_users, auth_routes, device_config, health, images, meters, ocr_jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    # Finalizes burst upload groups into ocr_jobs once their window
    # closes — see app/grouping.py. Runs independently of any HTTP
    # request, so the wait actually happens even if nobody polls.
    sweep_task = asyncio.create_task(group_sweep_loop())
    # ข้อ 18, confirmed request — retries/first-attempts CFO Platform
    # pushes on a schedule, same "runs independently of any HTTP
    # request" reasoning as sweep_task above. See
    # app/external_push.py::external_push_sweep_loop() for what it
    # actually does each tick (a no-op until external_api_base_url is
    # configured).
    external_push_task = asyncio.create_task(external_push_sweep_loop())
    yield
    sweep_task.cancel()
    external_push_task.cancel()
    for task in (sweep_task, external_push_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await db.disconnect()


settings = get_settings()

app = FastAPI(
    title="OCR Meter Store",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(auth_routes.router)
app.include_router(admin_users.router)
# ocr_jobs ต้อง include ก่อน images เสมอ — ocr_jobs.router มี
# "GET /admin/images/ocr" (literal, ไม่มี path param) ส่วน images.router
# มี "GET /admin/images/{item_id}" (path param เดียวกันตำแหน่งเดียวกัน)
# FastAPI/Starlette จับคู่ route ตามลำดับ include ก่อน-หลัง ไม่ใช่ตาม
# ความเฉพาะเจาะจง — ถ้า images มาก่อน "/admin/images/ocr" จะโดน
# {item_id} ดักจับไปตีความ "ocr" เป็นค่า item_id (แล้ว fail เป็น int
# ไม่ได้ -> 422) ก่อนที่ ocr_jobs.router จะมีโอกาสได้ทำงานเลย — บั๊กจริง
# ที่เจอตอน ocr_client_poller.py เรียก GET /admin/images/ocr ครั้งแรก
app.include_router(ocr_jobs.router)
app.include_router(images.router)
app.include_router(meters.router)
# No collision risk with anything above — "/devices/config" and
# "/admin/device-config/{meter_id}" are both brand-new path prefixes
# nothing else in this API uses, so include order doesn't matter here.
app.include_router(device_config.router)

# Strips BASE_PATH_PREFIX (e.g. "/iot") from the incoming request path
# before routing, if present. No-op if the proxy already strips it, or if
# base_path_prefix is "" — see app/prefix_middleware.py.
app = StripPathPrefixMiddleware(app, settings.base_path_prefix)
