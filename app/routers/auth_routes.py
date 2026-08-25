from fastapi import APIRouter, HTTPException, status

from app.auth import create_access_token, hash_secret, verify_secret
from app.config import get_settings
from app.db import pool
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut

router = APIRouter(tags=["default"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="Register")
async def register(body: RegisterRequest):
    """
    Self-service registration for human dashboard accounts. New accounts
    are always created with is_admin = false, is_device = false — promote
    to admin via POST /admin/users (as an existing admin), or directly in
    the DB for the very first admin account.
    """
    existing = await pool().fetchval("SELECT 1 FROM users WHERE username = $1", body.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    row = await pool().fetchrow(
        """
        INSERT INTO users (username, password_hash, is_admin, is_device)
        VALUES ($1, $2, false, false)
        RETURNING id, username, is_admin, is_device, created_at
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
