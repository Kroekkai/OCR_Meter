from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import CurrentUser, get_current_admin, hash_secret
from app.db import pool
from app.schemas import AdminCreateUserRequest, UserOut

router = APIRouter(prefix="/admin/users", tags=["default"])


@router.get("", response_model=list[UserOut], summary="Admin List Users")
async def admin_list_users(_: CurrentUser = Depends(get_current_admin)):
    rows = await pool().fetch(
        "SELECT id, username, is_admin, is_device, created_at FROM users ORDER BY id"
    )
    return [UserOut(**dict(r)) for r in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="Admin Create User")
async def admin_create_user(body: AdminCreateUserRequest, _: CurrentUser = Depends(get_current_admin)):
    existing = await pool().fetchval("SELECT 1 FROM users WHERE username = $1", body.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    row = await pool().fetchrow(
        """
        INSERT INTO users (username, password_hash, is_admin, is_device)
        VALUES ($1, $2, $3, $4)
        RETURNING id, username, is_admin, is_device, created_at
        """,
        body.username,
        hash_secret(body.password),
        body.is_admin,
        body.is_device,
    )
    return UserOut(**dict(row))
