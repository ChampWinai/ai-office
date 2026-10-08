# AI Office

ออฟฟิศ AI 3 ตำแหน่ง (BOSS หัวหน้า / DEV โปรแกรมเมอร์ / QA ตรวจงาน) ทำงานผ่าน [Ollama](https://ollama.com) บนหน้าเว็บ pixel art
DEV เขียนโค้ด → ระบบรันจริง → QA ตรวจ → ตีกลับแก้ได้ 3 รอบ → BOSS สรุป และเขียน/แก้ไฟล์ในโฟลเดอร์งานที่เลือกได้ (สำรองของเดิมใน `.office_bak/`)

## ใช้งาน
1. ติดตั้ง Ollama แล้วดึงโมเดล: `ollama pull qwen2.5:7b`, `qwen2.5-coder:7b` (งานยากใช้ `qwen2.5:14b`, `qwen2.5-coder:14b`) แก้ชื่อโมเดลที่ `roles.json`
2. `pip install -r requirements.txt`
3. แอปเดสก์ท็อป: `python office_app.py` หรือเว็บ: `python server.py` แล้วเปิด http://localhost:8000 (`MOCK=1` = ทดสอบโดยไม่ใช้โมเดล)
4. สร้าง .exe: `pip install pyinstaller` แล้ว `pyinstaller --noconfirm --windowed --name AIOffice --add-data "index.html;." --add-data "roles.json;." --add-data "rules.md;." office_app.py`

## ข้อควรระวัง
- โค้ดที่โมเดลเขียนถูกรันบนเครื่องจริง (จำกัดเวลา 10 วินาที ไม่มี sandbox) และแอปเขียนไฟล์ในโฟลเดอร์งานได้
- เซิร์ฟเวอร์ผูกกับ localhost และปฏิเสธคำขอข้ามเว็บ

กติกาของทีมอยู่ที่ `rules.md`, บทเรียนสะสมที่ `memory.md` (สร้างอัตโนมัติ)
