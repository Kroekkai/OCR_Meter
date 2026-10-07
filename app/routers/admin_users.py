"""
จัดการบัญชีผู้ดูแล — เฉพาะ super admin

กฎการลบ (ป้องกันไม่ให้ระบบเหลือ super admin เป็นศูนย์):
  - ลบบัญชีของตัวเองไม่ได้ (403) — ป้องกันการลบบัญชีที่ login อยู่
  - ลบ super admin คนสุดท้ายไม่ได้ (409) — นับใน transaction เดียวกับการลบ
    และล็อกแถว super admin ทั้งหมดด้วย FOR UPDATE เพื่อกันกรณี super admin
    2 คนลบกันเองพร้อมกัน (ทั้งสองคำขอจะผ่านกฎข้อแรก ถ้าไม่ล็อก)
  - ลบบัญชีของระบบ (is_device = true เช่น esp32, ocr-service) ไม่ได้ (403)
    — ผูกกับ DEVICE_API_KEY_USERNAME / OCR_CLIENT_KEY_USERNAME ถ้าลบ
    อุปกรณ์จะอัปโหลดภาพไม่ได้และระบบ OCR จะดึงงานไม่ได้ทันที
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse

from app.auth import CurrentUser, get_current_super_admin, hash_secret
from app.db import pool
from app.schemas import AdminCreateUserRequest, ResetPasswordRequest, UserOut

router = APIRouter(prefix="/admin/users", tags=["default"])
ui_router = APIRouter(tags=["default"])

_USERS_UI_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "users_ui.html"
_USER_COLS = "id, username, is_admin, is_device, is_super_admin, created_at"


@ui_router.get("/admin/users-ui", response_class=HTMLResponse, summary="Admin Users Management Page")
async def admin_users_ui():
    """
    หน้าเว็บจัดการผู้ดูแล — เป็น HTML คงที่เหมือน /admin/device-config-ui
    ตัว route ไม่ต้องตรวจสิทธิ์ เพราะไม่มีข้อมูลในหน้า ข้อมูลทั้งหมดมาจาก
    API /admin/users ซึ่งตรวจสิทธิ์ super admin ที่เซิร์ฟเวอร์ทุกคำขอ
    (หน้าเว็บจะพา admin ที่ไม่ใช่ super admin กลับหน้า config เอง)
    """
    return HTMLResponse(content=_USERS_UI_HTML_PATH.read_text(encoding="utf-8"))


@router.get("", response_model=list[UserOut], summary="Admin List Users")
async def admin_list_users(_: CurrentUser = Depends(get_current_super_admin)):
    """รายชื่อบัญชีทั้งหมด ยกเว้นบัญชีของระบบ (is_device = true)"""
    rows = await pool().fetch(f"SELECT {_USER_COLS} FROM users WHERE is_device = false ORDER BY id")
    return [UserOut(**dict(r)) for r in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="Admin Create User")
async def admin_create_user(body: AdminCreateUserRequest, _: CurrentUser = Depends(get_current_super_admin)):
    existing = await pool().fetchval("SELECT 1 FROM users WHERE username = $1", body.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    is_admin = body.is_admin or body.is_super_admin
    row = await pool().fetchrow(
        f"""
        INSERT INTO users (username, password_hash, is_admin, is_device, is_super_admin)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_USER_COLS}
        """,
        body.username,
        hash_secret(body.password),
        is_admin,
        body.is_device,
        body.is_super_admin,
    )
    return UserOut(**dict(row))


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Admin Delete User")
async def admin_delete_user(user_id: int, me: CurrentUser = Depends(get_current_super_admin)):
    if user_id == me.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete your own account")

    async with pool().acquire() as conn:
        async with conn.transaction():
            # ล็อกแถว super admin ทั้งหมดก่อน — คำขอลบที่มาพร้อมกันต้องรอกัน
            supers = await conn.fetch("SELECT id FROM users WHERE is_super_admin = true FOR UPDATE")
            target = await conn.fetchrow(
                "SELECT id, is_device, is_super_admin FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            if target["is_device"]:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Service accounts cannot be deleted")
            if target["is_super_admin"] and len(supers) <= 1:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot delete the last super admin")
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@router.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT, summary="Admin Reset User Password")
async def admin_reset_password(
    user_id: int, body: ResetPasswordRequest, me: CurrentUser = Depends(get_current_super_admin)
):
    """
    super admin ตั้งรหัสผ่านใหม่ให้บัญชีอื่น (เช่น admin ลืมรหัส)
    รหัสของตัวเองต้องเปลี่ยนผ่าน PUT /me/password ซึ่งต้องยืนยันรหัสเดิม
    """
    if user_id == me.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Use PUT /me/password to change your own password"
        )
    target = await pool().fetchrow("SELECT id, is_device FROM users WHERE id = $1", user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target["is_device"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Service accounts cannot be modified here")
    await pool().execute("UPDATE users SET password_hash = $1 WHERE id = $2", hash_secret(body.new_password), user_id)
