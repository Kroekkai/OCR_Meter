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
    port: int = 3005

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
    image_group_window_seconds: int = 60
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

    # --- Scheduled-vs-test capture detection ---------------------------------
    # Removed entirely (Project Carbon firmware update, confirmed) — this
    # used to be a tolerance window for comparing device_timestamp
    # against device_config's schedule (app/schedule_match.py, deleted).
    # Replaced by the ESP32's own wakeup_reason query param on
    # POST /images/upload ("timer" vs "manual") — see
    # app/routers/images.py::upload_image()'s docstring for the full
    # reasoning, and app/filename.py::is_test_filename() for how the
    # decision gets stored (still a "_Test" filename suffix, just
    # decided differently now).

    # --- External push (CFO Platform / NT Carbon) ---------------------------
    # Base URL — confirmed: kept as an env-configured value (.env), not
    # hardcoded here, even though the spec doc itself states it
    # directly (section 2: "API Base URL: https://carbon.ntplc.co.th/
    # engineer-api") and it isn't a secret like the API key — keeping
    # every environment-specific value in .env in one place, rather
    # than splitting "public but env-set" from "secret and env-set"
    # across two different mechanisms (a code default here vs. .env
    # elsewhere), is the confirmed preference. See .env.example for the
    # real value to copy in.
    #
    # Both endpoints hang off this base:
    #   POST {base}/external/v1/meter-readings  (push a reading)
    #   GET  {base}/external/v1/device            (verify a device's SN)
    # Blank default here is a deliberate loud signal that .env hasn't
    # been set up yet — once blank, the scheduler below
    # (external_push_sweep_loop, see app/main.py) skips every tick
    # without attempting anything, rather than repeatedly trying to
    # hit an empty URL.
    #
    # No API key here, confirmed — a single system-wide key was wrong:
    # the spec requires ONE KEY PER DEVICE (each tied to its own SN),
    # not one shared key for the whole integration. Per-device keys live
    # in external_api_keys (db/init.sql), encrypted via app/crypto.py —
    # never in this file. THAT is the actual secret still pending —
    # per-meter keys, issued individually by CFO Platform's own admin
    # UI ("ระบบจะแสดงกุญแจเพียงครั้งเดียว"), not something published in
    # the spec doc the way the base URL is.
    external_api_base_url: str = ""

    # ข้อ 18, confirmed request — how often the background scheduler
    # (external_push_sweep_loop, app/external_push.py +
    # app/main.py's lifespan) calls process_due_pushes() to check for
    # anything needing a (first attempt or retry) push. 30s, not the
    # 5s group_sweep_interval_seconds uses above — confirmed choice:
    # the retry schedule's own finest granularity is already 30s (see
    # RETRY_SCHEDULE_SECONDS in app/external_push.py), so polling any
    # more often than that can't make a retry fire any sooner, only
    # waste DB queries finding nothing new each time.
    external_push_sweep_interval_seconds: int = 30

    # --- External push (CFO Platform): per-device API key encryption --------
    # confirmed request: external_api_keys.api_key_encrypted must be
    # reversible (not a one-way hash like users.password_hash), since the
    # plaintext key has to go back out in the X-API-Key header on every
    # push. Fernet (symmetric, from the `cryptography` package) is a
    # flagged default choice — not yet confirmed with you specifically —
    # swap app/crypto.py for a different scheme (e.g. a KMS) if wanted.
    # Generate a real value with: python -c "from cryptography.fernet
    # import Fernet; print(Fernet.generate_key().decode())" — the blank
    # default is a deliberate loud failure (see app/crypto.py) rather
    # than a silently-insecure fallback key.
    external_key_encryption_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
