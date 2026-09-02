# OCR Meter Store

Python (FastAPI + asyncpg) implementation of the OCR Meter Store API —
hardware image capture + OCR job queue for electric/water/gas meters.
Schema is `db/init.sql` (unchanged from what you provided).

## Status — confirmed vs. still open

This is being built against a real, already-deployed system, matched
piece by piece against docs/config as they come in. Tracking here so
nothing gets silently re-guessed or re-flipped:

**Confirmed, implemented:**
- `error_type` is a plain integer 0/1/2/3 (not free-form text) — meanings live in the `error_type` lookup table (`db/init.sql`), server owns the definitions, OCR client just reports the code. Case 3 ("read a value, but anomalous") replaces the old `reading_decreased`/`usage_anomaly` text values as one combined case — still client-computed, server doesn't run this check. `error_detail` column removed from `ocr_meter`. `capture_date`/`capture_time` derived server-side from the job's `device_timestamp` now, never client-supplied. **No file upload on `/result` at all anymore** — it's plain form fields, not multipart; the old `result_image` field is gone (see "ocr_meter" below for why — it was a real risk, not just unnecessary). `ocr_meter.ocr_image_filename` renamed to `image_error`, and its value is now the FULL disk path (e.g. `/data/images/E101_..._01.jpg`, not just the bare filename) to the job's own `original_filename` (the anchor's already-stored file), not a separately uploaded one. `GET /admin/images/{item_id}/ocr-result-file` (original spec) removed accordingly — use `/file` instead. `meter_id` stored uppercase everywhere (was lowercase). `ocr_jobs.last_error`/`admin_reason` columns removed (not persisted anywhere now — `/fail`'s error message only reaches the server log). `group_id` is now the E1/W3/G12-style text code directly (the old numeric `group_id`/self-reference anchor mechanism and the separate `group_label` column from an earlier revision are both gone — merged into one `group_id` column, with a new `is_anchor` boolean replacing the self-reference trick), on `images_*`/`ocr_jobs`. A group also now finalizes into `ocr_jobs` immediately once it reaches `IMAGE_GROUP_SIZE` (3) images, not just on the 60s window fallback. **`ocr_meter` does NOT carry `group_id`** — briefly did in an intermediate revision, confirmed removed: `ocr_meter` is exactly 6 fields (`meter_id`, `capture_date`, `capture_time`, `ocr_reading`, `error_type`, `image_error`), nothing else, group tracking is an `images_*`/`ocr_jobs`-internal concern only. See "ocr_meter", "group_id", and "Burst upload grouping" sections below.
- `DB_HOST=timescaledb` (container name on `innovation_net`), **not** the host's own IP `192.168.248.199` — connecting via the host's external IP timed out from inside the container (self-referential/hairpin routing back to its own host), confirmed via `docker network inspect innovation_net` while debugging the actual deploy. `timescaledb` and `ocr-meter-store` are both already on that network, so Docker's internal DNS resolves it directly — no IP needed at all.
- **`is_test`/`ocr_meter_test`** — every group is checked server-side against that meter's `device_config` schedule (never the filename) and tagged `is_test` accordingly; results for `is_test=true` jobs go to the new `ocr_meter_test` table instead of `ocr_meter`. At most one normal (non-`is_test`) group per meter per Bangkok calendar day may reach `ocr_jobs` — a same-day duplicate is silently dropped (no job, no error, `images_*` rows left as-is, never deleted); test groups are exempt from this limit entirely. See "Scheduled vs. test captures" below.
- `ocr_jobs` is one shared table across meter types (per `db/init.sql`) — not split into `ocr_jobs_electric/water/gas`.
- Upload filename convention: `{meterId}_{YYYYMMDD}_{HHMMSS}_{seq}.jpg`, meter_id/device_timestamp parsed from it (Thailand local time, UTC+7), invalid meter_id prefix → HTTP 400.
- Auth is fixed-secret-per-deployment: `DEVICE_API_KEY`/`DEVICE_API_KEY_USERNAME` and `OCR_CLIENT_KEY`/`OCR_CLIENT_KEY_USERNAME`, each an *optional shortcut* alongside real JWT login (blank pair = login required). See `app/auth.py`.
- `JWT_SECRET`/`JWT_EXPIRE_MINUTES` — shared with `meter-dashboard`, same env var names.
- `BASE_PATH_PREFIX=/iot` stripped by app-level ASGI middleware (`app/prefix_middleware.py`), not FastAPI `root_path`.
- `DB_USER`/`DB_PASSWORD`/`DB_NAME`, `MAX_UPLOAD_MB` — renamed to match your real `.env` exactly (previously `POSTGRES_*`, corrected per your last message).
- Production `DB_NAME=cfo_iot` (confirmed) — **not** `imagestore`. Note: the pgweb screenshots and sample rows used earlier in this build (the `esp32`/`ocr-service`/`dashboard-service` users, the `ocr_jobs` attempts-storm data) came from a database named `imagestore`, which is a *different* database from production `cfo_iot`. Schema should be identical (same `init.sql`), but treat any specific row-level data from those screenshots as reference/staging data, not necessarily what's live in `cfo_iot` right now.
- On-disk filenames use the original uploaded filename directly (e.g. `e101_20260824_140000_01.jpg`), no `image_id` prefix — confirmed acceptable since meter_id+HHMMSS+seq is treated as guaranteed unique by convention. If that ever isn't true in practice, two uploads with the same filename will silently overwrite each other's file on disk (DB rows stay separate either way). See `app/storage.py`.
- `meter_id` is normalized to lowercase at parse time (`app/filename.py`) — `E101` and `e101` always store as `e101`, so the same physical meter's history never fragments across two casings.
- **Burst upload grouping** — server waits `IMAGE_GROUP_WINDOW_SECONDS` (60s, both local and production) after a burst's first image, one `ocr_jobs` row per group (not per image), OCR picks a single winning image per group. Confirmed via 3 explicit answers — see "Burst upload grouping" section below for the full design.
- **New `ocr_meter` table** (replaces the old "ocr_jobs only" results model) — a clean, standalone results table with no FK back to `images_*`/`ocr_jobs`, meant to be handed off to the external Store system. `ocr_jobs` stays exactly as before as the *internal* job queue (state machine, `attempts` cap, retry-storm guard) — the two are deliberately decoupled. Confirmed via 3 explicit answers:
  - Queue (`ocr_jobs`) and results (`ocr_meter`) are two separate tables, not one.
  - The OCR client computes the month-over-month "reading decreased" comparison itself (pulls history via the new `GET /admin/meters/{meter_id}/ocr-readings` endpoint), not the server.
  - `capture_date`/`capture_time` are two separate DB columns (`DATE` + `TIME`), not one combined timestamp.
  - See `db/init.sql` and `app/routers/ocr_jobs.py`'s `/result` endpoint for the full design. (Previously shipped as a standalone `db/add-ocr-meter.sql` migration — merged into `init.sql` itself now, which is safe to (re-)run against any DB state: fresh, pre-`ocr_meter`, or already up to date.)

**Still open — not confirmed from what you've shared:**
- `get_admin_or_service()` (the combined admin-JWT-or-service-key dependency on the read-mostly `/admin/images*`/`/admin/meters/*` routes) is my own addition — I don't have confirmation this exists in your real code, or what it actually requires.
- Whether the JWT-login path for `/images/upload` and `/admin/images/ocr/*` also requires `is_device`/`is_admin` on the logged-in user, or accepts any authenticated account.
- `GET /admin/meters/{meter_id}/ocr-readings` is a brand new endpoint, not part of the original confirmed Swagger list — added specifically to support the OCR client's month-over-month comparison. Flag if it should be named/shaped differently, or if it should require `OCR_CLIENT_KEY` specifically rather than any admin-or-service credential.

## Endpoints implemented

Matches the OpenAPI list from the Swagger screenshots (minus one removal
and plus one addition, both marked below):

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
POST   /admin/images/ocr/{job_id}/result           [X-OCR-Key]   (plain form fields, not multipart — see "ocr_meter" below)
POST   /admin/images/ocr/{job_id}/fail             [X-OCR-Key]
POST   /admin/images/{item_id}/reprocess           [admin JWT]
GET    /admin/images/{item_id}
DELETE /admin/images/{item_id}                    [admin JWT]
GET    /admin/meters/{meter_id}/history
GET    /admin/meters/{meter_id}/ocr-readings                     (NEW — not in original spec, see below)
GET    /admin/meters/ocr-meter                                   (NEW — not in original spec, see below)
GET    /admin/meters/ocr-meter-test                               (NEW — not in original spec, see below)
GET    /admin/images/{item_id}/file
PUT    /admin/images/{item_id}/ocr-manual         [admin JWT]
GET    /devices/config                                           (NEW — separate spec doc, see "device_config" below)
GET    /admin/device-config                                      (NEW — not in that spec doc either, see below)
GET    /admin/device-config/{meter_id}                           (NEW — not in that spec doc either, see below)
GET    /admin/device-config-ui                                   (NEW — standalone dashboard, see below)
PUT    /admin/device-config/{meter_id}            [admin JWT]     (NEW — not in that spec doc either, see below)
DELETE /admin/device-config/{meter_id}            [admin JWT]     (NEW — not in that spec doc either, see below)
```

`GET /admin/images/{item_id}/ocr-result-file` (in the original spec) was
**removed** — there's no separate "OCR result" file anymore (see
"ocr_meter" below for why). Use the same `/file` endpoint for an
error/anomaly row's referenced image too.

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

**`error_type` is now a plain integer (0/1/2/3), not free-form text** —
the full meaning of each code lives in the `error_type` lookup table in
`db/init.sql` (single source of truth), not scattered across comments:

| code | meaning | `ocr_reading` |
|---|---|---|
| `0` | read successfully | required |
| `1` | found the meter but couldn't read the digits | must be omitted |
| `2` | couldn't find any digits/meter in the image at all | must be omitted |
| `3` | read a value, but it's anomalous (decreased from last time, or usage spike) | required |

Case 3 covers what used to be two separate string values
(`reading_decreased`/`usage_anomaly`) — the OCR client is still the one
that pulls history and decides (server doesn't compute this), it just
reports one combined code now instead of two.

`POST /admin/images/ocr/{job_id}/result` covers both a successful read
AND all 3 error/anomaly outcomes — as opposed to `/fail`, which is only
for a transient/technical failure where OCR never got far enough to
reach any outcome at all (network error, `OCR_API_URL` missing, etc.).
**No file upload at all anymore** — plain form fields, not
multipart/form-data:

| field | required | notes |
|---|---|---|
| `ocr_reading` | codes 0, 3 only | must be omitted for codes 1, 2 |
| `error_type` | always | integer 0/1/2/3 — see table above |

**`capture_date`/`capture_time` are no longer sent by the client at
all** — the server derives them itself from the job's own
`device_timestamp` (when the ESP32 captured the photo), not from
whenever OCR happened to run. **`error_detail` is gone too** — human-
readable descriptions live once, in the `error_type` lookup table, not
repeated per-row.

**No `result_image` upload either** — the OCR client never attaches an
image to this endpoint. `ocr_meter.image_error` (only set when
`error_type != 0`) is the full disk path (via `storage.original_path()`)
to the job's own `original_filename` — e.g. `/data/images/E101_..._01.jpg`
— the same file the group's anchor image was already stored under at
upload time, nothing written twice. An earlier version of this
endpoint accepted a re-uploaded copy via a `result_image` field —
removed: it added nothing (the OCR client can only legitimately attach
one of the group's own already-stored photos anyway), and it was
actively risky — the save path was always computed from the *anchor's*
filename regardless of which photo's bytes were actually attached, so a
client attaching a different image in the group (say, image 2 of 3)
would silently overwrite image 1's file on disk with image 2's content.

```bash
# success
curl -X POST http://localhost:3003/admin/images/ocr/1/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'ocr_reading=12345' -F 'error_type=0'

# case 1: found the meter, couldn't read the digits
curl -X POST http://localhost:3003/admin/images/ocr/2/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'error_type=1'

# case 2: no digits/meter found in the image at all
curl -X POST http://localhost:3003/admin/images/ocr/3/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'error_type=2'

# case 3: read a value, but it's anomalous (client-computed against history)
curl -X POST http://localhost:3003/admin/images/ocr/4/result \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production' \
    -F 'ocr_reading=1510' -F 'error_type=3'
```

The OCR client is still responsible for deciding case 3 itself — pull
this meter's recent *successful* history first (`only_successful=true`
excludes every error/anomaly row, including old case-3 ones, so a
flagged reading never pollutes the "normal" baseline), compute the
deltas/average/threshold, and report `error_type=3` if it decides the
new reading qualifies. The server only ever serves the history; it
never computes this comparison itself:

```bash
curl -s "http://localhost:3003/admin/meters/e101/ocr-readings?limit=3&only_successful=true" \
    -H 'X-OCR-Key: dev-ocr-key-not-for-production'
# -> last 3 successful readings, most recent first — e.g. [1310, 1250, 1200]
# client computes: deltas [60, 50] -> avg 55 -> threshold 55*3=165
# if (new_reading - 1310) > 165 -> submit /result with error_type=3
```

## `group_id` — human-readable group codes (E1, W3, G12, ...)

**Changed again — `group_id` is now the E1/W3/G12-style text code
directly, not a separate `group_label` alongside a numeric anchor.**
Earlier revision had two columns: `group_id` (`BIGINT`, self-referencing
the anchor image's own `id` — jumps around unpredictably since
`images_electric`/`water`/`gas` all share one `images_id_seq`) plus
`group_label` (`TEXT`, the human-readable code) as a separate addition.
Confirmed simplification: drop the numeric one, rename `group_label` to
just `group_id` — one column, one name, in `images_*` and `ocr_jobs`.
**Not in `ocr_meter`** — briefly was, in an intermediate revision, but
confirmed removed: `ocr_meter` is exactly 6 fields and group tracking
isn't one of them (see "ocr_meter" section above).

Assigned once per meter type from its own dedicated sequence
(`electric_group_seq`/`water_group_seq`/`gas_group_seq`) the moment a
brand-new group opens (the first image of a burst, in
`POST /images/upload`). Every other image joining that same group
copies the anchor's existing `group_id` rather than pulling a new one.
It then flows through unchanged: `images_*.group_id` →
`ocr_jobs.group_id` (copied when the group finalizes — see below) — so
either table can be read on its own and still show a sensible `E1`,
`E2`, `E3`, ... sequence for that meter type, without a join.

**`is_anchor` (`BOOLEAN`) replaces the old self-reference trick.**
Since `group_id` is no longer a `BIGINT` that can equal a row's own
`id`, there needs to be an explicit flag marking which row in a group is
the "anchor" (the one whose `meter_id`/`original_filename`/
`device_timestamp` get copied into `ocr_jobs` when the group finalizes,
and the one the claim/sweep race-safety locks against) — `true` for the
first image that opened the group, `false` for every image that joined
it afterward.

## Burst upload grouping — multiple photos per reading, one job

ESP32 sends more than one photo per meter reading (a "burst" — e.g. 3
shots a few seconds apart). Confirmed design:

- **The server** decides when a group is "complete", not the OCR client.
- **Two paths to "complete", whichever comes first:**
  - **Count-based (fast path)**: the moment a group reaches its target
    count, that upload request finalizes the group into `ocr_jobs`
    immediately, synchronously, in the same request — no waiting at
    all. **Target count is per-meter now** — `device_config.photo_count`
    for that specific `meter_id` if it's been configured (via
    `PUT /admin/device-config/{meter_id}` or the dashboard), falling
    back to the system-wide `IMAGE_GROUP_SIZE` setting (3) only for a
    meter that's never been configured — the same fallback
    `GET /devices/config` itself uses via `DEFAULT_CONFIG`. Two meters
    can have different burst sizes this way — one set to `photo_count=5`
    finalizes its groups at 5 images, unaffected by another meter still
    on the default of 3.
  - **Time-based (fallback)**: for a group that never reaches its
    target count (e.g. only 1-2 images ever arrive), the server waits
    `IMAGE_GROUP_WINDOW_SECONDS` (60, both local and production) after
    the *first* image, then finalizes with whatever showed up — it does
    not wait forever for a straggler.
- **OCR picks one winner**: the OCR client tries every image in the
  group and submits a single result for the whole group, referencing
  whichever one image it judged best — not an aggregate of all three.

**How it works:**

1. `POST /images/upload` looks for a still-open group for that
   `meter_id` (an anchor image — `images_*.is_anchor = true` — received
   within the window, with no `ocr_jobs` row yet) and joins it, or
   starts a new group if none is open. After the insert, it counts how
   many images now share that `group_id`, looks up this `meter_id`'s
   own `device_config.photo_count` (falling back to `IMAGE_GROUP_SIZE`
   if unconfigured), and compares; if the count has reached that
   target, it finalizes the group into `ocr_jobs` right then and there,
   and `ocr_job_id` in the response is non-null on exactly that
   request. Otherwise `ocr_job_id` stays `null` — the group isn't done
   yet, and it'll either get more images or eventually get picked up by
   the fallback below.
2. A background task (`app/grouping.py`, started in `app/main.py`'s
   lifespan — not tied to any HTTP request) checks every
   `GROUP_SWEEP_INTERVAL_SECONDS` (default 5) for groups whose window has
   closed *and that don't already have an `ocr_jobs` row* — i.e. only
   the ones the fast path above never got to. Groups that already hit
   their target count and got finalized immediately never show up in
   this sweep at all — the sweep itself doesn't check `photo_count` at
   all, since it only ever runs for groups that *didn't* reach it.
   Both paths funnel through the same `finalize_group()` helper in
   `app/grouping.py`, so the actual `INSERT INTO ocr_jobs` only exists
   in one place.
3. `POST /admin/images/ocr/{job_id}/claim` returns `image_file_urls`
   (plural — a list, one URL per image sharing the job's `group_id`).
4. `POST /admin/images/ocr/{job_id}/result` — see the `ocr_meter` section
   above for the full current shape (`error_type` 0-3,
   `capture_date`/`capture_time` no longer client-supplied, `error_detail`
   gone). Internally, every image in the group gets `ocr_status` updated
   together (not just the anchor).

**Schema**: `images_electric/water/gas` gained columns beyond the
original spec — `group_id TEXT NOT NULL` (the E1/W3/G12 code, shared by
every image in the burst — see the `group_id` section above),
`is_anchor BOOLEAN NOT NULL` (marks the one row per group whose data
gets copied into `ocr_jobs`), and `received_at TIMESTAMPTZ NOT NULL`
(server receive time — used for the window check; deliberately separate
from `device_timestamp`, which is
client-supplied and can't be trusted to measure server-side elapsed
time). `meter_id` is also always stored **uppercase** now (normalized
once in `app/filename.py` at upload time, regardless of what case the
device sent) — was lowercase in an earlier version of this doc; flag if
anything downstream (meter-dashboard, External Store) still expects
lowercase. See `db/init.sql` — it's a single file, safe to (re-)run
against any DB state (fresh, an older schema, or already up to date).

**Concurrency**: both the upload path and the background sweep lock the
anchor row with `SELECT ... FOR UPDATE` before deciding — this is what
prevents a group from being finalized into a job at the exact moment a
new image is trying to join it (the image would otherwise silently join
a group that's already been queued and never get processed).

**Known rough edge**: deleting a group's anchor image
(`DELETE /admin/images/{item_id}`) does not cascade to or re-anchor its
still-present siblings — see that endpoint's docstring in
`app/routers/images.py`. Flag if you want that hardened.

## Scheduled vs. test captures (`is_test`, `ocr_meter_test`)

**Not in either original spec doc — a later addition, confirmed design
across a few rounds.** Every new group gets checked against that
meter's own `device_config` schedule, and is tagged as either a normal
scheduled capture or a one-off test — the two are kept completely
separate all the way through to two different result tables.

**How `is_test` gets decided — confirmed server-side, never
client-side:**
```
app/schedule_match.py::is_on_schedule(meter_id, device_timestamp)
```
Computed exactly once per group, at the moment its anchor image is
inserted (`app/routers/images.py`) — compares `device_timestamp`
against that meter's `device_config.date1`/`date2` (or `DEFAULT_CONFIG`
if unconfigured), within `SCHEDULE_MATCH_TOLERANCE_MINUTES` (default
5 — only the anchor is checked, so this covers "how close is the
wake-up shot to the scheduled time", not the whole burst's duration;
see `app/config.py`). Within tolerance of *either* slot →
`is_test = false`; otherwise `true`. Every other image joining that
group inherits the anchor's `is_test` rather than being checked
individually — a single burst is always entirely normal or entirely
test, never a mix.

**Confirmed: the incoming filename is never trusted or parsed for
this** — there's no `_Test`-suffix convention the server looks *for* on
the way in; whatever naming convention (if any) the ESP32 firmware uses
is a firmware-side concern only, and the server independently
re-derives `is_test` every time from `device_config`, never from a
self-reported flag in the filename.

**What the server *stores* is a different matter — confirmed it does
rename on the way out.** Once `is_test` is decided, `_stored_filename()`
appends `_Test` right before the extension — e.g.
`E101_20260901_130000_1.jpg` → `E101_20260901_130000_1_Test.jpg` —
applied identically to the actual file on disk and to
`images_*.original_filename`, so the two can never disagree. Every
image in a test group gets this, not just the anchor (the join branch
re-applies it too, using the anchor's already-decided `is_test`).

**One normal group per meter per (Bangkok) calendar day — confirmed
rule, applies only to non-test groups:**
```
app/grouping.py::has_normal_group_today(table, meter_id)
```
If a meter's burst times out and re-splits into two separate groups the
same day (e.g. images 1-2 group into `E1` and finalize via the sweep
before image 3 arrives, so image 3 opens a fresh `E2`) — only the
*first* group of the day to reach `ocr_jobs` is kept. Any later normal
group for that same `meter_id` the same day is **silently dropped**
right before it would have been finalized: no `ocr_jobs` row is
created, no error is raised, `ocr_job_id` in the upload response just
stays `null` forever for that group. **Confirmed: the dropped group's
`images_*` rows are left exactly as they are — never deleted, `is_anchor`
stays `true`, `ocr_status` stays `'pending'` indefinitely.** The limit
resets at Bangkok midnight (`00:00` local), computed fresh on every
check — not a stored "last reset" timestamp anywhere.

**Test groups (`is_test = true`) are completely exempt from this daily
limit** — a meter can produce any number of test groups/jobs in a
single day, confirmed. Checked identically in both finalization paths
(the upload handler's fast path and `app/grouping.py`'s sweep), via the
same `has_normal_group_today()` helper — one place the rule lives, not
duplicated.

**`ocr_meter_test`** — a table structurally identical to `ocr_meter`
(same 6 columns, own sequence, own index), confirmed to hold *only*
results from `is_test = true` jobs, completely separate from normal
`ocr_meter` data. `POST /admin/images/ocr/{job_id}/result` picks the
target table by checking `ocr_jobs.is_test` at write time — everything
else about that endpoint (validation, `capture_date`/`capture_time`
derivation, `image_error` path) is identical regardless of which table
the row lands in.

**Reading them back — two explicitly separate endpoints, confirmed
request:**
```
GET /admin/meters/ocr-meter        -> ocr_meter only
GET /admin/meters/ocr-meter-test   -> ocr_meter_test only
```
Both list across *every* meter by default (unlike
`.../{meter_id}/ocr-readings`, which is scoped to one) — an optional
`meter_id` query param narrows either down to a single meter, same
`limit`/`offset` pagination as everywhere else. Neither query joins the
two tables together at all — a meter's test results and its real
results can never mix in a single response from either endpoint.

**Not yet decided / worth flagging:**
- Whether `GET /admin/meters/{meter_id}/ocr-readings` (the endpoint the
  OCR client polls for anomaly-detection history) should read from
  `ocr_meter_test` too when a meter's `only_successful` history is
  entirely test data, or stay `ocr_meter`-only as it is now.
- No admin endpoint lists/inspects dropped groups specifically — a
  dropped group looks identical (from the API's view) to one still
  mid-burst, since both just have `ocr_job_id: null` forever. Flag if
  you want a way to tell these apart (e.g. a `GET .../images?dropped=true`
  filter, or surfacing `has_normal_group_today` on `ImageOut`).

## Running locally

```bash
cp .env.example .env   # fill in DB_USER / DB_PASSWORD / JWT_SECRET / etc.
docker compose up --build
```

The DB itself (container name `timescaledb` on `innovation_net`,
`DB_HOST` — **not** the host's `192.168.248.199`, which times out due to
container-to-own-host routing, confirmed while debugging the real
deploy — database from `DB_NAME`) is assumed to already exist and
already have `db/init.sql` applied — this compose file does not manage
a Postgres container. If it's a brand-new database, run `db/init.sql`
against it once by hand (`psql -f db/init.sql`) before starting the app.

### Testing on localhost first (no production DB needed)

`docker-compose.local.yml` is a separate stack that adds a throwaway
local Postgres, so you can smoke-test everything before pointing at the
real production database. It also sets dev `DEVICE_API_KEY`/
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
cp /path/to/some.jpg E101_20260818_151230_01.jpg

curl -s -X POST http://localhost:3003/images/upload \
    -H 'X-Device-Key: dev-device-key-not-for-production' \
    -F 'file=@E101_20260818_151230_01.jpg'
# -> {"image": {...}, "group_id": "E1", "ocr_job_id": null}
# ocr_job_id is null here — this group has only 1 image so far. E101's
# target is IMAGE_GROUP_SIZE (3) unless it has its own device_config
# row with a different photo_count. Either upload 2 more (or however
# many the target is) within the window to trigger the fast path, or
# wait out the full window (60s, matches production — see
# docker-compose.local.yml) for the fallback sweep to pick it up with
# just this 1 image. Then:

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
    -F 'ocr_reading=12345' -F 'error_type=0'

# 5. list images as admin
curl -s http://localhost:3003/admin/images -H "Authorization: Bearer $TOKEN"
```

Interactive docs (Swagger UI) work both ways locally now —
`http://localhost:3003/docs` **and** `http://localhost:3003/iot/docs`
both work simultaneously, since `BASE_PATH_PREFIX=/iot` matches
production but the middleware only acts on paths that actually start
with it (see `app/prefix_middleware.py`) — anything else passes through
untouched. Use the `/iot/...` form specifically when you want to verify
our own handling of the production path is correct, in isolation from
whatever's happening with the reverse proxy at `cfo.ntplc.co.th`.

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

## `device_config` — ESP32 capture schedule, from a separate spec doc

Not part of the original confirmed spec at all — came from a standalone
"Device Configuration API Specification" doc another team sent,
describing how ESP32 should fetch its own capture schedule.

```bash
curl "http://localhost:3003/devices/config?meter_id=E101" \
    -H 'X-Device-Key: dev-device-key-not-for-production'
```
```json
{"meter_id": "E101", "schedule_mode": 1, "date1": [26,0,0,8,0],
 "date2": [0,0,0,0,0], "photo_count": 3, "photo_delay": 5}
```

- `schedule_mode`: `0` = program/daily mode, `1` = fix-date mode.
- `date1`/`date2`: `[Day, Month, Year, Hour, Minute]`. In daily mode
  only Hour/Minute are meaningful (Day/Month/Year sit at `0`); `date2`
  stays `[0,0,0,0,0]` unless the meter has a second monthly cycle.
- **A meter with no row yet always gets `DEFAULT_CONFIG` back, still
  `200 OK`** — confirmed from the spec doc directly, never a 404.
- **`is_default` (my own addition, not in the spec)** — `true` when this
  response is `DEFAULT_CONFIG` because the meter has no row yet, `false`
  when it's a genuinely stored config. ESP32 ignores the extra field
  (harmless); a dashboard can use it to show "using default" vs
  "customized" state.

**Four things NOT in that spec doc, all my own additions — flag if
any should be different:**

1. **Auth** — the doc only showed a generic `Authorization: Bearer
   <token>` example, no detail on what issues/validates that token.
   This reuses `get_uploader` (the same dependency `/images/upload`
   already uses) — accepts the existing `X-Device-Key`, an admin JWT,
   **or** (confirmed against the real firmware source) the device key
   sent RAW in the `Authorization` header with no `Bearer ` prefix at
   all — `http.addHeader("Authorization", API_AUTH_BEARER_TOKEN)` in
   `fetchDeviceConfigWiFi`/`fetchDeviceConfig4G`, no string
   concatenation with `"Bearer "` anywhere in that code. A strict
   `Authorization: Bearer <token>` parser would 401 this firmware
   outright — `get_uploader` checks for the `bearer ` prefix first and
   falls back to a direct key comparison when it's absent, so this
   exact already-written ESP32 code works without any firmware changes.
2. **`GET /admin/device-config`** — lists every meter that has an
   explicit row (admin-or-service auth). Meters still running on
   `DEFAULT_CONFIG` don't appear — there's nothing in the table for
   them. `limit`/`offset` pagination, same pattern as everywhere else.
3. **`GET /admin/device-config/{meter_id}`** — fetch one meter's config
   for a dashboard's edit form to pre-fill. Falls back to
   `DEFAULT_CONFIG` (`is_default: true`) the same way the ESP32-facing
   endpoint does, rather than 404ing — opening "edit E999" for a
   never-configured meter should show sensible starting values, not an
   empty form.
4. **`PUT /admin/device-config/{meter_id}`** — the spec doc only
   describes ESP32 *reading* its config; it never says how one gets
   *set* in the first place. Without some way to write a row, every
   meter would read back the exact same hardcoded default forever. This
   endpoint upserts by `meter_id` (admin JWT only, overwrites every
   field — not a partial patch):
```bash
curl -X PUT http://localhost:3003/admin/device-config/E101 \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"schedule_mode":1,"date1":[26,0,0,8,0],"date2":[0,0,0,0,0],"photo_count":3,"photo_delay":5}'
```
5. **`DELETE /admin/device-config/{meter_id}`** — a dashboard "reset to
   default" button. Removes the row entirely (admin JWT only);
   idempotent — deleting an already-default meter is a no-op, not a
   404. After this, every GET above goes back to `DEFAULT_CONFIG` for
   that `meter_id`.
6. **`GET /admin/device-config-ui`** — a small standalone dashboard
   (`app/static/device_config_ui.html`, served as plain `HTMLResponse`
   — no template engine, no build step) so a human can browse/edit
   `device_config` from a browser instead of curl or Swagger. Own login
   form (posts to `/login`, stores the JWT in `localStorage`), a list
   pane of every configured meter (color-coded dot by type — amber
   electric, blue water, coral gas), and a detail pane that edits one
   meter's schedule and calls the four endpoints above. No FastAPI-side
   auth on the route itself — it's static HTML/CSS/JS, nothing to
   protect there; the page's own JS is what attaches the JWT to every
   `/admin/device-config*` call it makes, same as any other admin
   client. Every API call inside the page uses a relative URL (e.g.
   `fetch("device-config")`, `fetch("../login")`) resolved against the
   page's own path — works unmodified behind `BASE_PATH_PREFIX`
   (production) and without it (local), no hardcoded domain anywhere in
   the file. This is genuinely a from-scratch admin tool, not anything
   from the spec doc — flag if a different auth story (e.g. its own
   session cookie instead of reusing the JWT-in-localStorage pattern)
   is wanted instead.

`date1`/`date2` are stored as raw Postgres `INTEGER[]` (5 elements,
`CHECK`-constrained to exactly 5), matching the wire format exactly —
no attempt to normalize into real `DATE`/`TIME` columns, since
`schedule_mode=0`'s semantics (only Hour/Minute meaningful) don't map
cleanly onto them anyway. `schedule_mode`/`photo_count`/`photo_delay`
also have `CHECK` constraints matching the spec's valid ranges
(0-1, 1-10, 1-60) — enforced at the DB level, not just in the Pydantic
request model, so a bad value can never land in the table regardless of
how it got inserted.

## Things still worth double-checking

- **`get_admin_or_service()`** (admin-JWT-or-either-static-key, on the
  read-mostly `/admin/images*`/`/admin/meters/*` routes) — my own
  addition, not confirmed against your real code.
- **`meter_id` → table routing**: first letter `e/w/g` (case-insensitive),
  per your comment in `init.sql` and the "ประเภทมิเตอร์" spec page.
- **`error_type` codes 1 vs 2** ("found the meter but couldn't read the
  digits" vs "couldn't find any digits/meter at all") — the distinction
  is entirely the OCR client's own judgment call; nothing server-side
  tries to tell these apart.
- **`group_id` assignment timing**: happens when a group *opens* (first
  image of a burst), not when it's *finalized* into a job — so a burst
  that never completes still consumes a code from the sequence (a
  small, permanent gap, same as id gaps elsewhere in this system). Flag
  if you'd rather it only be assigned at finalization time.
- **Fast-path race window**: two uploads that both push a group's count
  to `IMAGE_GROUP_SIZE` at nearly the same instant are protected by
  `FOR UPDATE` on the anchor row plus a `NOT EXISTS` check right before
  inserting into `ocr_jobs` (`app/routers/images.py`) — same pattern as
  the sweep's own race-safety. Not independently tested against real
  concurrent load, just reasoned through.
