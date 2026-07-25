#!/usr/bin/env python3
"""Seed the database with sample Thai notes for manual testing."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import get_connection, init_db  # noqa: E402

SEED_NOTES = [
    {
        "title": "ต่อทะเบียนรถ",
        "content": "ทะเบียนหมดอายุ 15 สิงหาคม 2025 ไปที่ ขน. ต้องเตรียม สำเนาทะเบียนบ้าน บัตรประชาชน",
        "para_category": "projects",
        "sub_category": "Vehicle Registration",
        "priority": "high",
        "deadline": "2025-08-15",
        "tags": ["รถยนต์", "เอกสาร", "deadline"],
        "llm_model": "deepseek-v4-flash",
        "llm_confidence": 0.95,
        "llm_reasoning": "มีกำหนดเวลาชัดเจน (15 ส.ค. 2025) และมีเป้าหมายเฉพาะคือการต่อทะเบียนรถ",
    },
    {
        "title": "ดูแลเซิร์ฟเวอร์ Contabo",
        "content": "ดูแล server ทุกตัว เป็นงานประจำไม่มีวันจบ ตรวจสอบ disk, memory, uptime ทุกสัปดาห์",
        "para_category": "areas",
        "sub_category": "Server Maintenance",
        "priority": "medium",
        "deadline": None,
        "tags": ["server", "contabo", "ดูแลระบบ"],
        "llm_model": "deepseek-v4-flash",
        "llm_confidence": 0.9,
        "llm_reasoning": "เป็นความรับผิดชอบต่อเนื่องไม่มีกำหนดสิ้นสุด",
    },
    {
        "title": "สูตรผัดกะเพรา",
        "content": "พริกขี้หนู กระเพรา หมูสับ ซีอิ๊วขาว ซีอิ๊วดำ น้ำตาล กระเทียม ผัดไฟแรงให้หอม",
        "para_category": "resources",
        "sub_category": "Recipe",
        "priority": "low",
        "deadline": None,
        "tags": ["อาหาร", "สูตรอาหาร", "recipe"],
        "llm_model": "deepseek-v4-flash",
        "llm_confidence": 0.92,
        "llm_reasoning": "เป็นข้อมูลอ้างอิงไม่ต้องดำเนินการต่อ",
    },
    {
        "title": "Dashboard Renderer",
        "content": "โปรเจกต์ทำ dashboard renderer เสร็จสมบูรณ์แล้ว ปิดงานและย้ายไป archive",
        "para_category": "archives",
        "sub_category": "Completed Project",
        "priority": "low",
        "deadline": None,
        "tags": ["dashboard", "เสร็จแล้ว"],
        "llm_model": "gpt-oss:20b",
        "llm_confidence": 0.88,
        "llm_reasoning": "โปรเจกต์เสร็จสมบูรณ์แล้วจึงย้ายไปเก็บถาวร",
    },
    {
        "title": "จองตั๋วเครื่องบินไปเชียงใหม่",
        "content": "ต้องจองตั๋วก่อนวันที่ 1 กันยายน 2025 สำหรับทริปครอบครัวช่วงปีใหม่",
        "para_category": "projects",
        "sub_category": "Travel Booking",
        "priority": "medium",
        "deadline": "2025-09-01",
        "tags": ["เดินทาง", "ตั๋วเครื่องบิน", "เชียงใหม่"],
        "llm_model": "deepseek-v4-flash",
        "llm_confidence": 0.93,
        "llm_reasoning": "มีกำหนดเวลาที่ต้องดำเนินการก่อนวันที่กำหนด",
    },
]


async def main() -> None:
    await init_db()
    async with get_connection() as db:
        for note in SEED_NOTES:
            cursor = await db.execute(
                """
                INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                                    tags, source, llm_model, llm_confidence, llm_reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?)
                """,
                (
                    note["title"], note["content"], note["para_category"], note["sub_category"],
                    note["priority"], note["deadline"], json.dumps(note["tags"], ensure_ascii=False),
                    note["llm_model"], note["llm_confidence"], note["llm_reasoning"],
                ),
            )
            note_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO history (note_id, action, new_value, reason) VALUES (?, 'created', ?, 'seed data')",
                (note_id, note["para_category"]),
            )
        await db.commit()
    print(f"Seeded {len(SEED_NOTES)} notes.")


if __name__ == "__main__":
    asyncio.run(main())
