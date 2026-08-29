-- image-store — init.sql
-- --------------------------------------------------------------------------
-- Schema เดียว รันได้ปลอดภัยกับ DB ทุกสถานะ — fresh install, DB ที่เคยรัน
-- schema เก่า (จะถูกอัปเกรด/backfill ให้อัตโนมัติ), หรือ DB ที่อัปเดตครบ
-- แล้ว (รันซ้ำได้เฉยๆ) — ไม่มีไฟล์ migration แยกอีกต่อไป
--
-- image-store เก็บ 4 อย่าง:
--   1. users — ศูนย์กลาง auth เดียว ที่ meter-dashboard (และทุก service
--      อื่น) เชื่อถือ
--   2. images_electric/water/gas (แยกตามประเภทมิเตอร์) + ocr_jobs
--      (ตารางเดียวรวมทุกประเภท) — ข้อมูล hardware capture + internal
--      job queue ของ OCR ล้วนๆ
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

-- --- Per-meter-type hardware/OCR tables ----------------------------------
-- แยกตามตัวอักษรแรกของ meter_id ตอนอัปโหลด (E->electric, W->water,
-- G->gas) — meter_id เก็บเป็นตัวพิมพ์ใหญ่เสมอ (normalize ตอน parse ชื่อ
-- ไฟล์ — ดู app/filename.py)
--
-- images_electric/water/gas ทั้ง 3 ใช้ id จาก sequence เดียวกัน (และ
-- ocr_jobs_* อีกชุดหนึ่ง) เพื่อให้ id ไม่ชนกันข้ามตาราง
--
-- group_id / is_anchor / received_at: ESP32 ส่งภาพเป็นชุด (burst) หลายภาพ
-- ต่อการอ่าน 1 ครั้ง — server รวมภาพที่มาถึงจาก meter_id เดียวกันภายใน
-- หน้าต่างเวลาหนึ่ง (app.config.image_group_window_seconds) ให้เป็น
-- "กลุ่ม" เดียว ก่อนสร้าง ocr_jobs ให้ 1 job ต่อ 1 กลุ่ม — หรือทันทีถ้า
-- ภาพครบ image_group_size แล้ว (ไม่ต้องรอครบเวลา — ดู app/routers/images.py)
--
-- group_id (TEXT, เช่น "E1", "W3", "G12") คือรหัสกลุ่มที่มนุษย์อ่านง่าย —
-- นับแยกต่างหากต่อประเภทมิเตอร์ (ไม่ใช่เลข id ดิบที่กระโดดข้ามกันเพราะ 3
-- ตารางแชร์ sequence เดียวกัน) กำหนดครั้งเดียวตอนเปิดกลุ่มใหม่ ภาพอื่นใน
-- กลุ่มเดียวกันได้ค่าเดียวกันหมด — **เดิมมี group_id (BIGINT, ชี้ id ของ
-- หัวกลุ่ม) แยกกับ group_label (TEXT, E1/W3) คนละคอลัมน์ ตอนนี้รวมเป็น
-- คอลัมน์เดียว: group_id (TEXT) คือ E1/W3/G12 ตรงๆ เลย ไม่มีเลข BIGINT
-- คู่ขนานอีกต่อไป**
--
-- is_anchor (BOOLEAN) แทนที่กลไก "group_id = id" เดิมที่ใช้บอกว่าแถวไหน
-- เป็นหัวกลุ่ม — เพราะ group_id ไม่ใช่ BIGINT ที่ self-reference กับ id
-- ได้อีกแล้ว (เป็น TEXT ที่ทุกแถวในกลุ่มมีค่าเดียวกันหมด) จึงต้องมี flag
-- แยกบอกชัดๆ แทน — true = แถวนี้เป็นแถวแรกที่เปิดกลุ่มนี้ขึ้นมา (เก็บ
-- meter_id/original_filename/device_timestamp ที่จะก็อปเข้า ocr_jobs ตอน
-- ปิดกลุ่ม), false = แถวอื่นๆ ที่มาสมทบทีหลังในกลุ่มเดียวกัน กลไก
-- claim/sweep ที่กันการ race กันตอนสร้าง/ปิดกลุ่มล็อกที่แถว is_anchor=true
-- แทนที่จะล็อกที่ group_id=id แบบเดิม
--
-- received_at คือเวลาที่ server ได้รับภาพจริง (ใช้วัด timeout ของกลุ่ม)
-- ต่างจาก device_timestamp ซึ่งเป็นเวลาที่ device อ้างว่าถ่าย
CREATE SEQUENCE IF NOT EXISTS images_id_seq;
CREATE SEQUENCE IF NOT EXISTS ocr_jobs_id_seq;
CREATE SEQUENCE IF NOT EXISTS electric_group_seq;
CREATE SEQUENCE IF NOT EXISTS water_group_seq;
CREATE SEQUENCE IF NOT EXISTS gas_group_seq;

CREATE TABLE IF NOT EXISTS images_electric (
    id                BIGINT      PRIMARY KEY DEFAULT nextval('images_id_seq'),
    meter_id          TEXT        NOT NULL,
    original_filename TEXT,
    device_timestamp  TIMESTAMPTZ,
    ocr_status        TEXT        NOT NULL DEFAULT 'pending',  -- pending | done | failed
    group_id          TEXT        NOT NULL,
    is_anchor         BOOLEAN     NOT NULL DEFAULT false,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS images_water (LIKE images_electric INCLUDING ALL);
CREATE TABLE IF NOT EXISTS images_gas   (LIKE images_electric INCLUDING ALL);

CREATE INDEX IF NOT EXISTS idx_images_electric_meter ON images_electric (meter_id, device_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_images_water_meter    ON images_water    (meter_id, device_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_images_gas_meter      ON images_gas      (meter_id, device_timestamp DESC);

-- อัปเกรดสำหรับ DB ที่ยังมี schema รอบก่อน (group_id เป็น BIGINT คู่กับ
-- group_label เป็น TEXT แยกกัน) ให้กลายเป็น schema ใหม่ (group_id เดียว
-- เป็น TEXT, มี is_anchor แยก) — no-op บน fresh install (ตารางเพิ่งถูก
-- สร้างครบตามด้านบนอยู่แล้ว ไม่มี group_id แบบ BIGINT ให้เจอ) ปลอดภัยรัน
-- ซ้ำได้ไม่จำกัดจำนวนรอบ ต้องมาก่อน index ที่อ้างถึง is_anchor ด้านล่าง
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['images_electric', 'images_water', 'images_gas']
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = tbl AND column_name = 'group_id' AND data_type = 'bigint'
        ) THEN
            -- เติม is_anchor ชั่วคราว, backfill จากกลไกเดิม (group_id=id
            -- คือหัวกลุ่ม) ก่อนจะลบ column BIGINT เก่าทิ้ง
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS is_anchor BOOLEAN', tbl);
            EXECUTE format('UPDATE %I SET is_anchor = (group_id = id) WHERE is_anchor IS NULL', tbl);
            EXECUTE format('ALTER TABLE %I DROP COLUMN group_id', tbl);
            -- group_label เดิม (ถ้ามีจากรอบก่อน) กลายเป็น group_id ใหม่ —
            -- ถ้าไม่มีเลย (DB เก่ากว่านั้นอีก ไม่เคยผ่านรอบ group_label)
            -- เติม column เปล่าไว้ก่อน ให้ backfill ด้านล่างเติมค่าให้
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
        -- meter_id เก็บเป็นตัวพิมพ์ใหญ่เสมอตั้งแต่นี้ไป — บรรทัดนี้แปลง
        -- แถวเก่าที่อาจยังเป็นตัวพิมพ์เล็กอยู่ ให้ตรงกันหมดครั้งเดียว
        EXECUTE format('UPDATE %I SET meter_id = UPPER(meter_id) WHERE meter_id != UPPER(meter_id)', tbl);
    END LOOP;
END $$;

-- เผื่อมีแถวที่ไม่มีค่า group_id เลยหลัง migrate ด้านบน (ไม่ควรเกิดขึ้น
-- ถ้ามี group_label เดิมอยู่แล้วก่อนหน้า แต่กันไว้เผื่อ edge case) — ให้
-- แต่ละแถวกลายเป็นกลุ่มของตัวเองไปเลย ปลอดภัยสุด ไม่เสี่ยงรวมกลุ่มผิด
UPDATE images_electric SET group_id = 'E' || nextval('electric_group_seq'), is_anchor = true WHERE group_id IS NULL OR group_id = '';
UPDATE images_water    SET group_id = 'W' || nextval('water_group_seq'),    is_anchor = true WHERE group_id IS NULL OR group_id = '';
UPDATE images_gas      SET group_id = 'G' || nextval('gas_group_seq'),      is_anchor = true WHERE group_id IS NULL OR group_id = '';
ALTER TABLE images_electric ALTER COLUMN group_id SET NOT NULL;
ALTER TABLE images_water    ALTER COLUMN group_id SET NOT NULL;
ALTER TABLE images_gas      ALTER COLUMN group_id SET NOT NULL;

-- หา "กลุ่มที่ยังเปิดอยู่" ของมิเตอร์หนึ่งๆ ให้เร็ว (upload ใหม่เช็คว่ามี
-- กลุ่มเปิดอยู่ไหม, background sweep เช็คว่ากลุ่มไหนหมดเวลาแล้ว) — index
-- นี้ครอบคลุมเฉพาะแถวที่เป็น "หัวกลุ่ม" เท่านั้น (is_anchor = true) — ต้อง
-- DROP ก่อนเพราะเปลี่ยน WHERE clause จากเดิม (group_id = id) ไม่ใช่แค่
-- เปลี่ยนชื่อ คนละนิยาม ALTER แก้ partial index ในที่เดิมไม่ได้ ต้อง
-- drop+create ใหม่เท่านั้น — DROP IF EXISTS ปลอดภัยรันซ้ำได้เสมอ
DROP INDEX IF EXISTS idx_images_electric_group_lookup;
DROP INDEX IF EXISTS idx_images_water_group_lookup;
DROP INDEX IF EXISTS idx_images_gas_group_lookup;
CREATE INDEX IF NOT EXISTS idx_images_electric_group_lookup ON images_electric (meter_id, received_at) WHERE is_anchor = true;
CREATE INDEX IF NOT EXISTS idx_images_water_group_lookup    ON images_water    (meter_id, received_at) WHERE is_anchor = true;
CREATE INDEX IF NOT EXISTS idx_images_gas_group_lookup      ON images_gas      (meter_id, received_at) WHERE is_anchor = true;

-- ค้นภาพทั้งหมดในกลุ่มเดียวกันให้เร็ว (ตอน claim ต้องดึงทุกภาพในกลุ่ม)
CREATE INDEX IF NOT EXISTS idx_images_electric_group_id ON images_electric (group_id);
CREATE INDEX IF NOT EXISTS idx_images_water_group_id    ON images_water    (group_id);
CREATE INDEX IF NOT EXISTS idx_images_gas_group_id      ON images_gas      (group_id);

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
    status            TEXT        NOT NULL DEFAULT 'queued',  -- queued | processing | done | failed
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
-- reading_date/reading_time: เวลาที่ ESP32 **ถ่ายภาพ** (มาจาก
-- ocr_jobs.device_timestamp ของ job นั้น) ไม่ใช่เวลาที่ OCR ประมวลผล —
-- server เป็นคนเติมให้เองจาก device_timestamp ไม่ใช่ค่าที่ OCR client ส่งมา
--
-- image_error: ใส่เฉพาะตอน error_type != 0 เท่านั้น (1, 2, หรือ 3
-- — ไม่ใส่ตอนสำเร็จเปล่าๆ error_type=0) เป็น**ชื่อไฟล์เดียวกับที่หัวกลุ่ม
-- ถูกอัปโหลดไว้แล้วตรงๆ** (ไม่ใช่ไฟล์แยกที่ OCR อัปโหลดซ้ำมาใหม่ — เดิม
-- เคยรับ multipart แนบไฟล์ใหม่ แต่ยกเลิกไปแล้ว เพราะ OCR client ไม่มี
-- ภาพอื่นนอกจากภาพที่ ESP32 ส่งมาอยู่แล้วตั้งแต่ต้น การให้อัปโหลดซ้ำมีแต่
-- เสี่ยงชื่อไฟล์ชนกับภาพอื่นในกลุ่มเอง ไม่มีประโยชน์อะไรเพิ่ม) แค่ชี้กลับ
-- ไปที่ไฟล์ที่มีอยู่แล้วในเครื่อง ให้คนอ่านตรวจสอบตอนเกิด error/ผิดปกติ
-- (ชื่อคอลัมน์เดิมคือ ocr_image_filename — เปลี่ยนเป็น image_error ให้
-- สื่อความหมายตรงขึ้น เพราะมีค่าเฉพาะตอนเกิด error เท่านั้น)
--
-- ตารางนี้ตั้งใจให้มีแค่ 6 field ตามที่ยืนยัน (meter_id, reading_date,
-- reading_time, ocr_reading, error_type, image_error) — ไม่มี group_id
-- ในตารางนี้แล้ว (เคยมีอยู่ช่วงสั้นๆ ตอนรวม column กับ ocr_jobs แต่ตัด
-- ออกตามที่ขอ — group_id ยังใช้เป็นกลไกภายในต่อใน images_*/ocr_jobs
-- ตามเดิม แค่ไม่ก็อปมาใส่ตารางผลลัพธ์นี้อีกต่อไป)
CREATE TABLE IF NOT EXISTS ocr_meter (
    id                  BIGSERIAL   PRIMARY KEY,
    meter_id            TEXT        NOT NULL,
    reading_date        DATE        NOT NULL,
    reading_time        TIME        NOT NULL,
    ocr_reading         NUMERIC,
    error_type          INTEGER     NOT NULL REFERENCES error_type(code),
    image_error         TEXT
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
    -- reading_timestamp เดียว (TIMESTAMPTZ) จากรอบทดลองสั้นๆ ที่ยกเลิก
    -- ไปแล้ว -> แยกกลับเป็น reading_date + reading_time เหมือนเดิม — แปลง
    -- กลับเป็นเวลาไทย (Bangkok, UTC+7) local ก่อนแยก เพราะ TIMESTAMPTZ
    -- เก็บเป็น UTC ภายใน ถ้าแยกตรงๆ โดยไม่แปลงโซนก่อน วันที่/เวลาจะเพี้ยน
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ocr_meter' AND column_name = 'reading_timestamp'
    ) THEN
        ALTER TABLE ocr_meter ADD COLUMN IF NOT EXISTS reading_date DATE;
        ALTER TABLE ocr_meter ADD COLUMN IF NOT EXISTS reading_time TIME;
        UPDATE ocr_meter
        SET reading_date = (reading_timestamp AT TIME ZONE 'Asia/Bangkok')::DATE,
            reading_time = (reading_timestamp AT TIME ZONE 'Asia/Bangkok')::TIME
        WHERE reading_date IS NULL;
        ALTER TABLE ocr_meter DROP COLUMN reading_timestamp;
        ALTER TABLE ocr_meter ALTER COLUMN reading_date SET NOT NULL;
        ALTER TABLE ocr_meter ALTER COLUMN reading_time SET NOT NULL;
    END IF;
END $$;


ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_error_type_check;
ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_error_type_fkey;
ALTER TABLE ocr_meter ADD CONSTRAINT ocr_meter_error_type_fkey FOREIGN KEY (error_type) REFERENCES error_type(code);

-- reading_date DESC, reading_time DESC รองรับ query แบบที่ OCR client
-- ต้องใช้บ่อยที่สุด: "ค่าล่าสุดของมิเตอร์นี้คือเท่าไหร่" — DROP ก่อนเผื่อ
-- ยังมี index ชื่อเดิมค้างจาก definition ที่ต่างออกไป
DROP INDEX IF EXISTS idx_ocr_meter_meter_id;
CREATE INDEX IF NOT EXISTS idx_ocr_meter_meter_id ON ocr_meter (meter_id, reading_date DESC, reading_time DESC);
