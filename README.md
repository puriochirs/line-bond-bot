# 📱 Line Bond Info Chatbot — วิธี Setup

## สิ่งที่ต้องมี
- Python 3.11+
- Line Developer Account (ฟรี)
- Render.com Account (ฟรี)

---

## ขั้นตอนที่ 1 — สร้าง Line Bot

1. ไปที่ https://developers.line.biz → Log in
2. กด **Create Provider** → ตั้งชื่อ (เช่น BondBot)
3. กด **Create a Messaging API channel**
   - Channel name: Bond Info Bot
   - Category: Finance
4. เข้า Channel → แท็บ **Messaging API**
   - เปิด **Allow bot to join group chats** (optional)
   - **Disable** Auto-reply messages
   - **Disable** Greeting messages
5. คัดลอก **Channel Secret** (แท็บ Basic settings)
6. กด **Issue** Channel Access Token → คัดลอก

---

## ขั้นตอนที่ 2 — Deploy บน Render.com

1. Push โค้ดขึ้น GitHub (หรือ GitLab)
2. ไปที่ https://render.com → New → **Web Service**
3. เชื่อม GitHub repo → เลือก repo นี้
4. ตั้งค่า:
   - **Environment**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --workers 2 --timeout 60`
5. เพิ่ม Environment Variables:
   ```
   LINE_CHANNEL_SECRET     = (ค่าจาก Step 1)
   LINE_CHANNEL_ACCESS_TOKEN = (ค่าจาก Step 1)
   ```
6. กด **Create Web Service** → รอ deploy (~2-3 นาที)
7. คัดลอก URL เช่น `https://bond-bot-xxxx.onrender.com`

---

## ขั้นตอนที่ 3 — ตั้ง Webhook URL

1. กลับไปที่ Line Developer Console → Channel ของคุณ
2. แท็บ **Messaging API** → Webhook URL
3. ใส่: `https://bond-bot-xxxx.onrender.com/webhook`
4. กด **Verify** → ต้องขึ้น Success ✅
5. เปิด **Use webhook** = ON

---

## ขั้นตอนที่ 4 — ทดสอบ

- Scan QR Code จาก Line Developer Console
- Add เป็นเพื่อน
- พิมพ์: `PTT` หรือ `CPALL`
- Bot จะตอบกลับด้วยข้อมูลหุ้นกู้

---

## โครงสร้างไฟล์
```
line-bond-bot/
├── app.py          ← Webhook server (Flask)
├── scraper.py      ← ดึงข้อมูลจาก ThaiBMA
├── requirements.txt
├── Procfile        ← สำหรับ Render/Heroku
└── README.md
```

---

## หมายเหตุ
- Render Free tier จะ sleep หลัง 15 นาที ไม่มี request → response แรกอาจช้า ~30 วินาที
- หากต้องการ production จริง แนะนำใช้ Render Paid หรือ VPS
- ThaiBMA อาจมีการ block scraping → ถ้าพบปัญหาให้แจ้งเพื่อปรับ parser
