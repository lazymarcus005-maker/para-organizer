# Next Work — PARA Organizer

> อัปเดตล่าสุด: 2026-07-28
> สถานะอ้างอิงจากโค้ดจริง (ไม่ใช่ field `status` ใน improvement.json ซึ่งยังค้างเป็น `pending` ทั้งหมด)

## งานที่ทำเสร็จแล้ววันนี้

- แก้ `tests/test_mcp.py` — ชุด `ALL_TOOLS` ค้างที่ 10 tools อัปเดตเป็น 15 (Phase 1 เพิ่ม update/complete/delete/reclassify/ask)
- IMP-05: ต่อ `distill_note` เข้า `para_archive` → บันทึก `summary` ลง DB + log history
- IMP-04: เขียน `para_ask` ใหม่ — ของเดิมเรียก `_build_context` ผิด signature → เปลี่ยนเป็น `_hybrid_retrieve` + ข้อความ no-match ภาษาไทย + sources ครบ (note_id/title/relevance/para_category)
- ทั้ง suite ผ่าน: **180 passed**

## สถานะราย Phase (ตรวจจากโค้ดจริง)

| Phase | สถานะ |
|-------|-------|
| 0 — Foundation (F0-1 migration, F0-2 usage tracking) | เสร็จแล้ว |
| 1 — IMP-06 MCP tools, IMP-10 escalation, IMP-15 quick-capture | เสร็จแล้ว |
| 2 — IMP-01 auto-link, IMP-18 confidence routing, IMP-14 kanban | เสร็จแล้ว |
| 3 — IMP-02 weekly review, IMP-04 para_ask, IMP-05 distill, IMP-11 stale nudge, IMP-07 cron, IMP-13 voice, IMP-19 cost dashboard, IMP-16 graph | เสร็จแล้ว |

## งานที่ต้องทำต่อ (4 รายการ)

### 1. IMP-09 — Deadline snooze/reschedule (Telegram inline)
- **Lane:** claude-haiku · `app/integrations/telegram_bot.py`
- **สถานะ:** ยังไม่ทำ — `notify_deadline` (notifier.py) ส่งข้อความเตือนเฉยๆ ไม่มี inline keyboard
- **ต้องทำ:**
  - แนบ inline keyboard ตอนเตือน deadline: `+1d / +3d / +7d / Done`
  - callback handler ที่ telegram_bot.py:378 ยังไม่รองรับ `deadline:*` — ต้องเพิ่ม branch เรียก `para_update` / `para_complete`
  - ยืนยันผลกลับใน chat
- **Acceptance:** กดปุ่มใน Telegram → deadline/สถานะเปลี่ยนจริง
- **หมายเหตุ:** ดูแบบ `notify_stale` ที่มีปุ่ม Keep/Archive อยู่แล้วเป็นต้นแบบ

### 2. IMP-12 — Recurring notes
- **Lane:** claude-haiku · `app/scheduler.py`
- **สถานะ:** schema พร้อม (คอลัมน์ `notes.recurrence` จาก F0-1) แต่ยังไม่มี logic
- **ต้องทำ:**
  - เมื่อโน้ต recurring ถูก complete → สร้าง instance ใหม่ deadline ถัดไป
  - อ่าน field `recurrence` (JSON: {freq, interval, next_run})
  - รองรับ freq: daily/weekly/monthly
  - hook เข้า flow complete (MCP `para_complete` / Telegram Done)
- **Acceptance:** complete โน้ต recurring → เกิดโน้ตใหม่ deadline ถัดไปอัตโนมัติ

### 3. IMP-17 — Embedding backfill job
- **Lane:** claude-haiku · `app/scheduler.py`, `app/embed.py`
- **สถานะ:** schema พร้อม (คอลัมน์ `notes.embedding_status` default 'pending') แต่ไม่มี scheduler job
- **ต้องทำ:**
  - scheduler job หา notes ที่ `embedding_status='pending'`
  - สร้าง embedding + เขียน vector store + set `status='done'`
  - ทำเป็น batch เพื่อคุม cost
  - ลงทะเบียน job ใน `scheduler` (scheduler.py:373+)
- **Acceptance:** โน้ตเก่าถูก embed ครบ, semantic search เจอ

### 4. IMP-20 — Backup to cloud
- **Lane:** opencode-qwen · `app/routes/backup.py`
- **สถานะ:** backup ท้องถิ่นเสร็จแล้ว (create/list/restore/delete/download) แต่ยังอัปโหลดขึ้น cloud ไม่ได้
- **ต้องทำ:**
  - ต่อยอด `create_backup_file`: อัปโหลด dump ขึ้น object storage (Alibaba OSS / R2)
  - retention N วัน
  - config ผ่าน env (endpoint/bucket/key) — เพิ่มใน `app/config.py`
- **Acceptance:** รัน backup → ไฟล์ปรากฏบน bucket, log สำเร็จ

## งานบ้านที่ควรทำ (housekeeping)

- **อัปเดต `improvement.json`** — field `status` ของ task ที่เสร็จแล้ว (เกือบทั้งหมด) ยังเป็น `pending` ควรแก้เป็น `done` ให้ตรงกับความจริง เพื่อใช้ติดตามงานที่เหลือ (IMP-09, IMP-12, IMP-17, IMP-20) ได้ถูกต้อง
- **ตรวจ test coverage ของ IMP-09/IMP-12/IMP-17** — ยังไม่มี test รองรับ ควรเขียนพร้อมตอน implement

## สรุป

เหลืองานจริง 4 รายการ (IMP-09, IMP-12, IMP-17 เป็นของ claude-haiku / IMP-20 เป็นของ opencode-qwen)
ทุกตัวแตะไฟล์คนละชุด → ทำพร้อมกันแบบ parallel ได้ปลอดภัย
