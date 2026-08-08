# PARA Organizer — Second Brain × Hermes Agent Roadmap

> **Version:** 4.0 | **Date:** 2026-07-29 | **Owner:** Marcus
> **Goal:** ยกระดับ PARA จาก "ระบบเก็บโน้ต + เตือน" ไปเป็น **Second Brain ที่ทำงานร่วมกับ Hermes Agent แบบ bidirectional** — จำเป็น คิดเป็น สั่งงานได้ เรียนรู้จาก feedback

---

## 1. Current State Review (ตรวจจากโค้ดจริง 2026-07-29)

> **Production stack (verified against `main`, 2026-08):** PostgreSQL 16 + pgvector (external host `169.58.65.88:5436/paradb`), deployed on **Dokploy**. `main` เป็น production source of truth. MCP server เป็น HTTP SSE (`https://mc-para.mxlabs.cloud/mcp/sse`), PostgreSQL-native, 27 tools. เวอร์ชัน SQLite เดิม (aiosqlite + FTS5 + sqlite-vec, v4/legacy) ถูกเก็บไว้เพื่ออ้างอิงประวัติใน branch `backup/sqlite-version` เท่านั้น ไม่ได้ deploy แล้ว. LLM: Ollama Cloud (primary `deepseek-v4-flash`, fallback `gpt-oss:20b`), embeddings `nomic-embed-text`.

### 1.1 สิ่งที่ทำเสร็จแล้ว (22/22 tasks จาก improvement.json)

| ชั้น | ความสามารถ | สถานะ |
|------|-----------|-------|
| **Storage** | PostgreSQL 16 + pgvector (semantic search, 768-dim) + `tsvector` (full-text), deployed on Dokploy — เดิมเป็น SQLite WAL + FTS5 + sqlite-vec (v4, ปัจจุบันอยู่ใน branch `backup/sqlite-version`) | ✅ |
| **LLM** | Auto-classify, deadline extract, auto-tag, confidence routing | ✅ |
| **MCP Server** | 27 tools, HTTP SSE transport (`mc-para.mxlabs.cloud`), PostgreSQL-native (add/search/list/get/move/archive/stats/deadlines/digest/link/update/complete/delete/reclassify/ask/…) | ✅ |
| **Hermes Inbound** | Cron webhook (`POST /api/notes/cron`) + dedup | ✅ |
| **RAG** | Hybrid search (PostgreSQL `tsvector` full-text + `pgvector` semantic search), para_ask, chat mode | ✅ |
| **Automation** | 8 scheduler jobs (reclassify, auto-archive, escalate, deadline, stale, digest, weekly review, embedding backfill) | ✅ |
| **Telegram** | Chat mode, voice STT, inline buttons (snooze/done/keep/archive), /note distill | ✅ |
| **Intelligence** | Auto-linking (embedding), note distillation on archive, weekly AI review + 3 actions | ✅ |
| **UI** | Kanban board, graph view, cost dashboard, quick-capture | ✅ |
| **Ops** | Local backup + S3-compatible cloud upload, recurring notes | ✅ |

### 1.2 Test Suite

- **180 tests passed** (ตรวจจาก `next_work.md`)
- ครอบคลุม: migration, MCP, telegram, scheduler, RAG, classifier, linker, usage, backup, cron, review, chat

### 1.3 ข้อจำกัดปัจจุบัน (Gaps)

| # | Gap | ผลกระทบ |
|---|-----|----------|
| G1 | **No outbound events** — PARA แจ้ง Hermes ไม่ได้ เมื่อมีเหตุการณ์สำคัญ | Hermes ไม่รู้ว่า deadline ใกล้, มีโน้ตใหม่เข้า inbox, หรือ confidence ต่ำ |
| G2 | **No task delegation** — PARA สั่งงาน Hermes ไม่ได้ | โน้ต "ช่วยเช็ค server" เป็นได้แค่ข้อความ ไม่ใช่ action |
| G3 | **No proactive context push** — Hermes ต้องเรียก para_search เอง | Hermes ทำงานโดยไม่มี context จาก brain |
| G4 | **No graph reasoning** — มี links + graph view แต่ไม่ reason บน graph | ตอบไม่ได้ว่า "project นี้เกี่ยวข้องกับ area ไหน" |
| G5 | **No feedback loop** — ไม่เรียนรู้ว่า classify ถูก/ผิด | confidence ไม่ improve, user ต้องแก้เองตลอด |
| G6 | **No shared memory protocol** — agent หลายตัวแชร์ brain ไม่ได้ | ถ้ามี Hermes 2+ ตัว หรือ agent อื่น ไม่มี memory layer ร่วม |
| G7 | **No action items / subtasks** — โน้ตเป็น flat text | แตกงานเป็นขั้นตอนไม่ได้, track progress ไม่ได้ |
| G8 | **No temporal reasoning** — ไม่เข้าใจลำดับเวลา | "สัปดาห์หน้าทำอะไรดี?" ตอบได้แค่จาก deadline ไม่ดู pattern |

---

## 2. Vision: Second Brain × Hermes Agent

```
┌─────────────────────────────────────────────────────────────────┐
│                    HERMES AGENT LAYER                           │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐   │
│  │ Hermes   │  │ Hermes   │  │ Hermes   │  │ Other Agents  │   │
│  │ (primary)│  │ (cron)   │  │ (future) │  │ (Claude, etc) │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬────────┘   │
│       │             │             │               │             │
│       └─────────────┴──────┬──────┴───────────────┘             │
│                            │                                    │
│              ┌─────────────▼──────────────┐                     │
│              │   BRAIN INTERFACE LAYER     │  ← NEW             │
│              │                             │                     │
│              │  • Event Bus (outbound)     │                     │
│              │  • Task Delegation API      │                     │
│              │  • Context Injection        │                     │
│              │  • Shared Memory Protocol   │                     │
│              └─────────────┬──────────────┘                     │
└────────────────────────────┼────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────┐
│              PARA SECOND BRAIN CORE                             │
│                            │                                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐   │
│  │ Notes +  │ │ Knowledge│ │ Action   │ │ Learning Loop    │   │
│  │ PARA     │ │ Graph    │ │ Items    │ │ (feedback →      │   │
│  │ classify │ │ reasoning│ │ + subtask│ │  re-classify)    │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘   │
│                                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐   │
│  │ RAG      │ │ Temporal │ │ Weekly   │ │ Telegram +       │   │
│  │ (hybrid) │ │ reasoning│ │ AI Review│ │ Web UI           │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**หลักการ:**
1. **Brain จำทุกอย่าง** — ทุก interaction, ทุก output จาก Hermes, ทุก feedback
2. **Brain คิดต่อยอด** — ไม่ใช่แค่เก็บ แต่ reason, เชื่อมโยง, แนะนำ
3. **Brain สั่งงานได้** — แปลง insight เป็น action แล้ว delegate ให้ Hermes
4. **Brain เรียนรู้** — จาก feedback ว่าอะไรถูก/ผิด ปรับปรุงตัวเอง

---

## 3. Feature Roadmap

### Phase 4: Brain ↔ Hermes Bridge (Foundation)

> เป้า: ทำให้ PARA กับ Hermes คุยกันได้ 2 ทาง ไม่ใช่แค่ Hermes → PARA

| ID | Feature | Complexity | Depends |
|----|---------|:----------:|---------|
| **SB-01** | Outbound Event Webhook | M | — |
| **SB-02** | Task Delegation API | L | SB-01 |
| **SB-03** | Context Injection Middleware | M | — |
| **SB-04** | MCP Resource: Brain State | S | — |

---

#### SB-01: Outbound Event Webhook

**ปัญหา:** PARA แจ้ง Hermes ไม่ได้ — deadline ใกล้, โน้ตใหม่เข้า inbox, confidence ต่ำ, stale project — Hermes ไม่รู้

**Scope:**
- ตาราง `events` (id, event_type, note_id, payload JSON, status, created_at, delivered_at)
- Event types: `note.created`, `note.classified`, `note.deadline_approaching`, `note.stale`, `note.completed`, `note.low_confidence`, `review.generated`
- Webhook dispatcher: POST ไปที่ configured URLs (Hermes webhook, n8n, etc.)
- Config: `EVENT_WEBHOOK_URL`, `EVENT_WEBHOOK_SECRET`, `EVENT_TYPES_ENABLED`
- Retry 3x with backoff, log delivery status
- Hook เข้า flow ที่มีอยู่: classifier, scheduler (deadline/stale), notes CRUD

**Acceptance:**
- สร้างโน้ต → event `note.created` ถูก dispatch ไป Hermes
- Deadline 7/3/1 วัน → event `note.deadline_approaching` ถูกส่ง
- ทดสอบด้วย pytest + mock webhook server

**Files:** `app/events.py`, `app/database.py` (schema), `app/config.py`, `app/scheduler.py`, `app/routes/notes.py`

---

#### SB-02: Task Delegation API

**ปัญหา:** โน้ต "ช่วยเช็ค server ให้หน่อย" เป็นได้แค่ข้อความ — PARA สั่ง Hermes ให้ทำงานแทนไม่ได้

**Scope:**
- ตาราง `tasks` (id, note_id, task_type, prompt, status [pending/dispatched/completed/failed], result, hermes_job_id, created_at, completed_at)
- REST API:
  - `POST /api/tasks` — สร้าง task จากโน้ต (manual หรือ LLM extract)
  - `GET /api/tasks` — list tasks
  - `POST /api/tasks/{id}/complete` — Hermes รายงานผลกลับ
- MCP tools:
  - `para_create_task(note_id, prompt)` — สร้าง task จากโน้ต
  - `para_task_result(task_id, result)` — Hermes ส่งผลลัพธ์กลับ → สร้างโน้ตใหม่จาก result
- LLM task extraction: ตอน classify โน้ต ถ้าพบ action verb ("ช่วย", "เช็ค", "รัน", "สร้าง") → suggest task
- Task → Note loop: เมื่อ Hermes ทำงานเสร็จ → result ถูกบันทึกเป็นโน้ตใหม่อัตโนมัติ (source=`task:<task_id>`)

**Acceptance:**
- สร้างโน้ต "ช่วยเช็ค disk space server" → LLM suggest task → dispatch ไป Hermes
- Hermes ทำงานเสร็จ → POST /api/tasks/{id}/complete → เกิดโน้ตใหม่ใน PARA
- ทดสอบ end-to-end ด้วย mock Hermes

**Files:** `app/tasks.py`, `app/routes/tasks.py`, `app/mcp/mcp_server.py`, `app/classifier.py`, `app/database.py`

---

#### SB-03: Context Injection Middleware

**ปัญหา:** Hermes ต้องเรียก `para_search` / `para_ask` เองทุกครั้ง — ไม่มี proactive context

**Scope:**
- MCP tool ใหม่: `para_context(topic: str) -> dict` — คืน context package:
  - โน้ตที่เกี่ยวข้อง (hybrid search)
  - deadlines ที่ใกล้ถึง
  - tasks ที่ค้างอยู่
  - recent activity (last 7 days)
  - graph neighbors (โน้ตที่ link กัน)
- REST endpoint: `GET /api/context?topic=<str>` — สำหรับ agent ที่ไม่ใช้ MCP
- Hermes config: ใช้ `para_context` เป็น step แรกก่อนทำงานทุก job
- Auto-context: ตอน Hermes ส่ง cron output → PARA ตอบกลับด้วย related context

**Acceptance:**
- เรียก `para_context("server maintenance")` → ได้โน้ต + deadlines + tasks + links ที่เกี่ยวข้อง
- Hermes cron ส่งข้อมูล → response มี `related_notes` field

**Files:** `app/context.py`, `app/mcp/mcp_server.py`, `app/routes/cron_webhook.py`

---

#### SB-04: MCP Resource — Brain State

**ปัญหา:** Hermes ไม่มีภาพรวมของ brain — ต้องเรียกหลาย tools ต่อๆ กัน

**Scope:**
- MCP resource: `para://brain-state` — คืน JSON snapshot:
  - Stats (total, by category, by status)
  - Active projects + deadlines
  - Pending tasks (จาก SB-02)
  - Inbox items ที่รอ review (confidence < threshold)
  - Stale items
  - Recent events (จาก SB-01)
- MCP resource: `para://inbox` — โน้ต inbox ที่รอจัดหมวด
- Hermes เรียก resource เดียวได้ภาพรวม ไม่ต้องเรียก 5 tools

**Acceptance:**
- Hermes อ่าน `para://brain-state` → ได้ snapshot ครบใน 1 call
- ข้อมูลตรงกับ `para_stats` + `para_deadlines` + `para_digest` รวมกัน

**Files:** `app/mcp/mcp_server.py`

---

### Phase 5: Intelligence Layer (Brain คิดเป็น)

> เป้า: PARA ไม่ใช่แค่เก็บ แต่ reason บนข้อมูลที่มี

| ID | Feature | Complexity | Depends |
|----|---------|:----------:|---------|
| **SB-05** | Knowledge Graph Reasoning | L | — |
| **SB-06** | Action Items + Subtasks | M | — |
| **SB-07** | Temporal Reasoning + Smart Suggestions | M | SB-05 |
| **SB-08** | Feedback Loop + Learning | M | — |

---

#### SB-05: Knowledge Graph Reasoning

**ปัญหา:** มี links table + graph view แต่ไม่ reason บน graph — ตอบไม่ได้ว่า "project นี้เกี่ยวข้องกับ area ไหน"

**Scope:**
- Graph query engine บน links table:
  - `GET /api/notes/{id}/graph?depth=2` — คืน subgraph รอบโน้ต (nodes + edges)
  - `GET /api/graph/path?from={id}&to={id}` — หา path ระหว่างโน้ต 2 ตัว
  - `GET /api/graph/clusters` — หา groups ของโน้ตที่เชื่อมกัน (connected components)
- MCP tools:
  - `para_graph_context(note_id, depth=2)` — ดึง subgraph + summaries
  - `para_related(note_id)` — โน้ตที่เกี่ยวข้อง (links + embedding similarity)
- LLM graph reasoning: ป้อน subgraph ให้ LLM ตอบคำถามเชิงความสัมพันธ์
  - "project X เกี่ยวข้องกับ area Y ยังไง?"
  - "resource ไหนที่ใช้กับ project นี้ได้บ้าง?"
- Auto-suggest links: ตอนสร้างโน้ตใหม่ ถ้า embedding similarity > threshold กับโน้ตใน category อื่น → suggest cross-category link

**Acceptance:**
- โน้ต A link กับ B, B link กับ C → `para_graph_context(A, depth=2)` คืน A, B, C
- ถาม "project นี้เกี่ยวข้องกับ area ไหน" → LLM ตอบโดยอ้างอิง graph
- Graph clusters แสดงใน Web UI

**Files:** `app/graph.py`, `app/routes/graph.py`, `app/mcp/mcp_server.py`, `app/linker.py`

---

#### SB-06: Action Items + Subtasks

**ปัญหา:** โน้ตเป็น flat text — แตกงานเป็นขั้นตอนไม่ได้, track progress ไม่ได้

**Scope:**
- ตาราง `action_items` (id, note_id, content, status [todo/doing/done], order_index, created_at, completed_at)
- LLM extraction: ตอนสร้างโน้ต ถ้า content มี list / ขั้นตอน → auto-extract เป็น action items
- REST API:
  - `GET /api/notes/{id}/items` — list action items
  - `POST /api/notes/{id}/items` — เพิ่ม item
  - `PUT /api/items/{id}` — อัปเดต status
  - `DELETE /api/items/{id}` — ลบ
- MCP tools:
  - `para_items(note_id)` — list items
  - `para_add_item(note_id, content)` — เพิ่ม
  - `para_done_item(item_id)` — เสร็จ
- Progress tracking: note มี `progress` field (0-100%) คำนวณจาก items done/total
- Telegram: `/items <note_id>` — แสดง checklist, กด toggle ได้
- เมื่อ items ทั้งหมด done → suggest complete โน้ต

**Acceptance:**
- สร้างโน้ต "เตรียม deploy: 1) test 2) backup 3) deploy 4) verify" → auto-extract 4 items
- ทำ item เสร็จ 2/4 → progress = 50%
- ทำครบ 4/4 → Telegram ถาม "เสร็จแล้ว archive ไหม?"

**Files:** `app/database.py` (schema), `app/routes/items.py`, `app/mcp/mcp_server.py`, `app/classifier.py`, `app/integrations/telegram_bot.py`

---

#### SB-07: Temporal Reasoning + Smart Suggestions

**ปัญหา:** "สัปดาห์หน้าทำอะไรดี?" ตอบได้แค่จาก deadline ไม่ดู pattern

**Scope:**
- Activity pattern analysis:
  - วิเคราะห์ history table: user ทำงานอะไร วันไหน เวลาไหน
  - ระบุ peak productivity hours/days
  - ระบุ project ที่ใช้เวลานาน vs เร็ว
- Smart suggestions engine:
  - "สัปดาห์หน้าทำอะไรดี?" → วิเคราะห์ deadlines + stale + progress + pattern → แนะนำ
  - "มีอะไรค้างอยู่บ้าง?" → รวม active projects + pending tasks + overdue items
  - "project ไหนควร focus?" → rank โดย deadline + priority + stale + dependencies
- MCP tool: `para_plan(horizon_days=7) -> dict` — คืน suggested plan:
  - prioritized actions
  - deadlines approaching
  - stale items to revisit
  - suggested focus areas
- Weekly review++: รวม temporal insights ใน review (ทำตอนไหนดี, project ไหนควร focus)

**Acceptance:**
- เรียก `para_plan(7)` → ได้ plan 7 วันที่คำนึง deadlines + priority + pattern
- Weekly review มี section "Suggested Focus" ที่ไม่ใช่แค่ list แต่มีเหตุผล

**Files:** `app/planner.py`, `app/mcp/mcp_server.py`, `app/review.py`

---

#### SB-08: Feedback Loop + Learning

**ปัญหา:** ไม่เรียนรู้ว่า classify ถูก/ผิด — user ต้องแก้เองตลอด

**Scope:**
- Feedback capture:
  - เมื่อ user ย้ายหมวด manual (move) → log เป็น implicit feedback
  - เมื่อ user แก้ tags/priority → log feedback
  - Telegram: `/feedback <note_id> <correct_category>` — explicit feedback
- ตาราง `feedback` (id, note_id, field, llm_value, user_value, timestamp)
- Feedback analysis:
  - รายสัปดาห์: สรุปว่า LLM classify ผิดบ่อยใน category ไหน
  - ระบุ pattern: "โน้ตเรื่อง X มักถูก classify เป็น Y แต่ควรเป็น Z"
- Prompt tuning:
  - ถ้า feedback สะสม > threshold ใน category → เพิ่ม few-shot examples ใน classify prompt
  - Dynamic few-shot: เลือก examples จาก feedback ที่คล้ายโน้ตใหม่
- Confidence calibration:
  - เทียบ confidence กับ accuracy จริง → ปรับ threshold
  - ถ้า confidence 0.8 แต่ผิด 40% → flag ว่า overconfident

**Acceptance:**
- User ย้ายโน้ตจาก resources → projects 10 ครั้ง → system เพิ่ม example ใน prompt
- โน้ตใหม่ที่คล้ายกัน → classify ถูกต้องมากขึ้น
- Weekly review มี section "Classification Accuracy" + suggestions

**Files:** `app/feedback.py`, `app/classifier.py`, `app/review.py`, `app/database.py`

---

### Phase 6: Autonomous Brain (Brain สั่งงานเป็น)

> เป้า: PARA ทำงานเชิงรุก — ไม่ต้องรอ user สั่ง

| ID | Feature | Complexity | Depends |
|----|---------|:----------:|---------|
| **SB-09** | Autonomous Task Generation | L | SB-02, SB-07 |
| **SB-10** | Multi-Agent Memory Protocol | L | SB-01 |
| **SB-11** | Brain Health Dashboard | M | SB-08 |
| **SB-12** | Voice + Multimodal Input | M | — |

---

#### SB-09: Autonomous Task Generation

**ปัญหา:** PARA รอ user สั่งอย่างเดียว — ไม่เคยเสนองานเอง

**Scope:**
- Proactive task engine (scheduler job รายวัน):
  - วิเคราะห์ deadlines ที่ใกล้ → สร้าง task "เตรียม X ก่อน deadline"
  - วิเคราะห์ stale projects → สร้าง task "ทบทวน X"
  - วิเคราะห์ neglected areas → สร้าง task "ดูแล X"
  - วิเคราะห์ graph: project ใหม่ไม่มี link กับ area ที่ควร → suggest
- Task proposal flow:
  - PARA สร้าง task proposal → ส่ง Telegram ให้ user approve/reject
  - ถ้า approve → dispatch ไป Hermes (ผ่าน SB-02)
  - ถ้า reject → log feedback, ปรับ threshold
- Confidence-gated autonomy:
  - Task ที่ confidence สูง (deadline ใกล้, pattern ชัด) → auto-dispatch
  - Task ที่ confidence ต่ำ → ต้อง user approve
  - Config: `AUTONOMY_LEVEL` (suggest_only / auto_approve / full_auto)

**Acceptance:**
- Deadline "ต่อทะเบียนรถ" ใกล้ 7 วัน → PARA สร้าง task "เตรียมเอกสาร" → เสนอ user
- User approve → Hermes ได้รับ task → ทำงาน → รายงานผล
- AUTONOMY_LEVEL=full_auto → task ถูก dispatch โดยไม่ต้อง approve

**Files:** `app/autonomy.py`, `app/tasks.py`, `app/scheduler.py`, `app/notifier.py`

---

#### SB-10: Multi-Agent Memory Protocol

**ปัญหา:** ถ้ามี Hermes หลายตัว หรือ agent อื่น (Claude, Codex) ไม่มี shared memory

**Scope:**
- Memory namespace:
  - แต่ละ agent มี `agent_id` — โน้ต/task/event มี `agent_id` field
  - Shared notes: โน้ตที่ทุก agent อ่าน/เขียนได้
  - Private notes: โน้ตเฉพาะ agent
- MCP tools สำหรับ agent identity:
  - `para_whoami()` — คืน agent_id + permissions
  - `para_shared_notes()` — โน้ตที่แชร์ข้าม agent
  - `para_agent_notes(agent_id)` — โน้ตเฉพาะ agent
- Conflict resolution:
  - ถ้า 2 agents แก้โน้ตเดียวกัน → last-write-wins + log conflict
  - Merge suggestion: LLM รวม content ที่ขัดแย้ง
- Agent activity feed:
  - `GET /api/agents/{id}/activity` — ดูว่า agent ไหนทำอะไร
  - `para_agent_activity(agent_id)` — MCP version

**Acceptance:**
- Hermes A สร้างโน้ต → Hermes B เห็นผ่าน `para_shared_notes()`
- 2 agents แก้โน้ตเดียวกัน → conflict ถูก log, user แจ้งเตือน
- Activity feed แสดงว่า agent ไหนทำอะไร เมื่อไหร่

**Files:** `app/database.py` (schema), `app/mcp/mcp_server.py`, `app/routes/agents.py`

---

#### SB-11: Brain Health Dashboard

**ปัญหา:** ไม่มีภาพรวมว่า brain สุขภาพดีไหม — โน้ตค้าง inbox เยอะ? classify ผิดบ่อย? graph เชื่อมกันดี?

**Scope:**
- Health metrics:
  - **Inbox zero rate**: % โน้ตที่ออกจาก inbox ภายใน 24 ชม.
  - **Classification accuracy**: จาก feedback loop (SB-08)
  - **Graph connectivity**: % โน้ตที่มี links ≥ 1
  - **Staleness index**: % active projects ที่ stale
  - **Task completion rate**: % tasks ที่เสร็จทันเวลา
  - **Embedding coverage**: % โน้ตที่ embed แล้ว
  - **Review compliance**: % weekly reviews ที่ user อ่าน/action
- Web UI: `/health` — dashboard แสดง metrics + trends
- MCP tool: `para_health() -> dict` — Hermes เรียกดูได้
- Alert: ถ้า metric ต่ำกว่า threshold → แจ้ง Telegram

**Acceptance:**
- หน้า /health แสดง 7 metrics พร้อม trend 4 สัปดาห์
- Inbox zero rate < 50% → Telegram alert
- `para_health()` คืน metrics ครบ

**Files:** `app/health.py`, `app/routes/health.py`, `app/templates/health.html`, `app/mcp/mcp_server.py`

---

#### SB-12: Voice + Multimodal Input

**ปัญหา:** รับ input ได้แค่ text — ไม่มี image, file, screenshot

**Scope:**
- Image input:
  - Telegram: ส่งรูป → OCR (Tesseract / LLM vision) → extract text → สร้างโน้ต
  - Web UI: drag-drop รูป → OCR → create note
- File input:
  - PDF → extract text → create note
  - URL → fetch + summarize → create note
- Voice improvement:
  - ปัจจุบันมี Whisper STT ผ่าน Telegram → เพิ่ม language detection (ไทย/อังกฤษ)
  - Voice → action: ถ้าถอดความแล้วพบ action verb → suggest task (SB-06)
- MCP tool: `para_add_multimodal(content_type, data)` — agent ส่ง image/file ได้

**Acceptance:**
- ส่งรูปตารางงานผ่าน Telegram → OCR → โน้ตถูกสร้างพร้อม extract deadlines
- ส่ง URL → สรุปเนื้อหา → โน้ต resource
- PDF 3 หน้า → โน้ตสรุป 1 ย่อหน้า

**Files:** `app/multimodal.py`, `app/integrations/telegram_bot.py`, `app/routes/notes.py`

---

## 4. Implementation Plan

### Phase Sequencing

```
Phase 4 (Foundation)          Phase 5 (Intelligence)         Phase 6 (Autonomous)
┌─────────────────────┐      ┌─────────────────────┐       ┌─────────────────────┐
│ SB-01 Event Bus ────────────▶ SB-10 Multi-Agent  │       │                     │
│ SB-02 Task API ─────────────────────────────────────────▶ SB-09 Autonomous     │
│ SB-03 Context Inject│      │ SB-05 Graph Reason ────────▶ SB-07 Temporal      │
│ SB-04 Brain State   │      │ SB-06 Action Items  │       │ SB-11 Health Dash   │
│                     │      │ SB-08 Feedback Loop │       │ SB-12 Multimodal    │
└─────────────────────┘      └─────────────────────┘       └─────────────────────┘
     ~2 weeks                    ~3 weeks                      ~3 weeks
```

### Agent Assignment (3-agent parallel)

| Agent | Phase 4 | Phase 5 | Phase 6 |
|-------|---------|---------|---------|
| **codex** (heavy AI) | SB-02 Task API | SB-05 Graph Reasoning, SB-08 Feedback | SB-09 Autonomous |
| **claude-haiku** (automation) | SB-01 Event Bus, SB-03 Context | SB-07 Temporal, SB-06 Items | SB-10 Multi-Agent |
| **opencode-qwen** (UI/docs) | SB-04 Brain State | SB-06 Items (UI part) | SB-11 Health Dash, SB-12 Multimodal |

### File Ownership (Phase 4)

| File | Owner |
|------|-------|
| `app/events.py` | claude-haiku |
| `app/tasks.py` | codex |
| `app/routes/tasks.py` | codex |
| `app/context.py` | claude-haiku |
| `app/mcp/mcp_server.py` | codex (tools) + claude-haiku (context tool) — แบ่ง function ชัด |
| `app/database.py` | codex (schema only, Phase 4 เท่านั้น) |
| `app/config.py` | claude-haiku (event config) |

### Critical Path

```
SB-01 (Event Bus) → SB-02 (Task API) → SB-09 (Autonomous)
SB-05 (Graph) → SB-07 (Temporal) → SB-09
SB-08 (Feedback) → SB-11 (Health)
```

**SB-09 (Autonomous Task Generation)** เป็นงานที่ depends มากสุด — ต้องรอ SB-02, SB-07, SB-08

---

## 5. Database Schema Additions (Phase 4-6)

```sql
-- Phase 4: Events
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    note_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | failed
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

-- Phase 4: Tasks (delegation)
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER,
    task_type TEXT NOT NULL DEFAULT 'general',
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | dispatched | completed | failed
    result TEXT,
    hermes_job_id TEXT,
    agent_id TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE SET NULL
);

-- Phase 5: Action Items
CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',  -- todo | doing | done
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

-- Phase 5: Feedback
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    field TEXT NOT NULL,         -- para_category | priority | tags
    llm_value TEXT,
    user_value TEXT NOT NULL,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

-- Phase 6: Agent identity (multi-agent)
ALTER TABLE notes ADD COLUMN agent_id TEXT;
ALTER TABLE tasks ADD COLUMN agent_id TEXT;

-- Phase 4: notes.progress (computed from action_items)
ALTER TABLE notes ADD COLUMN progress REAL DEFAULT NULL;
```

---

## 6. New MCP Tools Summary

| Phase | Tool | Signature | Purpose |
|-------|------|-----------|---------|
| 4 | `para_context` | `(topic: str) -> dict` | Context package สำหรับ agent ก่อนทำงาน |
| 4 | `para_create_task` | `(note_id: int, prompt: str) -> dict` | สร้าง task จากโน้ต |
| 4 | `para_task_result` | `(task_id: int, result: str) -> dict` | Hermes รายงานผล → สร้างโน้ต |
| 4 | resource `para://brain-state` | — | Snapshot ภาพรวม brain |
| 4 | resource `para://inbox` | — | โน้ต inbox รอจัดหมวด |
| 5 | `para_graph_context` | `(note_id: int, depth: int) -> dict` | Subgraph รอบโน้ต |
| 5 | `para_related` | `(note_id: int) -> list` | โน้ตที่เกี่ยวข้อง (links + embedding) |
| 5 | `para_items` | `(note_id: int) -> list` | Action items ของโน้ต |
| 5 | `para_add_item` | `(note_id: int, content: str) -> dict` | เพิ่ม action item |
| 5 | `para_done_item` | `(item_id: int) -> dict` | ทำ item เสร็จ |
| 5 | `para_plan` | `(horizon_days: int) -> dict` | Suggested plan |
| 6 | `para_whoami` | `() -> dict` | Agent identity |
| 6 | `para_shared_notes` | `() -> list` | โน้ตแชร์ข้าม agent |
| 6 | `para_health` | `() -> dict` | Brain health metrics |
| 6 | `para_add_multimodal` | `(content_type: str, data: str) -> dict` | รับ image/file |

---

## 7. Success Metrics

| Metric | Current (v3) | Target (v4) |
|--------|:------------:|:-----------:|
| Hermes ↔ PARA interactions/day | ~5 (cron only) | 20+ (bidirectional) |
| Notes auto-classified correctly | ~85% (estimated) | 95%+ (feedback loop) |
| Tasks delegated to Hermes | 0 | 5+/week |
| Brain context used by Hermes | manual (para_search) | automatic (para_context) |
| Inbox zero time | N/A | < 24h median |
| Graph connectivity (notes with ≥1 link) | ~30% | 60%+ |
| Weekly review actionability | 3 generic actions | 3 specific + temporal |

---

## 8. Housekeeping (ทำก่อนเริ่ม Phase 4)

- [ ] อัปเดต `improvement.json` — status ทุก task เป็น `done` (ค้างเป็น `pending`)
- [ ] อัปเดต `next_work.md` — IMP-09, IMP-12, IMP-17, IMP-20 เสร็จหมดแล้วจากโค้ดจริง
- [ ] อัปเดต `agent_skill.md` — เพิ่ม MCP tools ใหม่ (update/complete/delete/reclassify/ask)
- [ ] เพิ่ม `SECOND_BRAIN_ROADMAP.md` เข้า `.gitignore`? หรือไม่ — แล้วแต่ต้องการ track ใน repo
- [ ] สร้าง git branch `phase4-brain-bridge` สำหรับ Phase 4
