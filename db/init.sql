-- image-store — init.sql
-- --------------------------------------------------------------------------
-- Schema เดียว รันได้ปลอดภัยกับ DB ทุกสถานะ — ทั้ง fresh install (ว่าง
-- เปล่าสนิท, รันอัตโนมัติตอนสร้าง container "db" ครั้งแรกผ่าน
-- docker-entrypoint-initdb.d), DB ที่เคยรัน schema เก่าไปแล้วก่อน
-- ocr_meter/group_id จะมีอยู่ (จะถูกเติมให้ครบอัตโนมัติ), หรือ DB ที่
-- อัปเดตครบแล้ว (รันซ้ำได้เฉยๆ ไม่มีอะไรเปลี่ยน) — ทุกคำสั่งใช้
-- IF NOT EXISTS/ตรวจก่อนแก้เสมอ ไม่มีไฟล์ migration แยกอีกต่อไป
--
-- image-store เก็บ 3 อย่าง:
--   1. users — ศูนย์กลาง auth เดียว ที่ meter-dashboard (และทุก service
--      อื่น) เชื่อถือ
--   2. images_electric/water/gas (แยกตามประเภทมิเตอร์) + ocr_jobs
--      (ตารางเดียวรวมทุกประเภท) — ข้อมูล hardware capture + internal
--      job queue ของ OCR ล้วนๆ ไม่มีข้อมูลร้านค้า/บิล/สิทธิ์เข้าถึงเลย
--   3. ocr_meter — ผลลัพธ์ OCR ที่จบแล้ว (สำเร็จ/error ที่รู้จัก) ตาราง
--      กลางสำหรับส่งต่อให้ระบบภายนอกใช้ ไม่อ้างอิงกลับไปที่ 2 ข้อบนเลย
--      อันนั้นอยู่ที่ meter-dashboard's เอง (db/init.sql ของมัน) —
--      ตารางพวกนี้เก็บไว้ให้ service อื่นดึงผล OCR ไปใช้ผ่าน API
--      เท่านั้น ไม่เกี่ยวกับ dashboard โดยตรงเลย
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
-- G->gas) — ไฟล์บนดิสก์ตั้งชื่อตาม original_filename ตรงๆ (ดู
-- app/storage.py) ไม่มีคอลัมน์เก็บชื่อไฟล์ที่ derive มาแยกอีก
--
-- images_electric/water/gas ทั้ง 3 ใช้ id จาก sequence เดียวกัน (และ
-- ocr_jobs_* อีกชุดหนึ่ง) เพื่อให้ id ไม่ชนกันข้ามตาราง แม้ข้อมูลจะแยก
-- เก็บจริงก็ตาม
--
-- group_id / received_at: ESP32 ส่งภาพเป็นชุด (burst) หลายภาพต่อการอ่าน
-- 1 ครั้ง (เช่น 3 ภาพติดกันภายในไม่กี่วินาที) — server รวมภาพที่มาถึงจาก
-- meter_id เดียวกันภายในหน้าต่างเวลาหนึ่ง (ดู
-- app.config.image_group_window_seconds) ให้เป็น "กลุ่ม" เดียว ก่อนค่อย
-- สร้าง ocr_jobs ให้ 1 job ต่อ 1 กลุ่ม (ไม่ใช่ 1 job ต่อ 1 ภาพเหมือนเดิม)
--
-- group_id ชี้ไปที่ id ของภาพ "หัวกลุ่ม" (ภาพแรกที่มาถึงของกลุ่มนั้น) —
-- ภาพหัวกลุ่มเองมี group_id = id ของตัวเอง (self-reference) ภาพอื่นที่
-- ตามมาในหน้าต่างเวลาเดียวกันจะมี group_id ชี้ไปที่ภาพหัวกลุ่มนั้น หา
-- ภาพทั้งหมดในกลุ่มเดียวกันได้ด้วย "WHERE group_id = <หัวกลุ่ม>"
--
-- received_at คือเวลาที่ server ได้รับภาพจริง (ใช้วัด timeout ของกลุ่ม)
-- ต่างจาก device_timestamp ซึ่งเป็นเวลาที่ device อ้างว่าถ่าย (จากชื่อ
-- ไฟล์ อาจคลาดเคลื่อนจากนาฬิกา device ได้) — สองอย่างนี้ไม่ใช่ตัวเดียวกัน
-- ตั้งใจแยกไว้
CREATE SEQUENCE IF NOT EXISTS images_id_seq;
CREATE SEQUENCE IF NOT EXISTS ocr_jobs_id_seq;

CREATE TABLE IF NOT EXISTS images_electric (
    id                BIGINT      PRIMARY KEY DEFAULT nextval('images_id_seq'),
    meter_id          TEXT        NOT NULL,
    original_filename TEXT,
    device_timestamp  TIMESTAMPTZ,
    ocr_status        TEXT        NOT NULL DEFAULT 'pending',  -- pending | done | failed
    group_id          BIGINT      NOT NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS images_water (LIKE images_electric INCLUDING ALL);
CREATE TABLE IF NOT EXISTS images_gas   (LIKE images_electric INCLUDING ALL);

CREATE INDEX IF NOT EXISTS idx_images_electric_meter ON images_electric (meter_id, device_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_images_water_meter    ON images_water    (meter_id, device_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_images_gas_meter      ON images_gas      (meter_id, device_timestamp DESC);

-- ถ้า images_electric/water/gas มีอยู่แล้วจาก schema เก่า (ก่อนมี
-- group_id/received_at) บล็อกนี้เติม column ให้ครบแล้ว backfill แถวเก่า
-- ให้เป็น "กลุ่มของตัวเอง" — บน fresh install ตารางเพิ่งถูกสร้างพร้อม
-- 2 column นี้อยู่แล้วด้านบน บล็อกนี้เลยแค่ no-op ผ่านไปเฉยๆ ไม่มีผลอะไร
-- ปลอดภัยรันซ้ำได้ไม่จำกัดจำนวนรอบ — ต้องมาก่อน index ด้านล่างที่อ้างถึง
-- group_id เสมอ ไม่งั้น CREATE INDEX จะพังถ้า column ยังไม่มี (แก้จากบั๊ก
-- ที่เจอจริงตอน migrate cfo_iot — เดิมอยู่หลัง index เลย fail ไป 3 ตัว)
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['images_electric', 'images_water', 'images_gas']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS group_id BIGINT', tbl);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ', tbl);
        EXECUTE format('UPDATE %I SET group_id = id WHERE group_id IS NULL', tbl);
        EXECUTE format(
            'UPDATE %I SET received_at = COALESCE(device_timestamp, now()) WHERE received_at IS NULL',
            tbl
        );
        EXECUTE format('ALTER TABLE %I ALTER COLUMN group_id SET NOT NULL', tbl);
        EXECUTE format('ALTER TABLE %I ALTER COLUMN received_at SET NOT NULL', tbl);
        EXECUTE format('ALTER TABLE %I ALTER COLUMN received_at SET DEFAULT now()', tbl);
    END LOOP;
END $$;

-- หา "กลุ่มที่ยังเปิดอยู่" ของมิเตอร์หนึ่งๆ ให้เร็ว (upload ใหม่เช็คว่ามี
-- กลุ่มเปิดอยู่ไหม, background sweep เช็คว่ากลุ่มไหนหมดเวลาแล้ว) — index
-- นี้ครอบคลุมเฉพาะแถวที่เป็น "หัวกลุ่ม" เท่านั้น (group_id = id)
CREATE INDEX IF NOT EXISTS idx_images_electric_group_lookup ON images_electric (meter_id, received_at) WHERE group_id = id;
CREATE INDEX IF NOT EXISTS idx_images_water_group_lookup    ON images_water    (meter_id, received_at) WHERE group_id = id;
CREATE INDEX IF NOT EXISTS idx_images_gas_group_lookup      ON images_gas      (meter_id, received_at) WHERE group_id = id;

-- meter_id/original_filename/device_timestamp ก็อปมาจากแถว "หัวกลุ่ม"
-- (denormalized) ให้เปิดตาราง ocr_jobs เฉยๆ (เช่นผ่าน pgweb) แล้วรู้ครบ
-- ระดับหนึ่งไม่ต้อง join กลับไปที่ images_* เอง — แต่เป็นแค่ข้อมูลของ
-- "หัวกลุ่ม" เท่านั้น ไม่ใช่ของทุกภาพในกลุ่ม (ภาพอื่นในกลุ่มเดียวกันหาได้
-- จาก images_*.group_id = image_id ของ job นี้)
--
-- ตารางเดียวรวมทุกประเภทมิเตอร์ (ไม่แยก electric/water/gas เหมือน
-- images_*) — image_id ไม่มี FK ตั้งใจ (แถวที่ชี้ไปอาจอยู่ใน
-- images_electric/water/gas ตารางไหนก็ได้ ขึ้นกับ meter_id ของ job นั้น
-- เอง — id ไม่ชนกันเพราะ images_* ทั้ง 3 ใช้ images_id_seq ร่วมกัน)
-- image_id ไม่ unique เพราะรูปเดียว reprocess ได้หลายรอบ แต่ละรอบสร้าง
-- แถว job ใหม่ ไม่ทับของเดิม ต้องมีได้หลายแถวต่อ image_id
--
-- admin_reason: เหตุผลที่ admin แก้ ocr_reading เอง (ถ้ามี) — ocr_reading
-- ถูกเขียนทับตรงๆ เลยตอน admin แก้ ไม่มีคอลัมน์แยกเก็บค่าเดิมที่ OCR
-- เคยอ่านได้อีกต่อไป
CREATE TABLE IF NOT EXISTS ocr_jobs (
    id                BIGINT      PRIMARY KEY DEFAULT nextval('ocr_jobs_id_seq'),
    image_id          BIGINT      NOT NULL,
    meter_id          TEXT        NOT NULL,
    original_filename TEXT,
    device_timestamp  TIMESTAMPTZ,
    ocr_reading       NUMERIC,
    status            TEXT        NOT NULL DEFAULT 'queued',  -- queued | processing | done | failed
    attempts          BIGINT      NOT NULL DEFAULT 0,
    last_error        TEXT,
    admin_reason      TEXT
);


-- ocr_meter — ผลลัพธ์ OCR ที่ "จบแล้ว" ของแต่ละมิเตอร์ (สำเร็จ หรือ error
-- ที่รู้แน่ชัดแล้วทั้ง 3 แบบ) — ตารางกลางสำหรับส่งต่อให้ระบบภายนอก
-- (External Store) ใช้ ไม่มี FK อ้างอิงกลับไปที่ images_*/ocr_jobs เลย
-- ตั้งใจ — อ่านตารางนี้เฉยๆ ก็รู้เรื่องครบ ไม่ต้อง join กลับไปที่ไหนอีก
--
-- ต่างจาก ocr_jobs ตรงนี้: ocr_jobs คือ internal job queue ล้วนๆ (กัน
-- retry ค้าง, เก็บ attempts) — ความล้มเหลวแบบชั่วคราว/retry ได้ (network,
-- OCR_API_URL ไม่ถูกตั้งค่า ฯลฯ) ยังคงอยู่แค่ใน ocr_jobs.last_error ผ่าน
-- /fail เหมือนเดิม ไม่มาสร้างแถวที่นี่ — ocr_meter มีแถวก็ต่อเมื่อ OCR
-- "จบงาน" แล้วเท่านั้น (ผ่าน /result) ไม่ว่าผลจะอ่านได้ค่าจริง หรือสรุป
-- เป็น error case ที่รู้จักก็ตาม
--
-- error_type: NULL = อ่านสำเร็จ (ocr_reading ต้องมีค่า) ไม่ NULL = 1 ใน
-- 4 แบบที่รู้จัก:
--   no_digits_found   - ภาพไม่มีตัวเลขให้อ่านเลย (ocr_reading เป็น NULL)
--   image_unreadable  - เปิด/ประมวลผลไฟล์ภาพไม่ได้เลย (ocr_reading เป็น NULL)
--   reading_decreased - ค่าที่อ่านได้ (ocr_reading ยังมีค่าอยู่) น้อยกว่า
--                        เดือนที่แล้ว ทั้งที่ควรเพิ่มขึ้นเสมอ
--   usage_anomaly     - ค่าที่อ่านได้ (ocr_reading ยังมีค่าอยู่) เดือนนี้
--                        ใช้ไปเยอะผิดปกติเทียบกับอัตราการใช้เฉลี่ยของ
--                        เดือนก่อนๆ (เช่น เกิน 3 เท่าของค่าเฉลี่ย)
-- ทั้ง reading_decreased และ usage_anomaly: OCR client เป็นคนดึง history
-- จากตารางนี้เอง (ผ่าน GET /admin/meters/{meter_id}/ocr-readings) คำนวณ
-- เทียบเอง แล้วส่ง error_type นี้มาพร้อม ocr_reading จริงที่อ่านได้ของ
-- เดือนปัจจุบัน (ไม่ใช่ server คำนวณให้)
CREATE TABLE IF NOT EXISTS ocr_meter (
    id                  BIGSERIAL   PRIMARY KEY,
    meter_id            TEXT        NOT NULL,
    reading_date        DATE        NOT NULL,
    reading_time        TIME        NOT NULL,
    ocr_reading         NUMERIC,
    error_type          TEXT        CHECK (error_type IS NULL OR error_type IN ('no_digits_found', 'image_unreadable', 'reading_decreased', 'usage_anomaly')),
    error_detail        TEXT,
    ocr_image_filename  TEXT
);

-- ถ้า ocr_meter มีอยู่แล้วจากรอบก่อน usage_anomaly (CHECK ตอนนั้นมีแค่ 3
-- ค่า) บล็อกนี้อัปเกรด constraint ให้ครบ 4 ค่า — ปลอดภัยรันซ้ำได้เสมอ
-- (บน fresh install ก็แค่ drop+create constraint ที่เหมือนเดิมเป๊ะ)
ALTER TABLE ocr_meter DROP CONSTRAINT IF EXISTS ocr_meter_error_type_check;
ALTER TABLE ocr_meter ADD CONSTRAINT ocr_meter_error_type_check
    CHECK (error_type IS NULL OR error_type IN ('no_digits_found', 'image_unreadable', 'reading_decreased', 'usage_anomaly'));

-- reading_date DESC, reading_time DESC รองรับ query แบบที่ OCR client
-- ต้องใช้บ่อยที่สุด: "ค่าล่าสุดของมิเตอร์นี้คือเท่าไหร่" (ไว้เทียบกับ
-- ค่าใหม่ที่เพิ่งอ่านได้)
CREATE INDEX IF NOT EXISTS idx_ocr_meter_meter_id ON ocr_meter (meter_id, reading_date DESC, reading_time DESC);
