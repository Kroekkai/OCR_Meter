-- image-store — init.sql
-- --------------------------------------------------------------------------
-- Schema เดียว รันได้ปลอดภัยกับ DB ทุกสถานะ — fresh install, DB ที่เคยรัน
-- schema เก่า (จะถูกอัปเกรด/backfill ให้อัตโนมัติ), หรือ DB ที่อัปเดตครบ
-- แล้ว (รันซ้ำได้เฉยๆ) — ไม่มีไฟล์ migration แยกอีกต่อไป
--
-- image-store เก็บ 4 อย่าง:
--   1. users — ศูนย์กลาง auth เดียว ที่ meter-dashboard (และทุก service
--      อื่น) เชื่อถือ
--   2. images (ตารางเดียวรวมทุกประเภทมิเตอร์ — แยกด้วย utility_type
--      column, confirmed request) + ocr_jobs — ข้อมูล hardware capture +
--      internal job queue ของ OCR ล้วนๆ
--   3. ocr_meter — ผลลัพธ์ OCR ที่จบแล้ว (สำเร็จ/error) ตารางกลางสำหรับ
--      ส่งต่อให้ระบบภายนอกใช้ ไม่อ้างอิงกลับไปที่ 2 ข้อบนเลย
--   4. error_type — ตาราง lookup อธิบายความหมายของรหัส error_type แต่ละ
--      ตัว (0/1/2/3) ที่ใช้ใน ocr_meter — server เป็นคนกำหนดความหมาย OCR
--      client แค่ส่งตัวเลขกลับมา
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT        NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    is_admin      BOOLEAN     NOT NULL DEFAULT false,
    is_device     BOOLEAN     NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --- Unified images table (all meter types) --------------------------------
-- แยกตามตัวอักษรแรกของ meter_id ตอนอัปโหลด (E->electric, W->water,
-- G->gas) — meter_id เก็บเป็นตัวพิมพ์ใหญ่เสมอ (normalize ตอน parse ชื่อ
-- ไฟล์ — ดู app/filename.py)
--
-- ⚠️ confirmed request: รวม images_electric/water/gas เป็นตารางเดียว
-- (images) ใช้คอลัมน์ utility_type แยกประเภทแทนชื่อตาราง — แก้ปัญหาเดิม
-- ที่ "เพิ่มมิเตอร์ชนิดใหม่ (เช่น steam) ต้องเพิ่มตารางใหม่ + แก้โค้ดหลาย
-- จุดพร้อมกัน (sequence, mapping, sweep loop, UNION ALL query, FK, index
-- ฯลฯ)" — ตอนนี้เพิ่มมิเตอร์ชนิดใหม่แค่เพิ่มค่า utility_type ใหม่เข้า
-- CHECK constraint พอ ไม่ต้องแตะ schema/โค้ดจุดอื่นเลย
--
-- group_id / is_anchor / received_at: ESP32 ส่งภาพเป็นชุด (burst) หลายภาพ
-- ต่อการอ่าน 1 ครั้ง — server รวมภาพที่มาถึงจาก meter_id เดียวกันภายใน
-- หน้าต่างเวลาหนึ่ง (app.config.image_group_window_seconds) ให้เป็น
-- "กลุ่ม" เดียว ก่อนสร้าง ocr_jobs ให้ 1 job ต่อ 1 กลุ่ม — หรือทันทีถ้า
-- ภาพครบ image_group_size แล้ว (ไม่ต้องรอครบเวลา — ดู app/routers/images.py)
--
-- group_id (TEXT, เช่น "E1", "W3", "G12") คือรหัสกลุ่มที่มนุษย์อ่านง่าย —
-- นับแยกต่างหากต่อประเภทมิเตอร์ (sequence คนละตัวต่อ utility_type ยังคง
-- แยกกันอยู่ แม้ตอนนี้จะอยู่ในตารางเดียวกันแล้วก็ตาม — ตัวเลขจะได้ไม่
-- กระโดดข้ามประเภทแบบสับสน)
--
-- is_anchor (BOOLEAN): true = แถวนี้เป็นแถวแรกที่เปิดกลุ่มนี้ขึ้นมา (เก็บ
-- meter_id/original_filename/device_timestamp ที่จะก็อปเข้า ocr_jobs ตอน
-- ปิดกลุ่ม), false = แถวอื่นๆ ที่มาสมทบทีหลังในกลุ่มเดียวกัน กลไก
-- claim/sweep ที่กันการ race กันตอนสร้าง/ปิดกลุ่มล็อกที่แถว is_anchor=true
--
-- received_at คือเวลาที่ server ได้รับภาพจริง (ใช้วัด timeout ของกลุ่ม)
-- ต่างจาก device_timestamp ซึ่งเป็นเวลาที่ device อ้างว่าถ่าย
CREATE SEQUENCE IF NOT EXISTS images_id_seq;
CREATE SEQUENCE IF NOT EXISTS ocr_jobs_id_seq;
CREATE SEQUENCE IF NOT EXISTS electric_group_seq;
CREATE SEQUENCE IF NOT EXISTS water_group_seq;
CREATE SEQUENCE IF NOT EXISTS gas_group_seq;

-- dataset — NOT part of any original spec. Confirmed request, sketched
-- by hand: a central table consolidating the file path of every image
-- across all meter types, so "where is this image on disk" can be
-- answered from one table. Each images row gets its OWN dataset row
-- (1:1, not shared) — see the dataset_id column added to images below.
CREATE TABLE IF NOT EXISTS dataset (
    id   BIGSERIAL PRIMARY KEY,
    path TEXT      NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    id                BIGINT      PRIMARY KEY DEFAULT nextval('images_id_seq'),
    meter_id          TEXT        NOT NULL,
    -- utility_type — confirmed request: 'electric' | 'water' | 'gas',
    -- extend the CHECK below (and app/db.py's mapping) to add a new
    -- meter type — no schema change needed anywhere else.
    utility_type      TEXT        NOT NULL,
    original_filename TEXT,
    device_timestamp  TIMESTAMPTZ,
    ocr_status        TEXT        NOT NULL DEFAULT 'pending',  -- pending | done | failed | dropped
    group_id          TEXT        NOT NULL,
    is_anchor         BOOLEAN     NOT NULL DEFAULT false,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- dataset_id — confirmed request (hand-drawn diagram). Nullable:
    -- existing rows from before this column existed have no dataset
    -- row to point to and are left alone (never backfilled) — only
    -- new uploads going forward get one, via app/routers/images.py.
    dataset_id        BIGINT      REFERENCES dataset(id),
    -- job_id — confirmed request: a direct FK back to ocr_jobs, instead
    -- of only being joinable via group_id (which isn't unique — a group
    -- has multiple images — and isn't a real FK target). Nullable and
    -- deliberately has NO "REFERENCES ocr_jobs(id)" right here — ocr_jobs
    -- isn't created until further down this file, so a forward reference
    -- here would error immediately ("relation ocr_jobs does not exist").
    -- The FK itself is added near the end of this file instead (same
    -- pattern already used for device_config's FKs). Starts NULL on every
    -- insert (images always arrive before ocr_jobs can exist for them —
    -- see app/routers/images.py) and gets backfilled once the group's
    -- ocr_jobs row is actually created, in
    -- app/grouping.py::finalize_group(). A group that gets dropped
    -- instead (see the "one normal group per meter per day" rule further
    -- down) never gets an ocr_jobs row at all — its images' job_id stays
    -- NULL forever, which is correct, not a bug.
    job_id            BIGINT,
    CONSTRAINT images_utility_type_check CHECK (utility_type IN ('electric', 'water', 'gas'))
);

-- ⚠️ Migration รวมตาราง — confirmed request. ย้ายข้อมูลจาก
-- images_electric/water/gas (ตารางแยกจากเวอร์ชันก่อนหน้า) เข้า images
-- ตัวใหม่ด้านบน แล้ว DROP ตารางเดิมทิ้ง — no-op บน fresh install (ไม่มี 3
-- ตารางเดิมให้เจอเลย ตั้งแต่ต้น)
DO $$
DECLARE
    tbl TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'images_electric') THEN
        -- Legacy migration เก่ากว่านี้อีก (BIGINT group_id -> TEXT,
        -- is_test column removal) — ต้องรันให้ 3 ตารางเดิมอยู่ในสภาพ
        -- ล่าสุดก่อน ถึงจะย้ายข้อมูลออกมาได้ถูกต้อง เก็บไว้ตรงนี้เพื่อ
        -- ความปลอดภัย เผื่อ DB ไหนยังไม่เคยผ่าน migration เก่าเหล่านี้มา
        -- ก่อนเลย (เนื้อหาเดียวกับที่เคยอยู่ตรงนี้มาตลอด ย้ายเข้ามาไว้ใน
        -- IF block นี้แทน ให้รันเฉพาะตอนตารางเดิมยังอยู่จริงเท่านั้น)
        FOREACH tbl IN ARRAY ARRAY['images_electric', 'images_water', 'images_gas']
        LOOP
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = tbl AND column_name = 'group_id' AND data_type = 'bigint'
            ) THEN
                EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS is_anchor BOOLEAN', tbl);
                EXECUTE format('UPDATE %I SET is_anchor = (group_id = id) WHERE is_anchor IS NULL', tbl);
                EXECUTE format('ALTER TABLE %I DROP COLUMN group_id', tbl);
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = tbl AND column_name = 'group_label'
                ) THEN
                    EXECUTE format('ALTER TABLE %I RENAME COLUMN group_label TO group_id', tbl);
                ELSE
                    EXECUTE format('ALTER TABLE %I ADD COLUMN group_id TEXT', tbl);
                END IF;
            END IF;
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS is_anchor BOOLEAN', tbl);
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ', tbl);
            EXECUTE format('UPDATE %I SET received_at = COALESCE(device_timestamp, now()) WHERE received_at IS NULL', tbl);
            EXECUTE format('UPDATE %I SET is_anchor = false WHERE is_anchor IS NULL', tbl);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN is_anchor SET NOT NULL', tbl);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN is_anchor SET DEFAULT false', tbl);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN received_at SET NOT NULL', tbl);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN received_at SET DEFAULT now()', tbl);
            EXECUTE format('UPDATE %I SET meter_id = UPPER(meter_id) WHERE meter_id != UPPER(meter_id)', tbl);
            EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS is_test', tbl);
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS dataset_id BIGINT', tbl);
            EXECUTE format(
                'UPDATE %I SET group_id = %L || nextval(%L), is_anchor = true WHERE group_id IS NULL OR group_id = %L',
                tbl,
                CASE tbl WHEN 'images_electric' THEN 'E' WHEN 'images_water' THEN 'W' ELSE 'G' END,
                CASE tbl WHEN 'images_electric' THEN 'electric_group_seq' WHEN 'images_water' THEN 'water_group_seq' ELSE 'gas_group_seq' END,
                ''
            );
            EXECUTE format('ALTER TABLE %I ALTER COLUMN group_id SET NOT NULL', tbl);
        END LOOP;

        -- ย้ายข้อมูลจริงเข้า images ตัวใหม่ — ระบุ id ตรงๆ (ไม่ใช้
        -- nextval ใหม่) เพื่อรักษาค่า id เดิมไว้ทั้งหมด — ปลอดภัยเพราะทั้ง
        -- 3 ตารางเดิมแชร์ images_id_seq เดียวกันมาตั้งแต่ต้นอยู่แล้ว ไม่มี
        -- ทาง id ชนกัน ON CONFLICT DO NOTHING กันไว้เผื่อ migration นี้
        -- เคยรันไปแล้วบางส่วนมาก่อน (ตารางเดิมยังไม่ถูก DROP ด้วยเหตุผล
        -- บางอย่าง) ให้รันซ้ำได้ปลอดภัย
        INSERT INTO images (id, meter_id, utility_type, original_filename, device_timestamp, ocr_status, group_id, is_anchor, received_at, dataset_id)
            SELECT id, meter_id, 'electric', original_filename, device_timestamp, ocr_status, group_id, is_anchor, received_at, dataset_id FROM images_electric
            UNION ALL
            SELECT id, meter_id, 'water', original_filename, device_timestamp, ocr_status, group_id, is_anchor, received_at, dataset_id FROM images_water
            UNION ALL
            SELECT id, meter_id, 'gas', original_filename, device_timestamp, ocr_status, group_id, is_anchor, received_at, dataset_id FROM images_gas
        ON CONFLICT (id) DO NOTHING;

        DROP TABLE images_electric;
        DROP TABLE images_water;
        DROP TABLE images_gas;
    END IF;
END $$;

-- เผื่อ images มีอยู่แล้วจากรอบก่อนของ migration นี้เองที่ยังไม่มี job_id
-- (fresh install ไม่ต้องทำอะไรเพิ่ม เพราะ CREATE TABLE ด้านบนมีครบอยู่แล้ว)
ALTER TABLE images ADD COLUMN IF NOT EXISTS job_id BIGINT;

-- FK ไปหา dataset — DROP+ADD เสมอ (idempotent, ไม่พึ่ง ADD COLUMN อย่าง
-- เดียวเพราะรอบก่อนอาจเคยเพิ่ม column โดยไม่มี FK ติดมา — pattern เดียว
-- กับที่เคยใช้กับ error_type/ocr_engine)
ALTER TABLE images DROP CONSTRAINT IF EXISTS images_dataset_id_fkey;
ALTER TABLE images ADD CONSTRAINT images_dataset_id_fkey FOREIGN KEY (dataset_id) REFERENCES dataset(id);

CREATE INDEX IF NOT EXISTS idx_images_meter ON images (meter_id, device_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_images_group_lookup ON images (meter_id, received_at) WHERE is_anchor = true;
CREATE INDEX IF NOT EXISTS idx_images_group_id ON images (group_id);
CREATE INDEX IF NOT EXISTS idx_images_utility_type ON images (utility_type);
CREATE INDEX IF NOT EXISTS idx_images_job_id ON images (job_id);

-- meter_id/original_filename/device_timestamp ก็อปมาจากแถว "หัวกลุ่ม"
-- (denormalized) ให้เปิดตาราง ocr_jobs เฉยๆ แล้วรู้ครบระดับหนึ่ง ไม่ต้อง
-- join กลับไปที่ images_* เอง
--
-- group_id (TEXT, E1/W3/G12) คือรหัสกลุ่มเดียวกับที่อยู่ใน images_*
-- ตรงๆ — ไม่มี FK ตั้งใจ, ไม่ unique เพราะรูปเดียว reprocess ได้หลายรอบ
-- แต่ละรอบสร้างแถว job ใหม่ ไม่ทับของเดิม (เดิมคอลัมน์นี้เป็น BIGINT ชี้
-- id ของภาพหัวกลุ่ม คู่กับ group_label ที่เป็น E1/W3 แยกกัน — ตอนนี้รวม
-- เป็น TEXT เดียวคือ E1/W3/G12 ตรงๆ ไม่มี BIGINT คู่ขนานอีกต่อไป)
--
-- last_error / admin_reason: **ตัดออกตามที่ขอ** — ผลคือ /fail ไม่มีที่
-- เก็บเหตุผลความล้มเหลวแบบชั่วคราวอีกต่อไป (ยัง log ไว้ฝั่ง server เฉยๆ
-- ไม่ persist ลง DB) และ /ocr-manual ไม่มีที่บันทึกเหตุผลที่ admin แก้ค่า
-- เองอีกต่อไป — เสียความสามารถ debug/audit ตรงนี้ไปทั้งคู่ ถ้าอยากได้
-- กลับมาทีหลัง บอกได้
CREATE TABLE IF NOT EXISTS ocr_jobs (
    id                BIGINT      PRIMARY KEY DEFAULT nextval('ocr_jobs_id_seq'),
    group_id          TEXT        NOT NULL,
    meter_id          TEXT        NOT NULL,
    original_filename TEXT,
    device_timestamp  TIMESTAMPTZ,
    ocr_reading       NUMERIC,
    status            TEXT        NOT NULL DEFAULT 'queued',  -- queued | processing | done | failed | dropped
    attempts          BIGINT      NOT NULL DEFAULT 0
);

-- อัปเกรด DB ที่มี ocr_jobs อยู่แล้วจาก schema เก่ากว่า ให้ตรงกับ schema
-- ใหม่ — no-op บน fresh install (ตารางเพิ่งถูกสร้างครบด้านบนอยู่แล้ว)
DO $$
BEGIN
    -- ชื่อเก่าสุด (ก่อนรอบ group_label): image_id ชี้ id ของภาพหัวกลุ่มตรงๆ
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_jobs' AND column_name = 'image_id'
    ) THEN
        ALTER TABLE ocr_jobs RENAME COLUMN image_id TO group_id;
    END IF;
    -- ชื่อรอบก่อนหน้า: group_id เป็น BIGINT คู่กับ group_label เป็น TEXT
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_jobs' AND column_name = 'group_id' AND data_type = 'bigint'
    ) THEN
        ALTER TABLE ocr_jobs DROP COLUMN group_id;
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'ocr_jobs' AND column_name = 'group_label'
        ) THEN
            ALTER TABLE ocr_jobs RENAME COLUMN group_label TO group_id;
        ELSE
            ALTER TABLE ocr_jobs ADD COLUMN group_id TEXT NOT NULL DEFAULT '';
        END IF;
    END IF;
    ALTER TABLE ocr_jobs ADD COLUMN IF NOT EXISTS group_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE ocr_jobs DROP COLUMN IF EXISTS group_label;
    ALTER TABLE ocr_jobs DROP COLUMN IF EXISTS last_error;
    ALTER TABLE ocr_jobs DROP COLUMN IF EXISTS admin_reason;
    -- is_test เคยเป็นคอลัมน์แยกอยู่ช่วงสั้นๆ — ตัดออกตามที่ยืนยัน ดูจาก
    -- ชื่อไฟล์ (original_filename ที่ยังมีอยู่แล้ว) แทน — ดูคำอธิบายเต็ม
    -- ที่ migration ของ images_*/is_test ด้านบน
    ALTER TABLE ocr_jobs DROP COLUMN IF EXISTS is_test;
END $$;

-- ย้าย group_id ให้อยู่หน้า original_filename (ตามที่ยืนยัน) — Postgres
-- ไม่มีคำสั่ง "ย้ายคอลัมน์" ตรงๆ เลย (ต่างจาก RENAME/DROP ที่มีคำสั่ง
-- ตรงๆ ให้ใช้) ทางเดียวที่ทำได้จริงคือสร้างตารางใหม่ด้วยลำดับคอลัมน์ที่
-- ต้องการ ย้ายข้อมูลเข้าไป แล้วสลับตารางเก่ากับใหม่ — DB ที่ผ่านการ
-- migrate มาหลายรอบ (image_id -> group_id -> group_label -> group_id
-- อีกที) มักจบด้วย group_id ไปอยู่ท้ายตาราง (เพราะ ALTER TABLE ADD
-- COLUMN ต่อท้ายเสมอ) ไม่ใช่อยู่หน้า meter_id/original_filename แบบที่
-- CREATE TABLE ด้านบนกำหนดไว้ตอน fresh install — เช็คก่อนว่าลำดับตอนนี้
-- ผิดจริงไหม (no-op ถ้าตรงอยู่แล้ว ปลอดภัยรันซ้ำได้)
DO $$
DECLARE
    correct_order TEXT[] := ARRAY['id','group_id','meter_id','original_filename','device_timestamp','ocr_reading','status','attempts'];
    actual_order TEXT[];
BEGIN
    SELECT array_agg(column_name ORDER BY ordinal_position) INTO actual_order
    FROM information_schema.columns WHERE table_name = 'ocr_jobs';

    IF actual_order IS DISTINCT FROM correct_order THEN
        CREATE TABLE ocr_jobs_reordered (
            id                BIGINT      PRIMARY KEY,
            group_id          TEXT        NOT NULL,
            meter_id          TEXT        NOT NULL,
            original_filename TEXT,
            device_timestamp  TIMESTAMPTZ,
            ocr_reading       NUMERIC,
            status            TEXT        NOT NULL DEFAULT 'queued',
            attempts          BIGINT      NOT NULL DEFAULT 0
        );
        INSERT INTO ocr_jobs_reordered (id, group_id, meter_id, original_filename, device_timestamp, ocr_reading, status, attempts)
            SELECT id, group_id, meter_id, original_filename, device_timestamp, ocr_reading, status, attempts
            FROM ocr_jobs
            ORDER BY id;
        DROP TABLE ocr_jobs;
        ALTER TABLE ocr_jobs_reordered RENAME TO ocr_jobs;
        ALTER TABLE ocr_jobs ALTER COLUMN id SET DEFAULT nextval('ocr_jobs_id_seq');
        ALTER TABLE ocr_jobs RENAME CONSTRAINT ocr_jobs_reordered_pkey TO ocr_jobs_pkey;
    END IF;
END $$;

-- เผื่อเคยรัน reorder migration ด้านบนไปแล้วรอบก่อน (ตอนนั้นยังไม่มี
-- RENAME CONSTRAINT บรรทัดนี้) — constraint ยังค้างชื่อ
-- ocr_jobs_reordered_pkey อยู่ ทั้งที่ตารางชื่อ ocr_jobs ไปแล้ว (RENAME
-- TABLE ไม่ rename ชื่อ constraint ตามให้อัตโนมัติ) แก้แยกเป็น
-- idempotent block ของตัวเอง เช็คว่าชื่อเก่ายังอยู่ก่อนค่อย rename กัน
-- error ตอนรันซ้ำ (คนละสถานการณ์กับ IF ด้านบนที่เช็คแค่ตอน column
-- ยังไม่ถูกจัดเรียง — เคสนี้จัดเรียงไปแล้ว เหลือแค่ชื่อ constraint ที่ยังไม่ตรง)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ocr_jobs_reordered_pkey') THEN
        ALTER TABLE ocr_jobs RENAME CONSTRAINT ocr_jobs_reordered_pkey TO ocr_jobs_pkey;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ocr_jobs_group_id ON ocr_jobs (group_id);

-- error_type — ตาราง lookup อธิบายความหมายของรหัส error_type ที่ใช้ใน
-- ocr_meter เก็บไว้ที่นี่ที่เดียว (single source of truth) ไม่กระจายไป
-- เขียนซ้ำเป็น comment หลายที่ — server เป็นคนกำหนดว่าแต่ละรหัสหมายถึง
-- อะไร, OCR client แค่ส่งตัวเลข (0/1/2/3) กลับมาบอกว่าเจอ case ไหน
CREATE TABLE IF NOT EXISTS error_type (
    code         INTEGER PRIMARY KEY,
    error_detail TEXT    NOT NULL
);
INSERT INTO error_type (code, error_detail) VALUES
    (0, 'อ่าน OCR สำเร็จ'),
    (1, 'อ่านเลขมิเตอร์ไม่ได้ (เจอมิเตอร์แต่ตัวเลขไม่ชัด/อ่านค่าไม่ได้)'),
    (2, 'หาตัวเลข/มิเตอร์ไม่เจอในภาพเลย'),
    (3, 'อ่านได้ค่า แต่ผิดปกติ (ลดลงจากเดือนก่อน หรือใช้เกินอัตราปกติมาก — OCR client เป็นคนเช็คเอง ดู README)')
ON CONFLICT (code) DO NOTHING;

-- ocr_engine — ตาราง lookup แบบเดียวกับ error_type ด้านบน (ยืนยันตาม
-- สเปกจากทีม Worker) บอกว่า OCR แต่ละครั้งอ่านผ่านโมเดลไหน — 1=LOCAL
-- (YOLO+CNN ในเครื่อง Worker เอง ไม่มีค่าใช้จ่าย), 2=GEMINI (fallback
-- ไปเรียก Gemini 3.7 Flash ตอนโมเดลในเครื่องอ่านไม่ผ่าน) — เก็บไว้ทำสถิติ
-- ว่าภาพกี่ % ต้องพึ่ง cloud AI เท่านั้น ไม่มีผลต่อ error_type/ocr_reading
-- หรือ logic อื่นในระบบเลย
CREATE TABLE IF NOT EXISTS ocr_engine (
    code INT         PRIMARY KEY,
    name VARCHAR(50) NOT NULL
);
INSERT INTO ocr_engine (code, name) VALUES
    (1, 'LOCAL (YOLO+CNN)'),
    (2, 'GEMINI (Cloud Fallback)')
ON CONFLICT (code) DO NOTHING;

-- ocr_meter — ผลลัพธ์ OCR ที่ "จบแล้ว" ของแต่ละมิเตอร์ (สำเร็จ/error) —
-- ตารางกลางสำหรับส่งต่อให้ระบบภายนอก (External Store) ใช้ ไม่มี FK
-- อ้างอิงกลับไปที่ images_*/ocr_jobs เลยตั้งใจ — อ่านตารางนี้เฉยๆ ก็รู้
-- เรื่องครบ ไม่ต้อง join กลับไปที่ไหนอีก
--
-- ต่างจาก ocr_jobs ตรงนี้: ocr_jobs คือ internal job queue ล้วนๆ — ความ
-- ล้มเหลวแบบชั่วคราว/retry ได้ (network, OCR_API_URL ไม่ถูกตั้งค่า ฯลฯ)
-- ผ่าน /fail เหมือนเดิม แต่ไม่มีที่เก็บเหตุผลลง DB อีกต่อไป (log ฝั่ง
-- server เฉยๆ — ดู comment ที่ ocr_jobs ด้านบนเรื่อง last_error ถูกตัดออก)
-- ไม่มาสร้างแถวที่นี่ — ocr_meter มีแถวก็ต่อเมื่อ OCR "จบงาน" แล้วเท่านั้น
-- (ผ่าน /result)
--
-- error_type (INTEGER, NOT NULL เสมอ — ทุกการส่งผลต้องระบุมาชัดเจน):
--   0 = อ่านสำเร็จ (ocr_reading ต้องมีค่า)
--   1 = อ่านเลขมิเตอร์ไม่ได้ (ocr_reading เป็น NULL)
--   2 = หาตัวเลข/มิเตอร์ไม่เจอเลย (ocr_reading เป็น NULL)
--   3 = อ่านได้ค่า แต่ผิดปกติ (ocr_reading ต้องมีค่า) — รวม
--       reading_decreased (ค่าลดลงจากเดือนก่อน) และ usage_anomaly (ใช้
--       เกินอัตราปกติมาก) จากดีไซน์เดิมเป็น case เดียว — OCR client เป็น
--       คนดึง history เอง (GET .../ocr-readings) เช็คเอง แล้วส่ง 3 กลับมา
--       ถ้าเข้าเงื่อนไขข้อใดข้อหนึ่ง — server ไม่คำนวณให้
-- ดูคำอธิบายเต็มที่ตาราง error_type ด้านบน — OCR client ส่งแค่ตัวเลขนี้
-- กลับมา ไม่ต้องรู้ความหมายเอง
--
-- capture_date/capture_time: เวลาที่ ESP32 **ถ่ายภาพ** (มาจาก
-- ocr_jobs.device_timestamp ของ job นั้น) ไม่ใช่เวลาที่ OCR ประมวลผล —
-- server เป็นคนเติมให้เองจาก device_timestamp ไม่ใช่ค่าที่ OCR client ส่งมา
-- (ชื่อเดิมคือ reading_date/reading_time — เปลี่ยนชื่อให้สื่อความหมาย
-- ตรงขึ้นว่าเป็นเวลาที่ "ถ่ายภาพ" ไม่ใช่เวลาที่ "อ่านค่า/ประมวลผล")
--
-- image_error: ใส่เฉพาะตอน error_type != 0 เท่านั้น (1, 2, หรือ 3
-- — ไม่ใส่ตอนสำเร็จเปล่าๆ error_type=0) เป็น**ชื่อไฟล์เดียวกับที่หัวกลุ่ม
-- ถูกอัปโหลดไว้แล้วตรงๆ** (ไม่ใช่ไฟล์แยกที่ OCR อัปโหลดซ้ำมาใหม่ — เดิม
-- เคยรับ multipart แนบไฟล์ใหม่ แต่ยกเลิกไปแล้ว เพราะ OCR client ไม่มี
-- ภาพอื่นนอกจากภาพที่ ESP32 ส่งมาอยู่แล้วตั้งแต่ต้น การให้อัปโหลดซ้ำมีแต่
-- เสี่ยงชื่อไฟล์ชนกับภาพอื่นในกลุ่มเอง ไม่มีประโยชน์อะไรเพิ่ม) แค่ชี้กลับ
-- ไปที่ไฟล์ที่มีอยู่แล้วในเครื่อง ให้คนอ่านตรวจสอบตอนเกิด error/ผิดปกติ
-- (ชื่อคอลัมน์เดิมคือ ocr_image_filename — เปลี่ยนเป็น image_error ให้
-- สื่อความหมายตรงขึ้น เพราะมีค่าเฉพาะตอนเกิด error เท่านั้น) — column
-- นี้ตั้งใจให้อยู่**หลัง** error_type เสมอ (ลำดับคอลัมน์ที่เห็นตอน
-- SELECT * — ดู DO block ท้าย section นี้ที่จัดลำดับให้ ถ้า DB เดิมมี
-- image_error อยู่ก่อน error_type จากการ migrate มาหลายรอบ)
--
-- ตารางนี้ตั้งใจให้มีแค่ 6 field ตามที่ยืนยัน (meter_id, capture_date,
-- capture_time, ocr_reading, error_type, image_error) — ไม่มี group_id
-- ในตารางนี้แล้ว (เคยมีอยู่ช่วงสั้นๆ ตอนรวม column กับ ocr_jobs แต่ตัด
-- ออกตามที่ขอ — group_id ยังใช้เป็นกลไกภายในต่อใน images_*/ocr_jobs
-- ตามเดิม แค่ไม่ก็อปมาใส่ตารางผลลัพธ์นี้อีกต่อไป)
CREATE TABLE IF NOT EXISTS ocr_meter (
    id                  BIGSERIAL   PRIMARY KEY,
    meter_id            TEXT        NOT NULL,
    capture_date        DATE        NOT NULL,
    capture_time        TIME        NOT NULL,
    ocr_reading         NUMERIC,
    error_type          INTEGER     NOT NULL REFERENCES error_type(code),
    image_error         TEXT,
    -- ocr_engine — ยืนยันตามสเปกทีม Worker: 1=LOCAL (default, กรณี
    -- ไม่ได้ระบุมาจากงานเก่าก่อนฟีเจอร์นี้), 2=GEMINI — DEFAULT 1 ที่
    -- DB ชั้นนี้เป็น safety net ชั้นสุดท้ายเท่านั้น (endpoint
    -- /result และ /result-test เป็นคนตั้ง default ให้จริงๆ ถ้า
    -- Worker ไม่ส่งมา — ดู app/routers/ocr_jobs.py)
    ocr_engine          INT         REFERENCES ocr_engine(code) DEFAULT 1
);

-- อัปเกรด DB ที่มี ocr_meter อยู่แล้วจาก schema เก่า (error_type เป็น TEXT,
-- มี error_detail, มี group_id หรือ group_label ที่ตัดออกไปแล้ว, หรือมี
-- reading_timestamp เดียวจากรอบทดลองสั้นๆ ที่ยกเลิกไปแล้ว, หรือคอลัมน์
-- ชื่อ ocr_image_filename แทน image_error) ให้ตรงกับ schema ใหม่ — no-op
-- บน fresh install
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'ocr_image_filename'
    ) THEN
        ALTER TABLE ocr_meter RENAME COLUMN ocr_image_filename TO image_error;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'error_type' AND data_type = 'text'
    ) THEN
        ALTER TABLE ocr_meter ADD COLUMN error_type_new INTEGER;
        -- แปลงค่าเก่า -> รหัสใหม่: reading_decreased/usage_anomaly ตอนนี้
        -- มีบ้านเป็น case 3 แล้ว (อ่านได้ค่าแต่ผิดปกติ) — ไม่ต้อง fold
        -- เป็น 0 (สำเร็จเฉยๆ) แบบรอบก่อนอีกต่อไป
        UPDATE ocr_meter SET error_type_new = CASE
            WHEN error_type IS NULL THEN 0
            WHEN error_type = 'image_unreadable' THEN 1
            WHEN error_type = 'no_digits_found' THEN 2
            WHEN error_type IN ('reading_decreased', 'usage_anomaly') THEN 3
            ELSE 0
        END;
        ALTER TABLE ocr_meter DROP COLUMN error_type;
        ALTER TABLE ocr_meter RENAME COLUMN error_type_new TO error_type;
        ALTER TABLE ocr_meter ALTER COLUMN error_type SET NOT NULL;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'error_detail'
    ) THEN
        ALTER TABLE ocr_meter DROP COLUMN error_detail;
    END IF;
    -- group_id/group_label: ตัดออกจากตารางนี้ตามที่ยืนยัน — ไม่ก็อป
    -- group_id เข้ามาที่ ocr_meter อีกต่อไป (ยังอยู่ใน images_*/ocr_jobs
    -- เหมือนเดิม แค่ไม่ไหลมาถึงตารางผลลัพธ์นี้)
    ALTER TABLE ocr_meter DROP COLUMN IF EXISTS group_id;
    ALTER TABLE ocr_meter DROP COLUMN IF EXISTS group_label;
    -- reading_date/reading_time -> capture_date/capture_time (เปลี่ยนชื่อ
    -- ให้สื่อความหมายตรงขึ้นว่าเป็นเวลาที่ถ่ายภาพ)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'reading_date'
    ) THEN
        ALTER TABLE ocr_meter RENAME COLUMN reading_date TO capture_date;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'reading_time'
    ) THEN
        ALTER TABLE ocr_meter RENAME COLUMN reading_time TO capture_time;
    END IF;
    -- reading_timestamp เดียว (TIMESTAMPTZ) จากรอบทดลองสั้นๆ ที่ยกเลิก
    -- ไปแล้ว -> แยกกลับเป็น capture_date + capture_time เหมือนเดิม — แปลง
    -- กลับเป็นเวลาไทย (Bangkok, UTC+7) local ก่อนแยก เพราะ TIMESTAMPTZ
    -- เก็บเป็น UTC ภายใน ถ้าแยกตรงๆ โดยไม่แปลงโซนก่อน วันที่/เวลาจะเพี้ยน
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'reading_timestamp'
    ) THEN
        ALTER TABLE ocr_meter ADD COLUMN IF NOT EXISTS capture_date DATE;
        ALTER TABLE ocr_meter ADD COLUMN IF NOT EXISTS capture_time TIME;
        UPDATE ocr_meter
        SET capture_date = (reading_timestamp AT TIME ZONE 'Asia/Bangkok')::DATE,
            capture_time = (reading_timestamp AT TIME ZONE 'Asia/Bangkok')::TIME
        WHERE capture_date IS NULL;
        ALTER TABLE ocr_meter DROP COLUMN reading_timestamp;
        ALTER TABLE ocr_meter ALTER COLUMN capture_date SET NOT NULL;
        ALTER TABLE ocr_meter ALTER COLUMN capture_time SET NOT NULL;
    END IF;
    -- ocr_engine — ยืนยันตามสเปกทีม Worker (ทำแบบเดียวกับ error_type
    -- ด้านบน: 1=LOCAL, 2=GEMINI) — no-op บน fresh install เพราะ
    -- CREATE TABLE ด้านบนมีคอลัมน์นี้ครบตั้งแต่ต้นอยู่แล้ว
    ALTER TABLE ocr_meter ADD COLUMN IF NOT EXISTS ocr_engine INT REFERENCES ocr_engine(code) DEFAULT 1;
END $$;

-- จัดลำดับคอลัมน์ให้ตรงกับ CREATE TABLE ด้านบนเป๊ะ (error_type ต้องมา
-- ก่อน image_error เสมอ) — ใช้วิธีเดียวกับ ocr_jobs ด้านบน (สร้างตาราง
-- ใหม่ตามลำดับที่ต้องการ ย้ายข้อมูล แล้วสลับตาราง) เพราะรับประกันลำดับ
-- ที่ถูกต้องแน่นอน ต่างจากการ drop+recreate ทีละคอลัมน์ที่แค่ "ค่อนข้าง
-- ถูก" — no-op ถ้าลำดับตรงอยู่แล้ว ปลอดภัยรันซ้ำได้
DO $$
DECLARE
    correct_order TEXT[] := ARRAY['id','meter_id','capture_date','capture_time','ocr_reading','error_type','image_error','ocr_engine'];
    actual_order TEXT[];
BEGIN
    SELECT array_agg(column_name ORDER BY ordinal_position) INTO actual_order
    FROM information_schema.columns WHERE table_name = 'ocr_meter';

    IF actual_order IS DISTINCT FROM correct_order THEN
        -- ocr_meter.id เดิมสร้างด้วย BIGSERIAL ตอน fresh install — Postgres
        -- ผูก ocr_meter_id_seq ให้เป็นของ (OWNED BY) column นี้โดยอัตโนมัติ
        -- ถ้าไม่ตัดความเป็นเจ้าของออกก่อน ตอน DROP TABLE ocr_meter ด้านล่าง
        -- Postgres จะพยายามลบ sequence ตามไปด้วย (เพราะเป็นเจ้าของ) แต่ลบ
        -- ไม่ได้เพราะ ocr_meter_reordered ที่เพิ่งสร้างก็อ้างอิง sequence
        -- เดียวกันอยู่ — ชนกัน error "cannot drop table ... other objects
        -- depend on it" (เจอจริงตอน deploy) แก้โดยตัดความเป็นเจ้าของออก
        -- ก่อน ให้ sequence ลอยอิสระ ไม่ผูกกับตารางไหนจนกว่าจะผูกใหม่ด้านล่าง
        ALTER SEQUENCE ocr_meter_id_seq OWNED BY NONE;

        CREATE TABLE ocr_meter_reordered (
            id            BIGINT      PRIMARY KEY DEFAULT nextval('ocr_meter_id_seq'),
            meter_id      TEXT        NOT NULL,
            capture_date  DATE        NOT NULL,
            capture_time  TIME        NOT NULL,
            ocr_reading   NUMERIC,
            error_type    INTEGER     NOT NULL REFERENCES error_type(code),
            image_error   TEXT,
            ocr_engine    INT         REFERENCES ocr_engine(code) DEFAULT 1
        );
        INSERT INTO ocr_meter_reordered (id, meter_id, capture_date, capture_time, ocr_reading, error_type, image_error, ocr_engine)
            SELECT id, meter_id, capture_date, capture_time, ocr_reading, error_type, image_error, ocr_engine
            FROM ocr_meter
            ORDER BY id;
        DROP TABLE ocr_meter;
        ALTER TABLE ocr_meter_reordered RENAME TO ocr_meter;
        ALTER TABLE ocr_meter RENAME CONSTRAINT ocr_meter_reordered_pkey TO ocr_meter_pkey;
        ALTER TABLE ocr_meter RENAME CONSTRAINT ocr_meter_reordered_error_type_fkey TO ocr_meter_error_type_fkey;
        -- ocr_engine ได้ FK constraint ชื่ออัตโนมัติจาก Postgres ตอน
        -- CREATE TABLE ด้านบนเหมือนกัน (คอลัมน์ที่มี REFERENCES ในนิยาม
        -- ได้ constraint เสมอ ไม่ขึ้นกับว่า nullable หรือไม่)
        ALTER TABLE ocr_meter RENAME CONSTRAINT ocr_meter_reordered_ocr_engine_fkey TO ocr_meter_ocr_engine_fkey;
        -- ผูก sequence กลับเข้ากับ column ใหม่ให้เรียบร้อย (ไม่จำเป็นต่อการ
        -- ทำงาน แค่ให้ Postgres จัดการ sequence ให้อัตโนมัติเวลา DROP TABLE
        -- ในอนาคต เหมือนตอนที่เป็น BIGSERIAL แต่แรก)
        ALTER SEQUENCE ocr_meter_id_seq OWNED BY ocr_meter.id;
    END IF;
END $$;


ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_error_type_check;
ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_error_type_fkey;
ALTER TABLE ocr_meter ADD CONSTRAINT ocr_meter_error_type_fkey FOREIGN KEY (error_type) REFERENCES error_type(code);
ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_ocr_engine_fkey;
ALTER TABLE ocr_meter ADD CONSTRAINT ocr_meter_ocr_engine_fkey FOREIGN KEY (ocr_engine) REFERENCES ocr_engine(code);

-- capture_date DESC, capture_time DESC รองรับ query แบบที่ OCR client
-- ต้องใช้บ่อยที่สุด: "ค่าล่าสุดของมิเตอร์นี้คือเท่าไหร่" — DROP ก่อนเผื่อ
-- ยังมี index ชื่อเดิมค้างจาก definition ที่ต่างออกไป
DROP INDEX IF EXISTS idx_ocr_meter_meter_id;
CREATE INDEX IF NOT EXISTS idx_ocr_meter_meter_id ON ocr_meter (meter_id, capture_date DESC, capture_time DESC);

-- ocr_meter_test — โครงสร้างเหมือน ocr_meter เป๊ะทุกคอลัมน์ แค่แยกตาราง
-- เก็บผลจากภาพที่ server ตัดสินว่า "ไม่ตรงตารางเวลาใน device_config"
-- (ชื่อไฟล์ของ job นั้นมี "_Test" ต่อท้าย — ดู
-- app/filename.py::is_test_filename()) เท่านั้น — ผลจากภาพที่ถ่ายตรง
-- ตามตารางเวลาจริงยังคงลงที่ ocr_meter ตามปกติ ไม่มายุ่งกับตารางนี้เลย
-- ไม่มี FK เชื่อมกับ ocr_meter เลย เป็นคนละตารางแยกขาดจากกันสนิท
CREATE TABLE IF NOT EXISTS ocr_meter_test (
    id                  BIGSERIAL   PRIMARY KEY,
    meter_id            TEXT        NOT NULL,
    capture_date        DATE        NOT NULL,
    capture_time        TIME        NOT NULL,
    ocr_reading         NUMERIC,
    error_type          INTEGER     NOT NULL REFERENCES error_type(code),
    image_error         TEXT,
    ocr_engine          INT         REFERENCES ocr_engine(code) DEFAULT 1
);
-- ยืนยันตามสเปกทีม Worker: "และตารางtest ถ้ามีแยกตารางครับ" — มีจริง
-- (ocr_meter_test) เพิ่มคอลัมน์เดียวกันให้ครบ ไม่มี reorder-migration
-- ที่ซับซ้อนแบบ ocr_meter (ตารางนี้ไม่เคยมีปัญหาลำดับคอลัมน์ผิดมาก่อน)
-- แค่ ADD COLUMN ตรงๆ ก็พอ
ALTER TABLE ocr_meter_test ADD COLUMN IF NOT EXISTS ocr_engine INT REFERENCES ocr_engine(code) DEFAULT 1;
-- anchor_image_path was briefly a column here (confirmed request,
-- reverted) — the dashboard's test-results image instead comes from a
-- query-time JOIN against images_*/is_anchor=true, matched on
-- meter_id + device_timestamp (which capture_date/capture_time were
-- themselves derived from) — see
-- app/routers/meters.py::_list_ocr_meter_rows(). No schema change
-- needed for this at all; if a stray anchor_image_path column exists
-- from that earlier version, drop it.
ALTER TABLE ocr_meter_test DROP COLUMN IF EXISTS anchor_image_path;
CREATE INDEX IF NOT EXISTS idx_ocr_meter_test_meter_id ON ocr_meter_test (meter_id, capture_date DESC, capture_time DESC);

-- --------------------------------------------------------------------------
-- device_config — NOT part of the original confirmed spec. Added from a
-- separate ESP32 "device configuration" API spec doc another team sent
-- (GET /devices/config?meter_id=...) — see app/routers/device_config.py
-- for the full explanation, including the gap this leaves open (no
-- documented way for an admin to actually SET a meter's config, so the
-- companion admin endpoint here is also my own addition, not in that spec).
--
-- date1/date2 stored as raw INTEGER[5] matching the wire format exactly
-- ([Day, Month, Year, Hour, Minute]) — no attempt made to normalize this
-- into real DATE/TIME columns, since the spec's own semantics don't map
-- cleanly onto them (schedule_mode=0 uses only Hour/Minute and zeroes
-- for Day/Month/Year; Postgres arrays round-trip through asyncpg as
-- plain Python lists with no extra work, which is all this needs).
CREATE TABLE IF NOT EXISTS device_config (
    meter_id      TEXT      PRIMARY KEY,
    schedule_mode INTEGER   NOT NULL DEFAULT 1 CHECK (schedule_mode IN (0, 1)),
    date1         INTEGER[] NOT NULL DEFAULT ARRAY[26,0,0,8,0] CHECK (array_length(date1, 1) = 5),
    date2         INTEGER[] NOT NULL DEFAULT ARRAY[0,0,0,0,0]  CHECK (array_length(date2, 1) = 5),
    photo_count   INTEGER   NOT NULL DEFAULT 3 CHECK (photo_count BETWEEN 1 AND 10),
    photo_delay   INTEGER   NOT NULL DEFAULT 5 CHECK (photo_delay BETWEEN 1 AND 60),
    -- is_default — confirmed request, round 2. This column has now been
    -- added, removed, and re-added — the CONCEPT (distinguish "nobody's
    -- ever touched this meter's schedule" from "an admin deliberately
    -- set it") stayed wanted the whole time; only the REASON for
    -- needing a real column changed. First time: needed because a row
    -- got auto-created to satisfy a (since-reverted) FK from other
    -- tables — see "device_config stands alone" further below for that
    -- whole story. This time: no FK involved at all — a row now gets
    -- auto-created (app/routers/device_config.py::get_or_create_device_config())
    -- purely so device_config has a real record of every meter_id
    -- that's ever been seen, confirmed request. true = still running
    -- on auto-provisioned defaults, nobody has explicitly set this
    -- meter's schedule; false = an admin used
    -- PUT /admin/device-config/{meter_id} at least once.
    is_default    BOOLEAN   NOT NULL DEFAULT true
);

-- เผื่อ device_config มีอยู่แล้วจากรอบก่อนที่ยังไม่มี CHECK constraint —
-- เพิ่มให้ครบ (DROP ก่อนกัน error "constraint already exists" ถ้าเคย
-- เพิ่มไปแล้วบางส่วน) no-op บน fresh install เพราะ CREATE TABLE ด้านบน
-- มี constraint ครบตั้งแต่ต้นอยู่แล้ว
ALTER TABLE device_config DROP CONSTRAINT IF EXISTS device_config_schedule_mode_check;
ALTER TABLE device_config ADD CONSTRAINT device_config_schedule_mode_check CHECK (schedule_mode IN (0, 1));
ALTER TABLE device_config DROP CONSTRAINT IF EXISTS device_config_date1_check;
ALTER TABLE device_config ADD CONSTRAINT device_config_date1_check CHECK (array_length(date1, 1) = 5);
ALTER TABLE device_config DROP CONSTRAINT IF EXISTS device_config_date2_check;
ALTER TABLE device_config ADD CONSTRAINT device_config_date2_check CHECK (array_length(date2, 1) = 5);
ALTER TABLE device_config DROP CONSTRAINT IF EXISTS device_config_photo_count_check;
ALTER TABLE device_config ADD CONSTRAINT device_config_photo_count_check CHECK (photo_count BETWEEN 1 AND 10);
ALTER TABLE device_config DROP CONSTRAINT IF EXISTS device_config_photo_delay_check;
ALTER TABLE device_config ADD CONSTRAINT device_config_photo_delay_check CHECK (photo_delay BETWEEN 1 AND 60);
-- เผื่อ DB นี้เคยผ่านสถานะ "มี is_default" มาก่อน (ไม่ว่าจะจากรอบ FK
-- เดิม หรือยังไม่เคยมีเลย) ADD COLUMN IF NOT EXISTS ครอบคลุมทั้ง 2 กรณี
-- ให้ผลเหมือนกัน — no-op บน fresh install เพราะ CREATE TABLE มีคอลัมน์
-- นี้อยู่แล้ว
ALTER TABLE device_config ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT true;

-- ⚠️ FK ไปหา device_config — ยืนยันแล้วรอบที่ 3 (ยอมรับผลที่ตามมา:
-- DELETE /admin/device-config/{meter_id} จะลบจริงไม่ได้อีกต่อไป
-- สำหรับมิเตอร์ที่มีประวัติในตารางเหล่านี้ — endpoint เปลี่ยนเป็น
-- upsert กลับเป็นค่า default แทน ดู app/routers/device_config.py)
-- ต้องอยู่ "หลัง" CREATE TABLE device_config ด้านบนเสมอ (ไม่ใช่แค่ตอน
-- CREATE TABLE images/ocr_jobs/ocr_meter/ฯลฯ ตอนต้นไฟล์ เพราะ
-- ตอนนั้น device_config ยังไม่ถูกสร้างเลย — จะ error "relation
-- device_config does not exist" ทันที) ใช้ DROP+ADD CONSTRAINT แยกเป็น
-- statement ของตัวเอง (pattern เดียวกับ error_type/ocr_engine/dataset
-- ก่อนหน้า) เพื่อเลี่ยงปัญหา ordering นี้โดยสิ้นเชิง
ALTER TABLE images DROP CONSTRAINT IF EXISTS images_meter_id_fkey;
ALTER TABLE images ADD CONSTRAINT images_meter_id_fkey FOREIGN KEY (meter_id) REFERENCES device_config(meter_id);
ALTER TABLE ocr_jobs DROP CONSTRAINT IF EXISTS ocr_jobs_meter_id_fkey;
ALTER TABLE ocr_jobs ADD CONSTRAINT ocr_jobs_meter_id_fkey FOREIGN KEY (meter_id) REFERENCES device_config(meter_id);
ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_meter_id_fkey;
ALTER TABLE ocr_meter ADD CONSTRAINT ocr_meter_meter_id_fkey FOREIGN KEY (meter_id) REFERENCES device_config(meter_id);
ALTER TABLE ocr_meter_test DROP CONSTRAINT IF EXISTS ocr_meter_test_meter_id_fkey;
ALTER TABLE ocr_meter_test ADD CONSTRAINT ocr_meter_test_meter_id_fkey FOREIGN KEY (meter_id) REFERENCES device_config(meter_id);
-- esp32_upload_log's meter_id FK is added near the end of this file
-- instead, right after that table's own reorder migration — confirmed
-- bug found in production: adding it here (before the reorder) meant
-- the reorder's DROP TABLE esp32_upload_log wiped this FK out again a
-- few statements later, since the reordered CREATE TABLE never
-- declared REFERENCES device_config(meter_id) itself.

-- --------------------------------------------------------------------------
-- esp32_upload_log — NOT part of either original spec. Added for
-- Project Carbon (a later ESP32 firmware update, confirmed) — the
-- device now sends 3 metadata values as URL query params alongside
-- every POST /images/upload: net_mode ("4G"/"WiFi"), carrier (mobile
-- carrier name on 4G, "-" on WiFi), wakeup_reason ("timer"/"manual").
-- wakeup_reason is also what decides is_test now (see
-- app/routers/images.py — replaced the old device_config schedule
-- comparison, app/schedule_match.py, since deleted) — this table is
-- purely an observability log of what the device reported, with no
-- bearing on grouping/OCR/results itself.
--
-- Confirmed: ONE ROW PER GROUP (burst), not one row per image — all
-- images in a burst share the same wake-up event, so the 3 values are
-- identical across them; logging per-image would just be 3x redundant
-- rows. Only inserted in the "open new group" branch of the upload
-- handler, same moment is_test gets decided for that group.
--
-- Column names net_mode/carrier/wakeup_reason — confirmed request
-- (an earlier version briefly used data1/data2/data3 — renamed, see
-- the migration block below for DBs that ran that version). All TEXT,
-- all nullable (older firmware that hasn't been updated yet sends
-- none of the 3 query params at all).
CREATE TABLE IF NOT EXISTS esp32_upload_log (
    id            BIGSERIAL PRIMARY KEY,
    log_date      DATE      NOT NULL,  -- device_timestamp's Bangkok-local date (not received_at) — matches capture_date elsewhere
    -- log_time — confirmed request, added later. Same Bangkok-local
    -- device_timestamp as log_date, just the time-of-day component —
    -- previously only the date was kept, meaning multiple bursts from
    -- the same meter on the same day were indistinguishable by time
    -- alone in this table. Confirmed: must sit directly after
    -- log_date, not at the end of the table.
    log_time      TIME      NOT NULL,
    meter_id      TEXT      NOT NULL,
    net_mode      TEXT,                -- "4G" | "WiFi" | null
    -- carrier — confirmed request, added later: arrives from ESP32 as
    -- a raw PLMN code (e.g. "52003"), not a pre-formatted carrier
    -- name — normalized to a human-readable name (e.g. "AIS (AWN)")
    -- before being stored here, via
    -- app/routers/images.py::_normalize_carrier(). "-" (on WiFi) and
    -- any PLMN code not in that mapping table pass through unchanged.
    carrier       TEXT,                -- normalized carrier name | "-" (on WiFi) | null
    wakeup_reason TEXT                 -- "timer" | "manual" | null (null/anything-but-"timer" -> is_test=true)
);

-- ⚠️ Migration สำหรับ DB ที่เคยผ่านเวอร์ชันก่อนหน้ามาแล้ว (ไม่ว่าจะยังไม่มี
-- log_time เลย, มี log_time แต่ต่อท้ายตาราง (จาก ADD COLUMN ซึ่งต่อท้าย
-- เสมอ ไม่ใช่แทรกตำแหน่งที่ระบุใน CREATE TABLE), หรือยังใช้ชื่อคอลัมน์เดิม
-- data1/data2/data3 อยู่) — ทำให้ตรงกับ CREATE TABLE ด้านบนทุกกรณี
-- confirmed: ต้อง reorder จริง (สร้างตารางใหม่+ย้ายข้อมูล) ไม่ใช่แค่
-- ALTER TABLE ADD COLUMN เพราะ Postgres ไม่มีคำสั่งขยับตำแหน่งคอลัมน์
-- ตรงๆ — pattern เดียวกับที่ใช้ reorder ocr_meter ก่อนหน้า
DO $$
DECLARE
    correct_order TEXT[] := ARRAY['id','log_date','log_time','meter_id','net_mode','carrier','wakeup_reason'];
    actual_order  TEXT[];
BEGIN
    -- เปลี่ยนชื่อคอลัมน์เก่าก่อน (no-op ถ้าเปลี่ยนไปแล้ว หรือเป็น fresh
    -- install ที่ไม่เคยมีชื่อเก่าเลย)
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'esp32_upload_log' AND column_name = 'data1') THEN
        ALTER TABLE esp32_upload_log RENAME COLUMN data1 TO net_mode;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'esp32_upload_log' AND column_name = 'data2') THEN
        ALTER TABLE esp32_upload_log RENAME COLUMN data2 TO carrier;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'esp32_upload_log' AND column_name = 'data3') THEN
        ALTER TABLE esp32_upload_log RENAME COLUMN data3 TO wakeup_reason;
    END IF;

    -- เพิ่ม log_time ถ้ายังไม่มีเลย (DB ที่ไม่เคยรัน migration รอบก่อน
    -- มาก่อนเลย) — backfill แถวเก่าด้วย 00:00:00 ชั่วคราว (ไม่มีทางรู้
    -- เวลาจริงย้อนหลังได้แม่นยำกว่านี้)
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'esp32_upload_log' AND column_name = 'log_time') THEN
        ALTER TABLE esp32_upload_log ADD COLUMN log_time TIME;
        UPDATE esp32_upload_log SET log_time = '00:00:00' WHERE log_time IS NULL;
        ALTER TABLE esp32_upload_log ALTER COLUMN log_time SET NOT NULL;
    END IF;

    -- เช็คว่าลำดับคอลัมน์ตรงกับที่ต้องการหรือยัง (log_time อยู่ติดหลัง
    -- log_date จริงไหม) ถ้าไม่ตรง (เช่น log_time ไปต่อท้ายตารางเพราะ
    -- ADD COLUMN ก่อนหน้า) ให้สร้างตารางใหม่ตามลำดับที่ถูกต้องแล้วย้าย
    -- ข้อมูล
    SELECT array_agg(column_name::text ORDER BY ordinal_position)
    INTO actual_order
    FROM information_schema.columns
    WHERE table_name = 'esp32_upload_log';

    IF actual_order IS DISTINCT FROM correct_order THEN
        -- เจอ error จริงตอน deploy รอบก่อน — esp32_upload_log.id เดิม
        -- สร้างด้วย BIGSERIAL ตอน fresh install ทำให้ Postgres ผูก
        -- esp32_upload_log_id_seq ให้เป็นของ (OWNED BY) column นี้
        -- อัตโนมัติ ถ้าไม่ตัดความเป็นเจ้าของออกก่อน ตอน DROP TABLE
        -- esp32_upload_log ด้านล่าง Postgres จะพยายามลบ sequence ตามไป
        -- ด้วย (เพราะเป็นเจ้าของ) แต่ลบไม่ได้เพราะ
        -- esp32_upload_log_reordered ที่เพิ่งสร้างก็อ้างอิง sequence
        -- เดียวกันอยู่ — ชนกัน error "cannot drop table ... other
        -- objects depend on it" (pattern เดียวกับที่เคยเจอกับ ocr_meter
        -- ด้านบน — คราวนี้ลืมใส่ fix นี้ตอนสร้างใหม่ แก้แล้ว) แก้โดยตัด
        -- ความเป็นเจ้าของออกก่อน ให้ sequence ลอยอิสระ ไม่ผูกกับตาราง
        -- ไหนจนกว่าจะผูกใหม่ด้านล่าง
        ALTER SEQUENCE esp32_upload_log_id_seq OWNED BY NONE;

        CREATE TABLE esp32_upload_log_reordered (
            id            BIGINT    PRIMARY KEY DEFAULT nextval('esp32_upload_log_id_seq'),
            log_date      DATE      NOT NULL,
            log_time      TIME      NOT NULL,
            meter_id      TEXT      NOT NULL,
            net_mode      TEXT,
            carrier       TEXT,
            wakeup_reason TEXT
        );
        INSERT INTO esp32_upload_log_reordered (id, log_date, log_time, meter_id, net_mode, carrier, wakeup_reason)
            SELECT id, log_date, log_time, meter_id, net_mode, carrier, wakeup_reason
            FROM esp32_upload_log
            ORDER BY id;
        DROP TABLE esp32_upload_log;
        ALTER TABLE esp32_upload_log_reordered RENAME TO esp32_upload_log;
        ALTER TABLE esp32_upload_log RENAME CONSTRAINT esp32_upload_log_reordered_pkey TO esp32_upload_log_pkey;
        -- ผูก sequence กลับเข้ากับ column ใหม่ให้เรียบร้อย (ไม่จำเป็นต่อ
        -- การทำงาน แค่ให้ Postgres จัดการ sequence ให้อัตโนมัติเวลา
        -- DROP TABLE ในอนาคต เหมือนตอนที่เป็น BIGSERIAL แต่แรก)
        ALTER SEQUENCE esp32_upload_log_id_seq OWNED BY esp32_upload_log.id;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_esp32_upload_log_meter ON esp32_upload_log (meter_id, log_date DESC);

-- ⚠️ esp32_upload_log's meter_id FK — ต้องอยู่ "หลัง" reorder migration
-- ด้านบนเสมอ (ไม่ใช่ก่อน แบบที่เคยเป็นบั๊กจริงตอน deploy) เพราะ reorder
-- migration ทำ DROP TABLE esp32_upload_log แล้วสร้างตารางใหม่ที่ไม่มี
-- REFERENCES device_config(meter_id) ในนิยามคอลัมน์เอง — ถ้าเพิ่ม FK
-- ก่อนบล็อก reorder ตาราง (และ FK) เดิมจะถูกลบทิ้งไปพร้อมกันตอน DROP
-- TABLE แล้วไม่มีอะไรมาเพิ่ม FK ให้ใหม่อีกเลย จบ statement สุดท้ายของ
-- ไฟล์ = รันหลัง reorder เสร็จสมบูรณ์เสมอ ไม่ว่า reorder จะ trigger
-- จริงหรือเป็น no-op (fresh install ที่ลำดับคอลัมน์ถูกต้องตั้งแต่ต้น)
ALTER TABLE esp32_upload_log DROP CONSTRAINT IF EXISTS esp32_upload_log_meter_id_fkey;
ALTER TABLE esp32_upload_log ADD CONSTRAINT esp32_upload_log_meter_id_fkey FOREIGN KEY (meter_id) REFERENCES device_config(meter_id);

-- images.job_id FK ไปหา ocr_jobs — confirmed request. ต้องอยู่ท้ายไฟล์
-- (หลัง CREATE TABLE ocr_jobs แน่นอน) เพราะคอลัมน์ job_id ใน CREATE TABLE
-- images ต้นไฟล์ตั้งใจไม่ใส่ REFERENCES ไว้เลย (ocr_jobs ยังไม่ถูกสร้าง ณ
-- จุดนั้น) — ดู comment ของคอลัมน์ job_id ในนิยาม CREATE TABLE images เอง
ALTER TABLE images DROP CONSTRAINT IF EXISTS images_job_id_fkey;
ALTER TABLE images ADD CONSTRAINT images_job_id_fkey FOREIGN KEY (job_id) REFERENCES ocr_jobs(id);
