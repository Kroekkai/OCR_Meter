# OCR Meter Store — เซิร์ฟเวอร์เก็บข้อมูลมิเตอร์ด้วย IoT และ AI

เซิร์ฟเวอร์กลางของระบบอ่านค่ามิเตอร์ไฟฟ้า น้ำประปา และแก๊สอัตโนมัติ
รับภาพหน้าปัดจากอุปกรณ์ ESP32 → จัดคิวให้ระบบ OCR อ่านค่า → เก็บผล → ส่งต่อไปยัง CFO Platform
พร้อมหน้าเว็บสำหรับผู้ดูแลระบบ

> เอกสารนี้เขียนให้ผู้ที่จะมาดูแลหรือพัฒนาต่อ อ่านตั้งแต่ต้นจนจบครั้งหนึ่งก่อนแก้โค้ด
> ส่วนที่สำคัญที่สุดคือ [ขั้นตอน deploy](#10-การอัปเดตเวอร์ชันบนเซิร์ฟเวอร์-deploy) และ [ปัญหาที่พบบ่อย](#12-ปัญหาที่พบบ่อยและวิธีแก้)

---

## สารบัญ

1. [ภาพรวมของระบบ](#1-ภาพรวมของระบบ)
2. [เทคโนโลยีที่ใช้](#2-เทคโนโลยีที่ใช้)
3. [โครงสร้างไฟล์](#3-โครงสร้างไฟล์)
4. [การติดตั้งครั้งแรก](#4-การติดตั้งครั้งแรก)
5. [ค่าตั้งค่า (ตัวแปรสภาพแวดล้อม)](#5-ค่าตั้งค่า-ตัวแปรสภาพแวดล้อม)
6. [ผู้ใช้ สิทธิ์ และการยืนยันตัวตน](#6-ผู้ใช้-สิทธิ์-และการยืนยันตัวตน)
7. [การทำงานหลักทีละขั้น](#7-การทำงานหลักทีละขั้น)
8. [ฐานข้อมูล](#8-ฐานข้อมูล)
9. [รายการ API](#9-รายการ-api)
10. [การอัปเดตเวอร์ชันบนเซิร์ฟเวอร์ (deploy)](#10-การอัปเดตเวอร์ชันบนเซิร์ฟเวอร์-deploy)
11. [คำสั่งที่ใช้บ่อย](#11-คำสั่งที่ใช้บ่อย)
12. [ปัญหาที่พบบ่อยและวิธีแก้](#12-ปัญหาที่พบบ่อยและวิธีแก้)
13. [งานค้างและข้อจำกัดที่รู้อยู่แล้ว](#13-งานค้างและข้อจำกัดที่รู้อยู่แล้ว)
14. [ความปลอดภัย](#14-ความปลอดภัย)

---

## 1. ภาพรวมของระบบ

```
 ┌──────────────┐  1. ส่งภาพ 3 ภาพ/รอบ      ┌────────────────────┐   3. จองงาน / ดึงภาพ   ┌─────────────┐
 │ ESP32 + กล้อง │ ───────────────────────▶ │                    │ ◀──────────────────── │  ระบบ OCR   │
 │ (แบต/เปิดตลอด)│ ◀─────────────────────── │  OCR Meter Store   │ ────────────────────▶ │ YOLO + CNN  │
 └──────────────┘  2. ขอตารางเวลาถ่ายภาพ     │  (FastAPI)         │   4. ส่งผลการอ่านค่า   │  + Gemini   │
                                            │                    │                       └─────────────┘
 ┌──────────────┐  หน้าเว็บผู้ดูแล            │                    │   5. ส่งค่าที่อ่านได้   ┌─────────────┐
 │  ผู้ดูแลระบบ   │ ◀──────────────────────▶ │                    │ ────────────────────▶ │ CFO Platform│
 └──────────────┘                           └─────────┬──────────┘                       └─────────────┘
                                                      │
                                            ┌─────────▼──────────┐
                                            │ PostgreSQL /       │  + โฟลเดอร์ภาพ (Docker volume)
                                            │ TimescaleDB        │
                                            └────────────────────┘
```

ลำดับการทำงานของภาพ 1 ชุด:

1. **อุปกรณ์ถ่ายภาพ** 3 ภาพติดกัน (ค่าเริ่มต้น) แล้วอัปโหลดทีละภาพผ่าน `POST /iot/images/upload`
2. **เซิร์ฟเวอร์จัดกลุ่ม** ภาพที่มาจากมิเตอร์เดียวกันภายใน 60 วินาทีเป็น 1 กลุ่ม (เช่น `E12`)
3. **เมื่อกลุ่มครบ** (หรือหมดเวลารอ) สร้าง**งาน OCR** 1 งานในคิว
4. **ระบบ OCR** (โปรแกรมแยก รันอยู่อีกคอนเทนเนอร์) จองงาน ดึงภาพ ดึงค่าย้อนหลังไปตรวจความผิดปกติ แล้วส่งผลกลับ
5. **เซิร์ฟเวอร์บันทึกผล** ลงตาราง `ocr_meter` (ภาพจริง) หรือ `ocr_meter_test` (ภาพทดสอบ)
6. **ถ้ามิเตอร์มี CFO API Key** ส่งค่าไป CFO Platform ทันที ถ้าส่งไม่สำเร็จ งานเบื้องหลังจะส่งซ้ำตามรอบ
7. **อุปกรณ์ขอตารางเวลา** (`GET /iot/devices/config`) หลังส่งภาพทุกครั้ง ผู้ดูแลแก้ตารางเวลาในหน้าเว็บได้ มีผลตั้งแต่รอบถัดไป

สิ่งที่ระบบนี้**ไม่ได้ทำเอง**:
- **อ่านตัวเลขจากภาพ:** เป็นหน้าที่ของระบบ OCR (repo แยก)
- **ถ่ายภาพ/ตั้งเวลาปลุก:** เป็นหน้าที่ของเฟิร์มแวร์ ESP32 (repo แยก: Carbon และ Carbon-AlwaysOn)
- **ตรวจยืนยันค่ากับเจ้าหน้าที่:** ทำบน CFO Platform

---

## 2. เทคโนโลยีที่ใช้

| ส่วน | เทคโนโลยี |
|---|---|
| ภาษา / เฟรมเวิร์ก | Python 3.12, FastAPI, Uvicorn |
| ฐานข้อมูล | PostgreSQL (TimescaleDB) ผ่าน `asyncpg` (ไม่ใช้ ORM เขียน SQL ตรง) |
| ยืนยันตัวตน | JWT (PyJWT), bcrypt สำหรับรหัสผ่าน, SHA-256 สำหรับ token อุปกรณ์ |
| เข้ารหัสกุญแจ CFO | `cryptography` (Fernet) |
| เรียก API ภายนอก | `httpx` |
| ปรับขนาดภาพก่อนส่ง CFO | Pillow |
| หน้าเว็บผู้ดูแล | HTML + JavaScript ล้วน ไฟล์เดียว (`app/static/*.html`) ไม่มีขั้นตอน build |
| รันระบบ | Docker + Docker Compose |
| Reverse proxy | Nginx Proxy Manager ขององค์กร (ไม่อยู่ใน repo นี้) |

---

## 3. โครงสร้างไฟล์

```
.
├── app/
│   ├── main.py                 # สร้างแอป FastAPI, ผูก router, เริ่มงานเบื้องหลัง (จัดกลุ่ม, ส่ง CFO)
│   ├── config.py               # ค่าตั้งค่าทั้งหมด (อ่านจาก .env / environment)
│   ├── db.py                   # connection pool, ตารางแปลงตัวอักษรนำหน้ารหัสมิเตอร์ → ประเภท
│   ├── auth.py                 # JWT, กุญแจอุปกรณ์, token รายเครื่อง, กุญแจระบบ OCR, ตรวจสิทธิ์
│   ├── filename.py             # แยกรหัสมิเตอร์และเวลาถ่ายจากชื่อไฟล์, ตัดสินภาพทดสอบ (_Test)
│   ├── grouping.py             # จัดกลุ่มภาพ, ปิดกลุ่มเมื่อหมดเวลา, กฎภาพจริงวันละ 1 ชุด
│   ├── storage.py              # เขียน/อ่านไฟล์ภาพใน UPLOAD_DIR
│   ├── external_push.py        # ส่งผลไป CFO Platform, ส่งซ้ำ, จัดการสถานะ push
│   ├── crypto.py               # เข้ารหัส/ถอดรหัส CFO API Key (Fernet)
│   ├── repo.py                 # ฟังก์ชันอ่านข้อมูลภาพที่ใช้ร่วมกัน
│   ├── schemas.py              # รูปแบบข้อมูลขาเข้า/ขาออก (Pydantic) และการตรวจค่า
│   ├── prefix_middleware.py    # รองรับ prefix /iot หลัง reverse proxy
│   ├── routers/
│   │   ├── auth_routes.py      # /login, /me, เปลี่ยนรหัสผ่าน
│   │   ├── admin_users.py      # จัดการบัญชีผู้ดูแล (เฉพาะ super admin) + หน้า /admin/users-ui
│   │   ├── images.py           # อัปโหลดภาพ, ดู/ลบภาพ, ประมวลผลใหม่, แก้ค่าด้วยมือ
│   │   ├── ocr_jobs.py         # คิว OCR: จองงาน, ส่งผล, เติมจุดทศนิยม
│   │   ├── meters.py           # ผลลัพธ์จริง/ทดสอบ, ค่าย้อนหลังให้ Rule Base, Push Issues
│   │   ├── device_config.py    # ตารางเวลาถ่ายภาพ, CFO API Key, token รายเครื่อง, เพิ่มมิเตอร์ + หน้าเว็บหลัก
│   │   └── health.py           # /health
│   └── static/
│       ├── device_config_ui.html   # หน้าเว็บหลักของผู้ดูแล (Meter config)
│       └── users_ui.html           # หน้าจัดการผู้ดูแล (super admin)
├── db/
│   └── init.sql                # สร้างตารางทั้งหมด + ALTER สำหรับเวอร์ชันใหม่ (รันซ้ำได้)
├── scripts/
│   └── create_user.py          # สร้างบัญชีผู้ใช้จาก command line
├── Dockerfile
├── docker-compose.yml          # ใช้บนเซิร์ฟเวอร์จริง
├── docker-compose.local.yml    # ใช้ทดสอบในเครื่อง
└── requirements.txt
```

---

## 4. การติดตั้งครั้งแรก

### 4.1 สิ่งที่ต้องมี
- Docker และ Docker Compose
- ฐานข้อมูล PostgreSQL/TimescaleDB ที่รันอยู่ (บนเซิร์ฟเวอร์จริงเป็นคอนเทนเนอร์ชื่อ `timescaledb` บนเครือข่าย Docker เดียวกัน)
- เครือข่าย Docker ที่กำหนดใน `docker-compose.yml` (ต้องสร้างไว้ก่อน เพราะประกาศเป็น `external`)

### 4.2 สร้างไฟล์ `.env`
คัดลอกจาก `.env.example` แล้วใส่ค่าจริง **ห้าม commit ไฟล์ `.env` ขึ้น git**

ค่าที่ต้องตั้งอย่างน้อย:
```env
DB_USER=...
DB_PASSWORD=...
DB_NAME=cfo_iot
JWT_SECRET=<สุ่มยาวอย่างน้อย 32 ตัวอักษร>
DEVICE_API_KEY=<กุญแจกลางของอุปกรณ์ (ช่วงเปลี่ยนผ่าน)>
DEVICE_API_KEY_USERNAME=esp32
OCR_CLIENT_KEY=<กุญแจของระบบ OCR>
OCR_CLIENT_KEY_USERNAME=ocr-service
EXTERNAL_API_BASE_URL=https://<โดเมน CFO Platform>/engineer-api
EXTERNAL_KEY_ENCRYPTION_KEY=<กุญแจ Fernet>
```
สร้างกุญแจ Fernet:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
> ⚠️ ถ้าเปลี่ยน `EXTERNAL_KEY_ENCRYPTION_KEY` ภายหลัง CFO API Key ที่บันทึกไว้ทั้งหมดจะถอดรหัสไม่ได้ ต้องใส่ key ของทุกมิเตอร์ใหม่

### 4.3 สร้างตารางในฐานข้อมูล
`db/init.sql` เขียนให้**รันซ้ำได้** (ใช้ `IF NOT EXISTS` และ `DROP CONSTRAINT IF EXISTS` ทั้งหมด) รันครั้งแรกบนฐานข้อมูลว่าง:
```bash
psql -h <host> -U <user> -d <dbname> -v ON_ERROR_STOP=1 -f db/init.sql
```

### 4.4 รันระบบ
```bash
sudo docker compose up -d --build
curl -s http://localhost:<พอร์ต>/iot/health      # ต้องได้ {"status":"ok","db":true}
```

### 4.5 สร้างบัญชีผู้ใช้
```bash
# super admin คนแรก (จัดการบัญชีอื่นได้)
sudo docker compose exec ocr-meter-store python -m scripts.create_user --username superadmin --password '<รหัสผ่าน>' --super-admin

# บัญชีของอุปกรณ์และระบบ OCR (ชื่อต้องตรงกับ DEVICE_API_KEY_USERNAME / OCR_CLIENT_KEY_USERNAME)
sudo docker compose exec ocr-meter-store python -m scripts.create_user --username esp32 --password '<สุ่ม>' --device
sudo docker compose exec ocr-meter-store python -m scripts.create_user --username ocr-service --password '<สุ่ม>' --admin --device
```
ผู้ดูแลคนอื่นสร้างจากหน้าเว็บ "จัดการผู้ดูแล" ได้เลย

### 4.6 ทดสอบในเครื่อง (ไม่ต้องใช้ฐานข้อมูลจริง)
ใช้ `docker-compose.local.yml` ซึ่งมีฐานข้อมูลของตัวเอง แล้วใช้ Postman collection ในโฟลเดอร์เอกสารของโครงงาน หรือ Swagger ที่ `http://localhost:<พอร์ต>/iot/docs`

---

## 5. ค่าตั้งค่า (ตัวแปรสภาพแวดล้อม)

ทุกค่าอยู่ใน `app/config.py` ตั้งผ่าน `.env` หรือ `environment:` ใน `docker-compose.yml` (ชื่อตัวแปรเป็นตัวพิมพ์ใหญ่) เปลี่ยนแล้วต้อง `sudo docker compose up -d --build`

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | `timescaledb`, 5432, -, -, `cfo_iot` | การเชื่อมต่อฐานข้อมูล |
| `BASE_PATH_PREFIX` | `/iot` | prefix ของทุก path (หลัง reverse proxy) |
| `JWT_SECRET` | - | กุญแจเซ็น token ผู้ดูแล **ต้องเปลี่ยน** |
| `JWT_EXPIRE_MINUTES` | 60 | อายุ token ผู้ดูแล (นาที) หมดแล้วหน้าเว็บเด้งไป login |
| `DEVICE_API_KEY` / `DEVICE_API_KEY_USERNAME` | ว่าง | กุญแจกลางของอุปกรณ์ (ใช้ได้กับทุกมิเตอร์) ลบทิ้งเมื่อทุกเครื่องใช้ token รายเครื่องแล้ว |
| `OCR_CLIENT_KEY` / `OCR_CLIENT_KEY_USERNAME` | ว่าง | กุญแจของระบบ OCR |
| `UPLOAD_DIR` | `/data/images` | โฟลเดอร์เก็บภาพในคอนเทนเนอร์ (ผูกกับ Docker volume) |
| `MAX_UPLOAD_MB` | 15 | ขนาดไฟล์ภาพสูงสุด |
| `IMAGE_GROUP_WINDOW_SECONDS` | 60 | ภาพที่มาห่างกันไม่เกินนี้นับเป็นกลุ่มเดียว |
| `IMAGE_GROUP_SIZE` | 3 | จำนวนภาพต่อกลุ่มเมื่อมิเตอร์ไม่ได้ตั้ง `photo_count` |
| `GROUP_SWEEP_INTERVAL_SECONDS` | 5 | รอบตรวจกลุ่มที่หมดเวลารอ |
| `DAILY_GROUP_LIMIT_ENABLED` | `true` | `true` = ภาพจริงเข้าคิว OCR ได้วันละ 1 ชุดต่อมิเตอร์ ชุดถัดไปถูก drop / `false` = ทุกชุดเข้าคิว |
| `MAX_OCR_ATTEMPTS` | 5 | จำนวนครั้งที่ระบบ OCR ลองงานเดียวได้ก่อนเป็น failed |
| `READING_DECIMALS_ELECTRIC` / `_GAS` / `_WATER` | 1 / 3 / 3 | จำนวนหลักทศนิยมที่เติมให้ เมื่อระบบ OCR ส่งค่ามาโดยไม่มีจุด |
| `OCR_HISTORY_INCLUDE_ANOMALIES` | `true` | ค่าย้อนหลังที่ส่งให้ Rule Base รวมค่าผิดปกติด้วย (ดู 7.3) |
| `EXTERNAL_API_BASE_URL` | ว่าง | URL ของ CFO Platform (ต้องเป็น `https://`) |
| `EXTERNAL_KEY_ENCRYPTION_KEY` | ว่าง | กุญแจ Fernet ที่ใช้เข้ารหัส CFO API Key |
| `EXTERNAL_PUSH_SWEEP_INTERVAL_SECONDS` | 30 | รอบของงานเบื้องหลังที่ส่งค่าค้างไป CFO |

---

## 6. ผู้ใช้ สิทธิ์ และการยืนยันตัวตน

### 6.1 ระดับสิทธิ์

| ระดับ | ใช้ทำอะไร | ยืนยันตัวตนด้วย |
|---|---|---|
| **super admin** | ทุกอย่างของผู้ดูแล + จัดการบัญชีผู้ดูแล + ออก/เพิกถอน token อุปกรณ์ | JWT (login) |
| **ผู้ดูแล (admin)** | หน้าเว็บ, ตารางเวลา, CFO API Key, ดูผลลัพธ์ | JWT (login) |
| **อุปกรณ์** | อัปโหลดภาพ, ขอตารางเวลา **ของมิเตอร์ตัวเองเท่านั้น** | token รายเครื่อง `dt_...` หรือกุญแจกลาง |
| **ระบบ OCR** | จองงาน, ดึงภาพ, ดึงค่าย้อนหลัง, ส่งผล | `X-OCR-Key` |

กฎสำคัญ:
- ลบบัญชีตัวเองไม่ได้ (403), ลบ super admin คนสุดท้ายไม่ได้ (409), ลบบัญชีอุปกรณ์จากหน้าเว็บไม่ได้
- เปลี่ยนรหัสผ่านต้องใส่รหัสเดิม ยกเว้น super admin รีเซ็ตให้คนอื่น
- ลบบัญชีแล้ว token ที่ออกไปใช้ไม่ได้ทันที (ระบบโหลดผู้ใช้จากฐานข้อมูลทุกคำขอ) แต่**เปลี่ยนรหัสผ่านไม่ได้ทำให้ token เดิมหมดอายุ**

### 6.2 การส่งกุญแจ
- **ผู้ดูแล:** `Authorization: Bearer <JWT>`
- **อุปกรณ์:** `X-Device-Key: <dt_... หรือกุญแจกลาง>` (เฟิร์มแวร์ส่ง `Authorization: Bearer` ด้วยก็ได้)
- **ระบบ OCR:** `X-OCR-Key: <OCR_CLIENT_KEY>`

### 6.3 token รายเครื่อง
- **ออก/เพิกถอน:** super admin กดในหน้าตั้งค่ามิเตอร์ (มิเตอร์ต้องมีอยู่ในระบบก่อน ใช้ปุ่ม "+ ใหม่")
- **รูปแบบ:** ขึ้นต้น `dt_` + สุ่ม 32 ไบต์ แสดงให้เห็น**ครั้งเดียว**ตอนออก ฐานข้อมูลเก็บเฉพาะ SHA-256
- **ผูกกับมิเตอร์:** ใช้กับมิเตอร์อื่น = 403, token ผิดหรือถูกเพิกถอน = 401
- **"ใช้ล่าสุด":** อัปเดตทุกครั้งที่มีคนใช้ token แม้คำขอจะถูกปฏิเสธทีหลัง ใช้ดูได้ว่าเครื่องใช้ token ไหนอยู่
- **ไม่หมดอายุ:** ต้องกด "ออก token ใหม่" เอง token เดิมใช้ไม่ได้ทันที

---

## 7. การทำงานหลักทีละขั้น

### 7.1 อุปกรณ์ส่งภาพ (`POST /iot/images/upload`)

**ชื่อไฟล์** (เซิร์ฟเวอร์อ่านรหัสมิเตอร์และเวลาถ่ายจากชื่อไฟล์ ไม่ใช่จากเวลาที่ได้รับ):
```
{meter_id}_{YYYYMMDD}_{HHMMSS}_{ลำดับ}.jpg      เช่น ELE-000002_20261030_061501_01.jpg
{meter_id}_NOTIME_{ms}_{ลำดับ}.jpg               ภาพที่ถ่ายก่อนเครื่องซิงก์เวลาได้ (ใช้เวลาที่เซิร์ฟเวอร์ได้รับแทน)
```
- เวลาในชื่อไฟล์เป็น**เวลาไทย**
- ชื่อไฟล์ผิดรูปแบบ → **400**
- ตัวอักษรนำหน้ารหัสบอกประเภท: `ELE-` ไฟฟ้า, `WAT-` น้ำประปา, `GAS-` แก๊ส

**พารามิเตอร์ใน URL**

| พารามิเตอร์ | ค่า | ใช้ทำอะไร |
|---|---|---|
| `net_mode` | `WiFi` / `4G` | บันทึกใน `esp32_upload_log` |
| `carrier` | รหัสเครือข่าย | บันทึกเฉย ๆ |
| `wakeup_reason` | `timer` (Carbon ตามรอบ), `interval` (AlwaysOn ตามรอบ) → **ภาพจริง** / `manual`, `boot`, อื่น ๆ → **ภาพทดสอบ** | แยกภาพจริงกับทดสอบ |
| `power_mode` | `battery` / `always_on` (ไม่ส่ง = battery) | บันทึกประเภทเครื่อง ใช้ตัดสินรูปแบบตารางเวลา |
| `fw_version` | เช่น `1.13`, `2.3-AlwaysOn` | แสดงในหน้าเว็บ |

**ภาพทดสอบ:** ระบบเติม `_Test` ท้ายชื่อไฟล์ ผลไปที่ `ocr_meter_test` ไม่ถูกส่งไป CFO และไม่ถูกใช้เป็นค่าย้อนหลังของ Rule Base

**การจัดกลุ่ม** (`app/grouping.py`):
- ภาพของมิเตอร์เดียวกันที่มาห่างกันไม่เกิน `IMAGE_GROUP_WINDOW_SECONDS` อยู่กลุ่มเดียวกัน ชื่อกลุ่มเช่น `E12`, `W3`, `G5`
- ครบตามจำนวนภาพ (`photo_count` ของมิเตอร์ หรือ `IMAGE_GROUP_SIZE`) → สร้างงาน OCR ทันที
- ไม่ครบแต่หมดเวลารอ → งานเบื้องหลังปิดกลุ่มแล้วสร้างงานจากภาพที่มี
- ประเภทภาพจริง/ทดสอบของกลุ่มตัดสินจาก**ภาพแรก**

**กฎวันละ 1 ชุด** (`DAILY_GROUP_LIMIT_ENABLED=true`): ภาพจริงชุดที่ 2 ขึ้นไปของวันเดียวกัน (ตามเวลาไทย) ถูกตั้งเป็น `dropped` ไม่เข้าคิว OCR (ภาพยังเก็บไว้) ภาพทดสอบไม่ติดกฎนี้

### 7.2 ตารางเวลาถ่ายภาพ (`device_config`)

| `schedule_mode` | ความหมาย | ฟิลด์ที่ใช้ | แบต | เปิดตลอด |
|---|---|---|---|---|
| 0 | ถ่ายทุกวัน (Daily) | `date1` (ชั่วโมง นาที) | ✔ | ✔ |
| 1 | ถ่ายตามวันที่ของเดือน (Fix date) | `date1`, `date2` (วัน ชั่วโมง นาที) | ✔ | ✔ |
| 2 | ทุก N นาที | `interval_minutes` | ✘ | ✔ |
| 3 | ตามรายการเวลา | `times` (เช่น `["08:00","17:00"]` สูงสุด 8) | ✘ | ✔ |

- `date1`, `date2` เป็น `[วัน, เดือน, ปี, ชั่วโมง, นาที]` ใช้เฉพาะวัน ชั่วโมง นาที
  - **วันที่จำกัด 0–28:** เครื่องแบตตั้งเวลาปลุกตามวันที่ตรง ๆ ถ้าตั้ง 29–31 เดือนที่ไม่มีวันนั้นจะไม่ถ่ายเลย
  - `date1` วัน = 0 หมายถึงวันที่ 26, `date2` ทั้งหมดเป็น 0 = ไม่มีรอบที่สอง
- `photo_delay` 0–3600 วินาที, `photo_count` 1–10
- บันทึกโหมด 2 หรือ 3 ให้มิเตอร์ที่ไม่ใช่ `always_on` → **422** (เฟิร์มแวร์ Carbon ไม่ได้กันค่าเหล่านี้)
- มิเตอร์ที่ไม่เคยตั้งค่า ได้ค่าเริ่มต้น (Fix date วันที่ 26 เวลา 08:00) และป้าย "ค่าเริ่มต้น"

**คำตอบของ `GET /iot/devices/config`** ขึ้นกับประเภทเครื่องที่บันทึกไว้:
- **battery / ไม่ทราบ:** รูปแบบเดิม (ไม่มี `power_mode`) เครื่อง Carbon ไม่ต้องแก้อะไร
- **always_on:** มี `"power_mode": "always_on"` และส่ง `interval_minutes`, `times` ไปด้วยเสมอ ถ้าไม่มี `power_mode` เครื่อง AlwaysOn จะรับแค่ `photo_count`, `photo_delay` แล้วใช้รอบถ่ายของเฟิร์มแวร์เอง

ประเภทเครื่องมาจาก `power_mode` ที่เครื่องส่งตอนอัปโหลดล่าสุด หรือจากที่ผู้ดูแลเลือกตอนกด "+ ใหม่" (ถ้าเครื่องยังไม่เคยส่งรูป)

### 7.3 คิว OCR (`/iot/admin/images/ocr/...`)

ขั้นตอนที่ระบบ OCR ทำ:
1. `GET /iot/admin/images/ocr?job_status=queued` ดูงานที่รอ
2. `POST .../{job_id}/claim` จองงาน (งานเป็น `processing`) ได้ URL ของภาพทุกภาพในกลุ่ม
3. `GET /iot/admin/meters/{meter_id}/ocr-readings?limit=3&only_successful=true` ดึงค่าย้อนหลัง (เฉพาะภาพจริง)
4. `POST .../{job_id}/result` (ภาพจริง) หรือ `.../result-test` (ภาพทดสอบ) ส่งผล
5. ถ้าทำงานพลาด `POST .../{job_id}/fail` เพื่อคืนงานหรือนับครั้งที่พลาด

**ผลที่ระบบ OCR ส่งมา**

| `error_type` | ความหมาย | ต้องมี `ocr_reading` |
|---|---|---|
| 0 | อ่านสำเร็จ | ✔ |
| 1 | หาตัวเลข/มิเตอร์ไม่เจอ หรืออ่านไม่ได้ | ✘ |
| 2 | ระบบ OCR ผิดพลาด | ✘ |
| 3 | อ่านได้แต่ค่าผิดปกติ (Rule Base ตีกลับ) | ✔ |

`ocr_engine` คือรหัสเส้นทางการอ่านของระบบ OCR (เช่น 0 = CNN อ่านได้เอง, 110/120 = ต้องใช้ Gemini) ดูความหมายเต็มได้ที่ `GET /iot/admin/meters/ocr-engine-meaning`

**การเติมจุดทศนิยม** (`app/routers/ocr_jobs.py`):
- ระบบ OCR **ต้องส่งค่าเป็นข้อความตามที่อ่านได้** (เช่น `"53873"`, `"02875.335"`) ไม่แปลงเป็น float ก่อนส่ง
- ข้อความ**ไม่มีจุด** → เติมจุดตาม `READING_DECIMALS_*` ของประเภทมิเตอร์ (`"53873"` ไฟฟ้า → 5387.3)
- ข้อความ**มีจุดแล้ว** → ใช้ตามเดิม (`"5387.0"` ≠ `"5387"`)
- ข้อความต้นฉบับเก็บใน `ocr_reading_raw` ไว้เป็นสถิติ ค่าที่ใช้งานอยู่ใน `ocr_reading`
- `nan`, `inf`, ข้อความที่ไม่ใช่ตัวเลข → **422**

**ค่าย้อนหลังที่ส่งให้ Rule Base** (`app/routers/meters.py`, `ocr-readings`):
- `OCR_HISTORY_INCLUDE_ANOMALIES=true` (ค่าเริ่มต้น): ค่าล่าสุดที่มีตัวเลขจริง (> 0) ทั้งอ่านสำเร็จ (0) และค่าผิดปกติ (3) ข้ามค่าที่อ่านไม่ได้และค่า 0
- `false`: เฉพาะอ่านสำเร็จแบบเดิม
- เหตุผล: ถ้าค่าที่อ่านผิดได้สถานะ "อ่านสำเร็จ" (เช่น อ่าน 3 เป็น 8 ได้ 5889.2) แบบเดิมจะใช้ค่านั้นเป็นฐานเทียบตลอดไป ค่าที่อ่านถูกหลังจากนั้นถูกตีผิดปกติทั้งหมด
- ตรรกะของ Rule Base อยู่ในระบบ OCR (`meter_validator.py`) ไม่ได้อยู่ใน repo นี้

### 7.4 ส่งค่าไป CFO Platform (`app/external_push.py`)

**เงื่อนไขการส่ง**
- เฉพาะผลลัพธ์จริง (`ocr_meter`) ของมิเตอร์ที่มี CFO API Key
- **เฉพาะค่าที่ถ่ายหลังบันทึก API Key ครั้งแรก** ค่าที่ถ่ายก่อนหน้า (ข้อมูลช่วงทดลอง) ได้สถานะ `skipped_before_key` ไม่ส่งอัตโนมัติ เปลี่ยน key ภายหลังจุดตัดไม่เลื่อน ผู้ดูแลสั่งส่งรายการเก่าเองได้

**ข้อมูลที่ส่ง** (`POST {EXTERNAL_API_BASE_URL}/external/v1/meter-readings`, multipart, header `X-API-Key`)
- `image`: ภาพหลักของกลุ่ม (ย่อให้ถ้าเกิน 10 MB)
- `externalRef`: `{meter_id}-{YYYYMMDD}-{HHmm}` ซ้ำนาทีเดียวกันต่อท้าย `-2`, `-3`
- `readingDate`: วันที่ถ่ายตามเวลาไทย
- `aiValue`: ค่าที่อ่านได้ **ตัด**ทศนิยมเหลือ 2 ตำแหน่ง (ไม่ปัดขึ้น) ไม่ส่งเมื่ออ่านไม่ได้
- `confidence`: ถ้ามี

**สถานะการส่ง (`push_status`)**

| สถานะ | ความหมาย |
|---|---|
| `not_pushed` | ยังไม่เคยส่ง (มิเตอร์ไม่มี key) |
| `pending` | ส่งสำเร็จ CFO รับแล้ว รอเจ้าหน้าที่ตรวจ |
| `failed_retryable` | ส่งไม่สำเร็จชั่วคราว (5xx, timeout) จะส่งซ้ำหลัง 30 วิ → 2 นาที → 10 นาที → 1 ชั่วโมง |
| `failed_permanent` | ข้อมูลของรายการนั้นผิด (400 ฯลฯ) ไม่ส่งซ้ำ ต้องแก้เอง |
| `blocked_auth` | key ใช้ไม่ได้ (401/403) หยุดส่งทั้งมิเตอร์จนกว่าจะใส่ key ใหม่ |
| `skipped_before_key` | ถ่ายก่อนบันทึก key ครั้งแรก ไม่ส่งอัตโนมัติ |

รายการที่ต้องตรวจ (`failed_permanent`, `blocked_auth`, มิเตอร์ไม่ตรงทะเบียน `meterMatched=false`) ดูได้จากปุ่ม **Push Issues** ในหน้าเว็บ

### 7.5 หน้าเว็บผู้ดูแล
- **`/iot/admin/device-config-ui`:** หน้าหลัก รายการมิเตอร์ (กรองตามประเภทมิเตอร์และประเภทเครื่อง), ตารางเวลา, CFO API Key, token อุปกรณ์, ผลลัพธ์จริง/ทดสอบล่าสุด, Push Issues, เพิ่มมิเตอร์ใหม่
  - อัปเดตอัตโนมัติทุก 15 วินาที (ไม่แตะฟอร์มที่กำลังแก้ หยุดเมื่อเปิดหน้าต่างอื่นหรือสลับแท็บ)
  - แสดงค่าตามจำนวนหลักทศนิยมของประเภทมิเตอร์ (ไฟ 1, แก๊ส/น้ำ 3)
- **`/iot/admin/users-ui`:** จัดการบัญชีผู้ดูแล (เฉพาะ super admin)
- **`/iot/docs`:** Swagger ของ API ทั้งหมด

---

## 8. ฐานข้อมูล

| ตาราง | เก็บอะไร |
|---|---|
| `users` | บัญชีผู้ดูแล อุปกรณ์ ระบบ OCR (`is_admin`, `is_super_admin`, `is_device`) |
| `device_config` | ตารางเวลาของแต่ละมิเตอร์, ประเภทเครื่อง (`power_mode`, `fw_version`), ข้อมูลอ้างอิงจาก CFO |
| `device_tokens` | token รายเครื่อง (เก็บแฮช) 1 มิเตอร์ 1 token |
| `external_api_keys` | CFO API Key (เข้ารหัส Fernet), เวลาที่ตั้งครั้งแรก (`created_at` = จุดตัดการส่ง) |
| `images` | ภาพทุกภาพที่รับ (กลุ่ม, สถานะ OCR, ชื่อไฟล์, เวลาถ่าย) |
| `ocr_jobs` | คิวงาน OCR (queued / processing / done / failed / dropped) |
| `ocr_meter` | ผลลัพธ์ของภาพจริง + สถานะการส่ง CFO |
| `ocr_meter_test` | ผลลัพธ์ของภาพทดสอบ (โครงสร้างเดียวกัน) |
| `esp32_upload_log` | บันทึกการส่งภาพของอุปกรณ์ (เครือข่าย, สาเหตุการตื่น) เฉพาะคำขอที่สำเร็จ |
| `error_type`, `ocr_engine_meaning` | ตารางอ้างอิงความหมายของรหัส |
| `dataset` | ตำแหน่งไฟล์ภาพ ใช้รวบรวมชุดข้อมูลฝึกโมเดล |

ไฟล์ภาพอยู่ใน Docker volume (ในคอนเทนเนอร์คือ `/data/images`) บนเครื่องโฮสต์ดูได้ที่ `/var/lib/docker/volumes/<ชื่อ volume>/_data/`

---

## 9. รายการ API

ทุก path มี prefix `/iot` (ค่า `BASE_PATH_PREFIX`) รายละเอียดพารามิเตอร์ดูที่ `/iot/docs`

| กลุ่ม | Method และ Path | สิทธิ์ |
|---|---|---|
| ระบบ | `GET /health` | ไม่ต้อง |
| เข้าสู่ระบบ | `POST /login`, `GET /me`, `PUT /me/password` | ผู้ดูแล |
| อุปกรณ์ | `POST /images/upload`, `GET /devices/config?meter_id=` | อุปกรณ์ |
| คิว OCR | `GET /admin/images/ocr`, `POST /admin/images/ocr/{id}/claim`, `.../result`, `.../result-test`, `.../fail` | ระบบ OCR / ผู้ดูแล |
| ภาพ | `GET /admin/images`, `GET /admin/images/{id}`, `GET .../{id}/file`, `GET /admin/images-by-filename/{name}/file`, `DELETE /admin/images/{id}`, `POST .../{id}/reprocess`, `PUT .../{id}/ocr-manual` | ผู้ดูแล / ระบบ OCR |
| ผลลัพธ์ | `GET /admin/meters/ocr-meter`, `GET /admin/meters/ocr-meter-test`, `GET /admin/meters/{id}/ocr-readings`, `GET /admin/meters/{id}/history`, `GET /admin/meters/esp32-upload-log`, `GET /admin/meters/ocr-engine-meaning` | ผู้ดูแล / ระบบ OCR |
| CFO | `PUT/GET /admin/device-config/{id}/external-api-key`, `POST .../verify-external`, `POST /admin/meters/ocr-meter/{id}/push-external`, `GET /admin/meters/push-issues` | ผู้ดูแล |
| มิเตอร์ | `GET /admin/device-config`, `POST /admin/device-config` (เพิ่มมิเตอร์), `GET/PUT/DELETE /admin/device-config/{id}` | ผู้ดูแล |
| token อุปกรณ์ | `GET /admin/device-config/{id}/device-token` (ผู้ดูแล), `POST`/`DELETE` (super admin) | |
| บัญชีผู้ดูแล | `GET/POST /admin/users`, `DELETE /admin/users/{id}`, `PUT /admin/users/{id}/password` | super admin |
| หน้าเว็บ | `GET /admin/device-config-ui`, `GET /admin/users-ui` | เปิดได้ (login ในหน้า) |

> `POST /register` ถูกจำกัดให้เฉพาะผู้ดูแลแล้ว (เคยเปิดสาธารณะ) ให้สร้างบัญชีผ่านหน้า "จัดการผู้ดูแล" หรือ `scripts/create_user.py`

---

## 10. การอัปเดตเวอร์ชันบนเซิร์ฟเวอร์ (deploy)

### 10.1 ขั้นตอนปกติ
```bash
cd ~/docker_ocr_meter/OCR_Meter        # โฟลเดอร์ของ repo บนเซิร์ฟเวอร์

# 1) เก็บ log ไว้ก่อน — log จะหายเมื่อสร้างคอนเทนเนอร์ใหม่
sudo docker logs -t ocr-meter-store > ~/log_$(date +%Y%m%d_%H%M).txt 2>&1

# 2) ดึงโค้ด
git pull

# 3) ถ้าเวอร์ชันนี้มีคอลัมน์/ตารางใหม่ → เพิ่มในฐานข้อมูล "ก่อน" build (ดู 10.2)

# 4) build และรันใหม่
sudo docker compose up -d --build

# 5) ตรวจ
curl -s http://localhost:<พอร์ต>/iot/health
sudo docker logs --since 2m ocr-meter-store 2>&1 | grep -iE "error|traceback"
```
แล้วในเบราว์เซอร์กด Ctrl + Shift + R (Mac: Cmd + Shift + R) เพื่อโหลดหน้าเว็บใหม่

### 10.2 ⚠️ เพิ่มคอลัมน์ก่อน build เสมอ
ถ้าโค้ดใหม่อ่านคอลัมน์ที่ฐานข้อมูลยังไม่มี ทั้งหน้าเว็บและอุปกรณ์จะได้ **500** (`KeyError` หรือ `column ... does not exist`) จนกว่าจะเพิ่มคอลัมน์

วิธีเพิ่มคอลัมน์โดยใช้การเชื่อมต่อของคอนเทนเนอร์เอง (ไม่ต้องรู้รหัสผ่านฐานข้อมูล):
```bash
sudo docker compose exec -T ocr-meter-store python - <<'EOF'
import asyncio, sys
sys.path.insert(0, "/srv")
from app import db
SQL = [
    "ALTER TABLE <ตาราง> ADD COLUMN IF NOT EXISTS <คอลัมน์> <ชนิด>",
]
async def main():
    await db.connect()
    for q in SQL:
        await db.pool().execute(q)
        print("done:", q)
    await db.disconnect()
asyncio.run(main())
EOF
```
คำสั่ง ALTER ทุกตัวที่ระบบต้องใช้อยู่ท้าย `db/init.sql` อยู่แล้ว คัดลอกมาใช้ได้ ทุกคำสั่งรันซ้ำได้

**ถ้าลืมแล้วระบบ error:** รันคำสั่งเพิ่มคอลัมน์ได้เลยตอนนั้น ไม่ต้อง build ใหม่ ระบบกลับมาทำงานทันที

### 10.3 ย้อนกลับเวอร์ชันก่อน
```bash
git log --oneline -5
git checkout <รหัส commit ก่อนหน้า> -- app db
sudo docker compose up -d --build
```
คอลัมน์ที่เพิ่มไปแล้วไม่ต้องลบ โค้ดเก่าไม่ได้ใช้ก็ไม่เป็นไร

### 10.4 ข้อควรระวังกับ git
- **อย่าใช้ `git add .` หรือ `git commit -a`** ให้ add เฉพาะไฟล์ที่แก้ (เคยมีไฟล์ใน `app/routers/` ถูกลบในเครื่องโดยไม่ตั้งใจ ถ้า commit ไปเซิร์ฟเวอร์จะพัง)
- เพิ่มใน `.gitignore`: `.env`, `.DS_Store`, `*.jpg`, `*.patch`
- ตรวจด้วย `git status` และ `git diff --stat` ก่อน commit ทุกครั้ง

---

## 11. คำสั่งที่ใช้บ่อย

เวลาใน log เป็น **UTC** (เวลาไทย = UTC + 7)

```bash
# log สด เฉพาะคำขอจากอุปกรณ์
sudo docker logs -f --tail 20 ocr-meter-store 2>&1 | grep -E "devices/config|images/upload"

# log ย้อนหลังตามช่วงเวลา (UTC)
sudo docker logs -t --since "2026-10-30T00:00:00" ocr-meter-store 2>&1 | grep -E "devices/config|images/upload"

# เส้นทางของภาพจริง 1 ชุด: อัปโหลด → จองงาน → ดึงค่าย้อนหลัง → ส่งผล
sudo docker logs -t --since 1h ocr-meter-store 2>&1 | grep -E "wakeup_reason=(timer|interval)|/claim|ocr-readings|/result"

# คำขอที่ถูกปฏิเสธ
sudo docker logs -t --since 1h ocr-meter-store 2>&1 | grep -E " 40[0-9] "

# error ของระบบ
sudo docker logs --since 1h ocr-meter-store 2>&1 | grep -iE "error|traceback|keyerror"

# คอนเทนเนอร์เริ่มเมื่อไร (log ก่อนเวลานี้หายแล้ว)
sudo docker inspect -f '{{.State.StartedAt}}' ocr-meter-store

# เช็กว่าคอนเทนเนอร์ใช้โค้ดเวอร์ชันใหม่ (ค้นคำที่มีเฉพาะในโค้ดใหม่)
sudo docker compose exec ocr-meter-store grep -c "<คำ>" app/<ไฟล์>.py
```

**ดูข้อมูลในฐานข้อมูลโดยไม่ต้องใช้ psql** (แก้ SQL ตามต้องการ):
```bash
sudo docker compose exec -T ocr-meter-store python - <<'EOF'
import asyncio, sys
sys.path.insert(0, "/srv")
from app import db
async def main():
    await db.connect()
    for r in await db.pool().fetch("""
        SELECT capture_date, capture_time, ocr_reading, ocr_reading_raw, error_type, ocr_engine, push_status
        FROM ocr_meter WHERE meter_id = 'ELE-000002'
        ORDER BY capture_date DESC, capture_time DESC LIMIT 5"""):
        print(dict(r))
    await db.disconnect()
asyncio.run(main())
EOF
```

> การลบข้อมูล (`DELETE`) ให้ดูแถวด้วย `SELECT` ก่อนทุกครั้ง แล้วลบด้วย `id` พร้อมเงื่อนไข `meter_id` กันลบผิดมิเตอร์ ลบแล้วกู้คืนไม่ได้

---

## 12. ปัญหาที่พบบ่อยและวิธีแก้

| อาการ | สาเหตุที่เป็นไปได้ | วิธีแก้ |
|---|---|---|
| หน้าเว็บ/อุปกรณ์ได้ **500** หลัง deploy, log มี `KeyError: '<ชื่อคอลัมน์>'` | ยังไม่ได้เพิ่มคอลัมน์ใหม่ | รันคำสั่งเพิ่มคอลัมน์ (10.2) ไม่ต้อง build ใหม่ |
| อุปกรณ์ได้ **401** | กุญแจผิด, token ถูกออกใหม่ทับ/เพิกถอน | ดู "ใช้ล่าสุด" ของ token เทียบเวลาใน log, ออก token ใหม่แล้วแฟลชเครื่อง |
| อุปกรณ์ได้ **403** | token ของมิเตอร์อื่น | รหัสมิเตอร์ในเครื่องอยู่ใน EEPROM ไม่ใช่ `DEFAULT_METER_ID` ต้องเข้าโหมดตั้งค่าเครื่องใหม่ |
| อุปกรณ์ได้ **400** ตอนอัปโหลด | ชื่อไฟล์ผิดรูปแบบ (เวลาเครื่องเพี้ยน, รหัสมิเตอร์ผิด) | ดู Serial Monitor ของเครื่อง |
| ภาพจริงชุดที่ 2 ของวันไม่ถูกอ่าน | กฎวันละ 1 ชุด (`dropped`) | ปกติ หรือตั้ง `DAILY_GROUP_LIMIT_ENABLED=false` |
| ภาพของ AlwaysOn ไปอยู่ในผลทดสอบ | เครื่องส่ง `wakeup_reason` ที่ไม่ใช่ `interval` | เช็กเฟิร์มแวร์ |
| ค่ามากกว่าจริง 10 / 1000 เท่า | ระบบ OCR ไม่ใส่จุดทศนิยม และจำนวนหลักตามประเภทไม่ตรงกับรุ่นมิเตอร์ | ดู `ocr_reading_raw`, ปรับ `READING_DECIMALS_*` (ดูข้อ 13) |
| ค่าที่อ่านถูกโดนตี "ค่าผิดปกติ" ต่อเนื่อง | ฐานเทียบเป็นค่าที่อ่านผิด | ตรวจ `OCR_HISTORY_INCLUDE_ANOMALIES=true` |
| "อ่านเลขไม่ได้" (`error_type 1`) | ภาพมืด/เบลอ/กล้องขยับ หรือระบบ OCR อ่านไม่ออก | เปิดดูภาพในการ์ด ถ้าภาพดีแต่อ่านไม่ได้ ส่งให้ทีม OCR |
| หน้าเว็บเด้งไป login เอง | token ผู้ดูแลหมดอายุ (`JWT_EXPIRE_MINUTES`) | login ใหม่ |
| ไม่ส่งไป CFO ทั้งที่ใส่ key แล้ว | ภาพถ่ายก่อนบันทึก key (`skipped_before_key`) หรือ `blocked_auth` | ดู Push Issues / สถานะในการ์ด |
| ภาพผ่าน 4G เสียเป็นแถบ | ข้อมูลหายระหว่าง ESP32 กับโมดูล 4G (ไม่ใช่เซิร์ฟเวอร์ — เซิร์ฟเวอร์เก็บไฟล์ตามไบต์ที่ได้รับทุกไบต์) | ฝั่งเฟิร์มแวร์ |

---

## 13. งานค้างและข้อจำกัดที่รู้อยู่แล้ว

### บั๊กฝั่งเซิร์ฟเวอร์ที่ยังไม่แก้ (ควรแก้ก่อนเปิดส่ง CFO เต็มรูปแบบ)
1. **งาน OCR ค้าง `processing`** ถ้าระบบ OCR ล่มหลังจองงาน ไม่มีกลไกคืนงานเข้าคิว ต้องเพิ่มคอลัมน์ `claimed_at` ใน `ocr_jobs` และงานเบื้องหลังที่คืนงานที่ค้างเกินเวลา
2. **อัปโหลดชื่อไฟล์ซ้ำ** สร้างแถวใหม่และเขียนทับไฟล์ภาพเดิม ควรตอบข้อมูลภาพเดิมกลับแทน
3. **แก้ค่าด้วยมือ (`ocr-manual`) กับงานที่ failed** ตอบ 200 แต่ไม่บันทึก
4. **JWT ตรวจเฉพาะ `uid`** ไม่ตรวจ `sub`
5. **ค่าทศนิยมยาวในฐานข้อมูล** (เช่น `5397.3999…`) เพราะบันทึกผ่าน float ไม่กระทบการแสดงผลและการส่ง CFO ควรบันทึกเป็น Decimal
6. **ไม่มีการจำกัดจำนวนครั้งที่ login ผิด** ทำที่ reverse proxy ได้
7. **บันทึกเหตุผลที่ปฏิเสธอุปกรณ์** log มีแค่ 401/403 ไม่บอกสาเหตุ

### ข้อจำกัดของการออกแบบ
- **จำนวนหลักทศนิยมตั้งตามประเภท ไม่ใช่รายมิเตอร์:** มิเตอร์ไฟฟ้าบางรุ่นไม่มีทศนิยม (เช่น Mitsubishi) และมิเตอร์น้ำบางรุ่นแสดงทศนิยมเป็นเข็มซึ่งระบบ OCR ไม่ได้อ่าน ถ้าใช้มิเตอร์เหล่านี้ต้องเพิ่มการตั้งค่ารายมิเตอร์
- **ข้อมูลที่แก้บน CFO ไม่ย้อนกลับมา:** ระบบนี้เก็บค่าดิบของ OCR ไว้เป็นสถิติโดยตั้งใจ ค่าที่ถูกต้องอยู่บน CFO Platform จับคู่กันได้ด้วย `externalRef`
- **การส่งทันทีกับงานเบื้องหลังอาจส่งรายการเดียวกันซ้ำ:** ใช้ `externalRef` เดิม CFO ตอบ `duplicate` ไม่สร้างรายการซ้ำ

### เรื่องที่อยู่นอก repo นี้
- **ระบบ OCR:** ภาพที่อ่านไม่ได้ควรส่ง `error_type 1` ไม่ใช่ 3 พร้อมค่า 0, Rule Base ควรเทียบค่ามัธยฐานและคิดอัตราการใช้ต่อวัน
- **เฟิร์มแวร์:** เปลี่ยนเป็น HTTPS, แยก token ออกจาก `setting.h` ไม่ให้ขึ้น git

---

## 14. ความปลอดภัย
- **ห้าม commit:** `.env`, token อุปกรณ์ (`dt_...`), CFO API Key, รหัสผ่าน, ภาพหน้าจอที่เห็นค่าเหล่านี้
- **token หรือ key หลุด:** ออกใหม่ทันที (token อุปกรณ์: ปุ่ม "ออก token ใหม่" / CFO API Key: ขอใหม่จาก CFO แล้วบันทึกทับ)

---

ผู้พัฒนาเริ่มต้น: นายเกริกไกร จอดนอก (นักศึกษาสหกิจศึกษา วิศวกรรมคอมพิวเตอร์ มหาวิทยาลัยรังสิต) ปีการศึกษา 2569
