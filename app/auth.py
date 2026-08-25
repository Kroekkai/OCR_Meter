"""
Two auth mechanisms, matching the real .env / app/main.py:

1. JWT bearer auth (Authorization: Bearer <token>), issued by /login.
   Declared as an OpenAPI security scheme, so it shows the padlock icon
   in Swagger for the routes that require it (/admin/users,
   DELETE /admin/images/{id}, PUT .../ocr-manual). JWT_SECRET must match
   meter-dashboard's exactly — both services decode the same tokens.

2. A single fixed API key per deployment, set via env var, as an
   *optional alternative* to logging in for machine callers:
     - X-Device-Key on POST /images/upload, checked against
       DEVICE_API_KEY; on match, the request is attributed to the
       existing user named by DEVICE_API_KEY_USERNAME (e.g. "esp32").
     - X-OCR-Key on the /admin/images/ocr/* routes, checked against
       OCR_CLIENT_KEY; on match, attributed to OCR_CLIENT_KEY_USERNAME,
       which must be an existing ADMIN account (e.g. "ocr-service").
   Leaving a pair blank in settings disables that shortcut entirely and
   falls back to requiring a real JWT login for that route. This is a
   plain FastAPI Header() dependency (not an OpenAPI security scheme),
   which is why these routes don't show a padlock in Swagger even
   though they are authenticated.

Several /admin/images/* read routes accept EITHER a valid admin JWT OR
one of the two static keys above (e.g. meter-dashboard's own service
account needs read access without a human login). See
get_admin_or_service() below — this combined dependency is my own
addition for those routes, not something confirmed from your real code.
"""
from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.db import pool

# Declared as an OpenAPI security scheme on purpose: routes that depend on
# get_current_admin() (via this) get the padlock icon in Swagger, matching
# /admin/users, DELETE /admin/images/{id}, and PUT .../ocr-manual in the
# spec. Routes using the plain-header dependencies further down
# (get_uploader / get_ocr_client / get_admin_or_service) do NOT use this,
# so they render without a padlock — they're still authenticated, just
# not via a scheme Swagger auto-detects.
_bearer_scheme = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------
# Password / key hashing
# --------------------------------------------------------------------------
def hash_secret(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_secret(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
@dataclass
class CurrentUser:
    id: int
    username: str
    is_admin: bool
    is_device: bool


def create_access_token(user_id: int, username: str, is_admin: bool) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": username,
        "uid": user_id,
        "is_admin": is_admin,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def _user_from_token(token: str) -> CurrentUser:
    payload = _decode_token(token)
    row = await pool().fetchrow(
        "SELECT id, username, is_admin, is_device FROM users WHERE id = $1",
        payload.get("uid"),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    return CurrentUser(id=row["id"], username=row["username"], is_admin=row["is_admin"], is_device=row["is_device"])


async def _user_from_header_token(authorization: str | None) -> CurrentUser:
    """For get_admin_or_service() — reads the raw Authorization header directly
    (no HTTPBearer) so those routes don't pick up a padlock in Swagger."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _user_from_token(authorization.split(" ", 1)[1].strip())


async def get_current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """Requires a valid JWT for a user with is_admin = true."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await _user_from_token(credentials.credentials)
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


# --------------------------------------------------------------------------
# Static device / service keys (optional shortcut, see module docstring)
# --------------------------------------------------------------------------
async def _user_by_username(username: str) -> CurrentUser | None:
    row = await pool().fetchrow(
        "SELECT id, username, is_admin, is_device FROM users WHERE username = $1",
        username,
    )
    if row is None:
        return None
    return CurrentUser(id=row["id"], username=row["username"], is_admin=row["is_admin"], is_device=row["is_device"])


async def _try_static_key(header_value: str | None, configured_key: str | None, configured_username: str | None) -> CurrentUser | None:
    """Returns the CurrentUser if header_value matches the configured key
    (constant-time compare), else None. None is also returned (rather than
    raising) when the key pair isn't configured at all, so callers can fall
    through to JWT."""
    if not configured_key or not configured_username or not header_value:
        return None
    if not secrets.compare_digest(header_value, configured_key):
        return None
    user = await _user_by_username(configured_username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Configured key username {configured_username!r} does not exist as a user",
        )
    return user


async def get_uploader(
    authorization: str | None = Header(default=None),
    x_device_key: str | None = Header(default=None),
) -> CurrentUser:
    """
    Auth for POST /images/upload: a real JWT login, OR (if configured)
    DEVICE_API_KEY as a fixed shortcut for machine callers — see
    settings.device_api_key / settings.device_api_key_username.
    """
    settings = get_settings()
    via_key = await _try_static_key(x_device_key, settings.device_api_key, settings.device_api_key_username)
    if via_key is not None:
        return via_key
    if authorization:
        return await _user_from_header_token(authorization)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Provide either an Authorization: Bearer token or X-Device-Key",
    )


async def get_ocr_client(
    authorization: str | None = Header(default=None),
    x_ocr_key: str | None = Header(default=None),
) -> CurrentUser:
    """
    Auth for /admin/images/ocr/*: a real admin JWT login, OR (if
    configured) OCR_CLIENT_KEY as a fixed shortcut — see
    settings.ocr_client_key / settings.ocr_client_key_username, which
    must name an existing ADMIN account.
    """
    settings = get_settings()
    via_key = await _try_static_key(x_ocr_key, settings.ocr_client_key, settings.ocr_client_key_username)
    if via_key is not None:
        return via_key
    if authorization:
        user = await _user_from_header_token(authorization)
        if not user.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
        return user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Provide either an Authorization: Bearer token (admin) or X-OCR-Key",
    )


async def get_admin_or_service(
    authorization: str | None = Header(default=None),
    x_device_key: str | None = Header(default=None),
    x_ocr_key: str | None = Header(default=None),
) -> CurrentUser:
    """
    NOT confirmed from your real code — my own addition for the
    read-mostly /admin/images* and /admin/meters/* routes, so a service
    account (or either static key) can read without a human admin login.
    Accepts an admin JWT, or DEVICE_API_KEY, or OCR_CLIENT_KEY.
    """
    settings = get_settings()
    for header_value, key, username in (
        (x_device_key, settings.device_api_key, settings.device_api_key_username),
        (x_ocr_key, settings.ocr_client_key, settings.ocr_client_key_username),
    ):
        via_key = await _try_static_key(header_value, key, username)
        if via_key is not None:
            return via_key
    if authorization:
        return await _user_from_header_token(authorization)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Provide an Authorization: Bearer token, X-Device-Key, or X-OCR-Key",
    )
