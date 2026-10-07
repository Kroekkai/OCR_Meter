from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import CurrentUser, create_access_token, get_current_admin, get_current_super_admin, hash_secret, verify_secret
from app.config import get_settings
from app.db import pool
from app.schemas import ChangePasswordRequest, LoginRequest, MeOut, RegisterRequest, TokenResponse, UserOut

router = APIRouter(tags=["default"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="Register")
async def register(body: RegisterRequest, _: CurrentUser = Depends(get_current_super_admin)):
    """
    Confirmed request (security fix): admin-only now — requires a valid
    admin JWT (same as every other /admin/* write). Previously this had
    NO auth at all ("self-service registration"), reachable by anyone
    who found the URL — confirmed exploited in practice: a batch of
    bot-registered accounts with random-looking usernames
    (artexops1, artex7667, ocruser01, etc., spread across several days)
    turned up in the users table, discovered by the admin directly in
    the DB, not created by them. Kept the endpoint itself (rather than
    removing it in favor of only the CLI script, scripts/create_user.py)
    since an admin may still want to create accounts from the dashboard
    without shell access to the server — just no longer reachable by
    an anonymous caller. New accounts are still always created with
    is_admin = false, is_device = false regardless of who's creating
    them — promote via POST /admin/users afterward if the new account
    needs elevated access.
    """
    existing = await pool().fetchval("SELECT 1 FROM users WHERE username = $1", body.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    row = await pool().fetchrow(
        """
        INSERT INTO users (username, password_hash, is_admin, is_device)
        VALUES ($1, $2, false, false)
        RETURNING id, username, is_admin, is_device, is_super_admin, created_at
        """,
        body.username,
        hash_secret(body.password),
    )
    return UserOut(**dict(row))


@router.post("/login", response_model=TokenResponse, summary="Login")
async def login(body: LoginRequest):
    row = await pool().fetchrow(
        "SELECT id, username, password_hash, is_admin FROM users WHERE username = $1",
        body.username,
    )
    if row is None or not verify_secret(body.password, row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    settings = get_settings()
    token = create_access_token(row["id"], row["username"], row["is_admin"])
    return TokenResponse(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@router.get("/me", response_model=MeOut, summary="Current User")
async def me(user: CurrentUser = Depends(get_current_admin)):
    """ข้อมูลบัญชีที่ login อยู่ — หน้าเว็บใช้ตัดสินว่าจะแสดงปุ่ม "จัดการผู้ดูแล" หรือไม่"""
    return MeOut(id=user.id, username=user.username, is_admin=user.is_admin, is_super_admin=user.is_super_admin)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT, summary="Change Own Password")
async def change_own_password(body: ChangePasswordRequest, user: CurrentUser = Depends(get_current_admin)):
    """admin และ super admin เปลี่ยนรหัสผ่านของตัวเอง ต้องยืนยันรหัสผ่านเดิม"""
    row = await pool().fetchrow("SELECT password_hash FROM users WHERE id = $1", user.id)
    if row is None or not verify_secret(body.current_password, row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different")
    await pool().execute("UPDATE users SET password_hash = $1 WHERE id = $2", hash_secret(body.new_password), user.id)
