# OCR Meter Store

Python (FastAPI + asyncpg) implementation of the OCR Meter Store API —
hardware image capture + OCR job queue for electric/water/gas meters.
Schema is `db/init.sql` (unchanged from what you provided).

## Status — confirmed vs. still open

This is being built against a real, already-deployed system, matched
piece by piece against docs/config as they come in. Tracking here so
nothing gets silently re-guessed or re-flipped:

**Confirmed, implemented:**
- `ocr_jobs` is one shared table across meter types (per `db/init.sql`) — not split into `ocr_jobs_electric/water/gas`.
- Upload filename convention: `{meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg`, meter_id/device_timestamp parsed from it (Thailand local time, UTC+7), invalid meter_id prefix → HTTP 400.
- Auth is fixed-secret-per-deployment: `DEVICE_API_KEY`/`DEVICE_API_KEY_USERNAME` and `OCR_CLIENT_KEY`/`OCR_CLIENT_KEY_USERNAME`, each an *optional shortcut* alongside real JWT login (blank pair = login required). See `app/auth.py`.
- `JWT_SECRET`/`JWT_EXPIRE_MINUTES` — shared with `meter-dashboard`, same env var names.
- `BASE_PATH_PREFIX=/iot` stripped by app-level ASGI middleware (`app/prefix_middleware.py`), not FastAPI `root_path`.
- `DB_USER`/`DB_PASSWORD`/`DB_NAME`, `MAX_UPLOAD_MB` — renamed to match your real `.env` exactly (previously `POSTGRES_*`, corrected per your last message).
- Production `DB_NAME=cfo_iot` (confirmed) — **not** `imagestore`. Note: the pgweb screenshots and sample rows used earlier in this build (the `esp32`/`ocr-service`/`dashboard-service` users, the `ocr_jobs` attempts-storm data) came from a database named `imagestore`, which is a *different* database from production `cfo_iot`. Schema should be identical (same `init.sql`), but treat any specific row-level data from those screenshots as reference/staging data, not necessarily what's live in `cfo_iot` right now.
- On-disk filenames use the original uploaded filename directly (e.g. `e101_20260824_140000_01.jpg`), no `image_id` prefix — confirmed acceptable since meter_id+HHMMSS+seq is treated as guaranteed unique by convention. If that ever isn't true in practice, two uploads with the same filename will silently overwrite each other's file on disk (DB rows stay separate either way). See `app/storage.py`.
- `meter_id` is normalized to lowercase at parse time (`app/filename.py`) — `E101` and `e101` always store as `e101`, so the same physical meter's history never fragments across two casings.
- **Burst upload grouping** — server waits `IMAGE_GROUP_WINDOW_SECONDS` (default 30) after a burst's first image, one `ocr_jobs` row per group (not per image), OCR picks a single winning image per group. Confirmed via 3 explicit answers — see "Burst upload grouping" section below for the full design.
- **New `ocr_meter` table** (replaces the old "ocr_jobs only" results model) — a clean, standalone results table with no FK back to `images_*`/`ocr_jobs`, meant to be handed off to the external Store system. `ocr_jobs` stays exactly as before as the *internal* job queue (state machine, `attempts` cap, retry-storm guard) — the two are deliberately decoupled. Confirmed via 3 explicit answers:
  - Queue (`ocr_jobs`) and results (`ocr_meter`) are two separate tables, not one.
  - The OCR client computes the month-over-month "reading decreased" comparison itself (pulls history via the new `GET /admin/meters/{meter_id}/ocr-readings` endpoint), not the server.
  - `reading_date`/`reading_time` are two separate DB columns (`DATE` + `TIME`), not one combined timestamp.
  - See `db/init.sql` (or the standalone `db/add-ocr-meter.sql` for the already-deployed `cfo_iot` database) and `app/routers/ocr_jobs.py`'s `/result` endpoint for the full design.

**Still open — not confirmed from what you've shared:**
- `get_admin_or_service()` (the combined admin-JWT-or-service-key dependency on the read-mostly `/admin/images*`/`/admin/meters/*` routes) is my own addition — I don't have confirmation this exists in your real code, or what it actually requires.
- Whether the JWT-login path for `/images/upload` and `/admin/images/ocr/*` also requires `is_device`/`is_admin` on the logged-in user, or accepts any authenticated account.
- `ocr_client_poller.py` — the version I wrote earlier in this conversation (text only, not yet a file) will need updating for the new `/result` request shape (multipart with `reading_date`/`reading_time`/`error_type`, not a plain JSON `ocr_reading`) and to call the new `/ocr-readings` endpoint for its own history check — ask if you want that regenerated.
- `GET /admin/meters/{meter_id}/ocr-readings` is a brand new endpoint, not part of the original confirmed Swagger list — added specifically to support the OCR client's month-over-month comparison. Flag if it should be named/shaped differently, or if it should require `OCR_CLIENT_KEY` specifically rather than any admin-or-service credential.

## Endpoints implemented

Matches the OpenAPI list from the Swagger screenshots, plus one addition (marked below) not in the original list:

```
GET    /health
POST   /register
POST   /login
GET    /admin/users                              [admin JWT]
POST   /admin/users                               [admin JWT]
POST   /images/upload                              [X-Device-Key]
GET    /admin/images
GET    /admin/images/ocr
POST   /admin/images/ocr/{job_id}/claim            [X-OCR-Key]
POST   /admin/images/ocr/{job_id}/result           [X-OCR-Key]   (multipart — see "ocr_meter" below)
POST   /admin/images/ocr/{job_id}/fail             [X-OCR-Key]
POST   /admin/images/{item_id}/reprocess           [admin JWT]
GET    /admin/images/{item_id}
DELETE /admin/images/{item_id}                    [admin JWT]
GET    /admin/meters/{meter_id}/history
GET    /admin/meters/{meter_id}/ocr-readings                     (NEW — not in original spec, see below)
GET    /admin/images/{item_id}/file
GET    /admin/images/{item_id}/ocr-result-file
PUT    /admin/images/{item_id}/ocr-manual         [admin JWT]
```

## Auth design

The Swagger screenshots show a padlock icon on only 4 routes
(`GET/POST /admin/users`, `DELETE /admin/images/{id}`,
`PUT .../ocr-manual`) — those use a declared JWT bearer security scheme;
everything else uses a plain custom header (FastAPI/Swagger doesn't
auto-flag those as "security", so no padlock even though they're
authenticated).

- **Admin JWT** (`Authorization: Bearer <token>`, from `POST /login`,
  requires `is_admin = true`) — user management and the two
  destructive/override actions. `JWT_SECRET` must match
  `meter-dashboard`'s exactly — both services decode the same tokens,
  and this service is the only one with a `users` table.
- **`X-Device-Key: <DEVICE_API_KEY>`** on `POST /images/upload` — a
  single fixed key for the whole deployment (not per-device). On match,
  the upload is attributed to the existing user named by
  `DEVICE_API_KEY_USERNAME` (e.g. `esp32`). This is an *optional
  shortcut*: leave `DEVICE_API_KEY`/`DEVICE_API_KEY_USERNAME` blank and
  uploads require a real JWT login instead.
- **`X-OCR-Key: <OCR_CLIENT_KEY>`** on `/admin/images/ocr/*` — same
  mechanism, attributed to `OCR_CLIENT_KEY_USERNAME`, which must be an
  existing **admin** account (e.g. `ocr-service`). Same
  blank-to-disable behavior, falling back to a real admin JWT login.
- The read-mostly `/admin/images*`/`/admin/meters/*` routes accept an
  admin JWT **or** either static key — this combined dependency
  (`get_admin_or_service()`) is my own addition, not confirmed from
  your code (see "Still open" above).

`DEVICE_API_KEY_USERNAME`/`OCR_CLIENT_KEY_USERNAME` must already exist
as real rows in `users` — the key only maps to an identity, it doesn't
create the account. Bootstrap them with `scripts/create_user.py`
(their `--password` value is never actually checked on the key path,
since that path doesn't touch `password_hash` at all — but the column
is `NOT NULL`, so pass something):

```bash
python -m scripts.create_user --username esp32 --password '<throwaway>' --device
python -m scripts.create_user --username ocr-service --password '<throwaway>' --admin --device
```

Also use `scripts/create_user.py` to create the very first human admin
account (chicken-and-egg: `POST /admin/users` itself requires an
existing admin).

## The stuck-retry bug (`OCR_API_URL is not configured`, attempts > 2500)

Looking at your `ocr_jobs` data: jobs 12–16 sit at `status = 'failed'`
with `attempts` in the thousands. That combination — already terminal,
yet still climbing — means whatever was calling
`/admin/images/ocr/{job_id}/fail` was doing so **repeatedly on a job
that was already failed**, and nothing on the API side rejected that.

This implementation adds a state-machine guard on the job's `status`
column (no schema change needed):

```
queued --[claim]--> processing --[result]--> done
                                --[fail]-----> failed   (terminal)
```

`/claim`, `/result`, and `/fail` now check the job's *current* status
before acting, and return **409 Conflict** if it's not in the expected
state — e.g. `/fail` on a job that's already `'failed'` is rejected
instead of silently bumping `attempts` again. That stops the counter
from growing unbounded and gives the caller an actual error to notice,
instead of a quiet 200 OK.

The only way to give a failed job a fresh attempt is
`POST /admin/images/{item_id}/reprocess`, which — per your own comment
in `init.sql` — creates a **new** `ocr_jobs` row (`attempts = 0`)
rather than resetting the old one, so history isn't lost.

**This fixes the symptom on the API side, not necessarily the root
cause.** The actual retry loop lives in the external OCR client (the
box in your architecture diagram that polls → claims → processes) —
if it's the one calling `/fail` on jobs it doesn't own or has already
been told are terminal, that code needs the same fix. `OCR_API_URL not
configured` specifically means the *OCR client's own* `.env` is missing
that variable — that's config on the client side, not something this
service can fix from here.

## `ocr_meter` — clean results table, separate from the job queue

`ocr_jobs` and `ocr_meter` are deliberately two different tables with
two different jobs:

- **`ocr_jobs`** — internal only. Just the claim/retry state machine
  (`queued`/`processing`/`done`/`failed`, `attempts`, the 409 guard from
  the section above). Nothing outside this service should read it.
- **`ocr_meter`** — the real output. One row per *finished* OCR attempt,
  written by `/result`. No FK to `images_*` or `ocr_jobs` at all — it's
  meant to be hand-off-ready for the external Store system without that
  system needing to know anything about our internal job queue.

`POST /admin/images/ocr/{job_id}/result` now covers both a successful
read AND the 3 known business-error outcomes — all four are "OCR
finished and has a definitive answer," as opposed to `/fail`, which is
only for a transient/technical failure where OCR never got far enough
to produce any answer at all (network error, `OCR_API_URL` missing,
etc.). Request is now **multipart/form-data**, not JSON, so it can carry
an optional annotated result image alongside the fields:

| field | required | notes |
|---|---|---|
| `reading_date` | always | date OCR performed the read |
| `reading_time` | always | time OCR performed the read |
| `ocr_reading` | success + `reading_decreased` only | omit for the other two error types |
| `error_type` | only on error | `no_digits_found` \| `image_unreadable` \| `reading_decreased`, omit entirely on success |
| `error_detail` | optional | free-text detail |
| `result_image` | optional | annotated OCR image file |

```bash
# success
curl -X POST http://localhost:3003/admin/images/ocr/1/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'reading_date=2026-08-25' -F 'reading_time=09:31:02' \
    -F 'ocr_reading=12345'

# error case 1: no digits found in the image
curl -X POST http://localhost:3003/admin/images/ocr/2/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'reading_date=2026-08-25' -F 'reading_time=09:34:11' \
    -F 'error_type=no_digits_found' -F 'error_detail=YOLO found nothing'

# error case 3: reading is present but lower than last month (client-computed)
curl -X POST http://localhost:3003/admin/images/ocr/3/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'reading_date=2026-08-25' -F 'reading_time=09:37:20' \
    -F 'ocr_reading=9800' -F 'error_type=reading_decreased'
```

The OCR client is responsible for the `reading_decreased` check itself —
pull this meter's history first, compare, then decide `error_type`
before calling `/result`:

```bash
curl -s http://localhost:3003/admin/meters/e101/ocr-readings \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production'
```

## Burst upload grouping — multiple photos per reading, one job

ESP32 sends more than one photo per meter reading (a "burst" — e.g. 3
shots a few seconds apart). Confirmed design (3 explicit answers):

- **The server** decides when a group is "complete", not the OCR client.
- **Time-based**: the server waits `IMAGE_GROUP_WINDOW_SECONDS` (default
  30) after the *first* image of a burst arrives, then finalizes the
  group with whatever showed up — it does not require an exact count and
  will not wait forever for a straggler.
- **OCR picks one winner**: the OCR client tries every image in the
  group and submits a single result for the whole group, referencing
  whichever one image it judged best — not an aggregate of all three.

**How it works:**

1. `POST /images/upload` no longer creates an `ocr_jobs` row directly.
   It looks for a still-open group for that `meter_id` (an anchor image —
   `images_*.group_id = id` — received within the window) and joins it,
   or starts a new group if none is open. `image_id` in the response is
   gone; you get `group_id` instead, and `ocr_job_id` is always `null`
   (the job doesn't exist yet).
2. A background task (`app/grouping.py`, started in `app/main.py`'s
   lifespan — not tied to any HTTP request) checks every
   `GROUP_SWEEP_INTERVAL_SECONDS` (default 5) for groups whose window has
   closed, and creates exactly **one** `ocr_jobs` row per group,
   referencing the group's anchor image.
3. `POST /admin/images/ocr/{job_id}/claim` now returns
   `image_file_urls` (plural — a list, one URL per image in the group),
   not a single `image_file_url`.
4. `POST /admin/images/ocr/{job_id}/result` is unchanged in shape — the
   OCR client just submits its one chosen result (optionally with
   `result_image`, the winning photo) the same way as before. Internally,
   every image in the group gets `ocr_status` updated together (not just
   the anchor).

**Schema**: `images_electric/water/gas` gained two columns —
`group_id BIGINT NOT NULL` (self-referencing: the anchor's `group_id`
equals its own `id`; other images in the burst point at that anchor) and
`received_at TIMESTAMPTZ NOT NULL` (server receive time — used for the
window check; deliberately separate from `device_timestamp`, which is
client-supplied and can't be trusted to measure server-side elapsed
time). See `db/init.sql` for the full rationale, or
`db/add-image-grouping.sql` for a standalone migration that safely
backfills existing rows (each becomes a group of one) without touching
`ocr_jobs`/`ocr_meter`.

**Concurrency**: both the upload path and the background sweep lock the
anchor row with `SELECT ... FOR UPDATE` before deciding — this is what
prevents a group from being finalized into a job at the exact moment a
new image is trying to join it (the image would otherwise silently join
a group that's already been queued and never get processed).

**Known rough edge**: deleting a group's anchor image
(`DELETE /admin/images/{item_id}`) does not cascade to or re-anchor its
still-present siblings — see that endpoint's docstring in
`app/routers/images.py`. Flag if you want that hardened.

## Running locally

```bash
cp .env.example .env   # fill in DB_USER / DB_PASSWORD / JWT_SECRET / etc.
docker compose up --build
```

The DB itself (`192.168.248.199:5432`, database from `DB_NAME`) is
assumed to already exist and already have `db/init.sql` applied — this
compose file does not manage a Postgres container. If it's a brand-new
database, run `db/init.sql` against it once by hand
(`psql -f db/init.sql`) before starting the app.

### Testing on localhost first (no production DB needed)

`docker-compose.local.yml` is a separate stack that adds a throwaway
local Postgres, so you can smoke-test everything before pointing at the
real `192.168.248.199` database. It also sets dev `DEVICE_API_KEY`/
`OCR_CLIENT_KEY` values so you can test the no-login key path too:

```bash
docker compose -f docker-compose.local.yml up --build
```

This runs `db/init.sql` automatically against the local Postgres on
first start. Once it's up:

```bash
# 1. health check
curl http://localhost:3003/health

# 2. create the first admin, esp32, and ocr-service accounts, then log in as admin
docker compose -f docker-compose.local.yml exec ocr-meter-store \
    python -m scripts.create_user --username admin --password 'devpassword123' --admin
docker compose -f docker-compose.local.yml exec ocr-meter-store \
    python -m scripts.create_user --username esp32 --password 'unused-but-required' --device
docker compose -f docker-compose.local.yml exec ocr-meter-store \
    python -m scripts.create_user --username ocr-service --password 'unused-but-required' --admin --device

curl -s -X POST http://localhost:3003/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"devpassword123"}'
# -> {"access_token": "...", ...}  — save this as $TOKEN

# 3. upload an image via the no-login device key (matches DEVICE_API_KEY_USERNAME=esp32)
# meter_id and the capture timestamp come from the FILENAME itself —
# {meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg — rename any test JPEG to match:
cp /path/to/some.jpg e101_20260818_151230_01.jpg

curl -s -X POST http://localhost:3003/images/upload \
    -H 'X-Device-Key: dev-device-key-not-for-production' \
    -F 'file=@e101_20260818_151230_01.jpg'
# -> {"image": {...}, "group_id": 1, "ocr_job_id": null}
# ocr_job_id is always null here now — a burst upload window is running
# (5s in this local stack, see docker-compose.local.yml). Wait ~5s, then:

curl -s "http://localhost:3003/admin/images/ocr?job_status=queued" \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production'
# -> [{"id": 1, "image_id": 1, ...}]  — the job the background sweep just created

# 4. run job 1 through claim -> result via the no-login OCR key
curl -s -X POST http://localhost:3003/admin/images/ocr/1/claim \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production'
# -> {"job": {...}, "image_file_urls": ["/admin/images/1/file"]}  — plural,
# one URL per image in the burst group (just one here since only one was uploaded)

curl -s -X POST http://localhost:3003/admin/images/ocr/1/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'reading_date=2026-08-18' -F 'reading_time=15:12:45' \
    -F 'ocr_reading=12345'

# 5. list images as admin
curl -s http://localhost:3003/admin/images -H "Authorization: Bearer $TOKEN"
```

Interactive docs (Swagger UI) are at `http://localhost:3003/docs` — same
shape as the production `/iot/docs`, just without the `/iot` prefix
since `BASE_PATH_PREFIX` is empty in the local stack.

Once this all checks out, switch to the real `docker-compose.yml`
(production DB + `/iot` base path + `innovation_net`) for the actual
deploy.

Create your real admin/esp32/ocr-service accounts in production the same
way, against the real container:

```bash
docker compose exec ocr-meter-store python -m scripts.create_user \
    --username admin --password 'change-me' --admin
```

## Deployment notes (per your spec)

- Container listens on **port 3003 only**.
- `BASE_PATH_PREFIX=/iot` is baked into `docker-compose.yml`, matching
  the public base URL `https://cfo.ntplc.co.th/iot`. The middleware
  (`app/prefix_middleware.py`) strips it if present and is a no-op if
  the proxy already strips it before forwarding — safe either way, per
  your own note that you weren't 100% sure which.
- `innovation_net` is declared `external: true`, so create it first if
  it doesn't exist yet: `docker network create innovation_net`.
- I dropped `NODE_ENV=production` from the compose env — that's a
  Node.js convention and this is a pure-Python service, so it wouldn't
  do anything here. Let me know if it's actually needed for something
  else in your stack (e.g. a shared reverse-proxy config) and I'll add
  it back.
- `DB_HOST`/`DB_PORT`/`TZ`/`BASE_PATH_PREFIX` are pinned in
  `docker-compose.yml` as specified; set `DB_USER`/
  `DB_PASSWORD`/`DB_NAME`/`JWT_SECRET`/`DEVICE_API_KEY`/
  `OCR_CLIENT_KEY` in `.env` (see `.env.example`).

## Things still worth double-checking

- **`get_admin_or_service()`** (admin-JWT-or-either-static-key, on the
  read-mostly `/admin/images*`/`/admin/meters/*` routes) — my own
  addition, not confirmed against your real code.
- **OCR result submission** (`POST .../result`): JSON body
  `{"ocr_reading": <number>}` — no annotated result image upload wired
  in yet, even though `GET .../ocr-result-file` expects one to exist.
  If the real OCR client also uploads an annotated result image, tell
  me and I'll add a multipart file field to `/result`.
- **`meter_id` → table routing**: first letter `e/w/g` (case-insensitive),
  per your comment in `init.sql` and the "ประเภทมิเตอร์" spec page.
# OCR_Meter
