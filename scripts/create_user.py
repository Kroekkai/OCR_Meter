"""
One-off CLI to create a user directly in the DB — mainly for bootstrapping
the first admin account (POST /admin/users itself requires an admin JWT,
so there has to be some other way to create the very first one), and for
creating device/service accounts (is_device=true) like esp32-cam-1 or
ocr-service without going through /register (which always creates a
plain, non-admin, non-device account).

Run inside the container, e.g.:
    docker compose exec ocr-meter-store python -m scripts.create_user \
        --username kroekkai --password '...' --admin

    docker compose exec ocr-meter-store python -m scripts.create_user \
        --username ocr-service --password '...' --device
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/srv")

from app import db  # noqa: E402
from app.auth import hash_secret  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--device", action="store_true")
    args = parser.parse_args()

    await db.connect()
    try:
        existing = await db.pool().fetchval("SELECT 1 FROM users WHERE username = $1", args.username)
        if existing:
            print(f"User {args.username!r} already exists — aborting.")
            return
        row = await db.pool().fetchrow(
            """
            INSERT INTO users (username, password_hash, is_admin, is_device)
            VALUES ($1, $2, $3, $4)
            RETURNING id, username, is_admin, is_device
            """,
            args.username,
            hash_secret(args.password),
            args.admin,
            args.device,
        )
        print(f"Created user: {dict(row)}")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
