"""
Central configuration, loaded from environment variables (.env in dev,
real env vars in the Docker container). Field names/env var names below
are matched exactly to the real .env you shared — not renamed for style.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Postgres -----------------------------------------------------
    # Default only — docker-compose.yml/.local.yml always override this
    # explicitly. Production uses the container name "timescaledb" (same
    # innovation_net network), not the host's own IP — connecting via the
    # host's external IP timed out (container-to-own-host routing).
    db_host: str = "timescaledb"
    db_port: int = 5432
    db_user: str = "CHANGE_ME"
    db_password: str = "CHANGE_ME"
    db_name: str = "cfo_iot"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- HTTP / reverse proxy ------------------------------------------
    # Stripped from the incoming request path (if present) by
    # StripPathPrefixMiddleware before routing — see app/main.py. Safe to
    # leave at "/iot" even if the proxy already strips it itself: the
    # middleware is a no-op on any path that doesn't start with this
    # prefix. Set to "" if not behind a proxy with a base path at all.
    base_path_prefix: str = "/iot"
    port: int = 3003

    # --- Auth: JWT ---------------------------------------------------------
    # Must match meter-dashboard's JWT_SECRET exactly — both services
    # decode the same tokens, meter-dashboard has no users table of its
    # own. Generate with: openssl rand -hex 32
    jwt_secret: str = "changeme-please-set-a-real-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # --- Auth: static device/service keys (optional) ------------------------
    # Alternative to a real login for machine callers. Leave both of a
    # pair blank to disable it and require a real JWT login instead.
    #
    # X-Device-Key on POST /images/upload — must be an EXISTING username
    # (any is_device account, e.g. "esp32").
    device_api_key: str | None = None
    device_api_key_username: str | None = None
    # X-OCR-Key on /admin/images/ocr/* — must be an EXISTING ADMIN
    # username (e.g. "ocr-service").
    ocr_client_key: str | None = None
    ocr_client_key_username: str | None = None

    # --- Uploads -----------------------------------------------------------
    upload_dir: str = "/data/images"
    max_upload_mb: int = 15

    # --- OCR job queue -----------------------------------------------------
    # Once ocr_jobs.attempts reaches this value for a job that keeps
    # failing, the job is left in a terminal 'failed' state and further
    # /claim or /fail calls against it are rejected (409) instead of being
    # silently accepted. This is what stops a mis-behaving OCR client from
    # retrying the same job forever and running the attempts counter into
    # the thousands.
    max_ocr_attempts: int = 5

    # --- Image grouping (burst uploads) -------------------------------------
    # ESP32 sends multiple photos per reading (a "burst", e.g. 3 images a
    # few seconds apart). The server waits this many seconds after the
    # first image in a burst before finalizing the group into a single
    # ocr_jobs row — whatever arrived by then, not necessarily all of
    # them. See app/grouping.py.
    image_group_window_seconds: int = 180
    # How often the background sweep checks for expired groups to
    # finalize. Independent of the window above — this is just the poll
    # interval, not the wait time itself.
    group_sweep_interval_seconds: int = 5
    # Fast path: if a group already has this many images, it's finalized
    # into ocr_jobs IMMEDIATELY on upload — doesn't wait for the window
    # above at all. The window is only the FALLBACK for groups that never
    # reach this count (e.g. only 1-2 images arrive) — those still wait
    # the full image_group_window_seconds via the background sweep, same
    # as before. See app/routers/images.py's upload handler.
    image_group_size: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
