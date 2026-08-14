"""Brain health metrics engine (SB-11).

`compute_health` works against either backend (auto-detected by the configured
``PARA_DB_URL``) and returns a 0-100 score per metric, a weighted overall score,
and a list of human-readable alerts when any metric crosses its threshold.

The weight / threshold tables are identical for both backends — only the SQL
shape differs.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, func, select

from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import Feedback as PgFeedback
from app.models_v2 import Link as PgLink
from app.models_v2 import Note as PgNote
from app.models_v2 import Task as PgTask

logger = logging.getLogger("para.health")

_WEIGHTS = {
    "inbox_zero_rate": 0.15,
    "classification_accuracy": 0.20,
    "graph_connectivity": 0.10,
    "staleness_index": 0.20,
    "task_completion_rate": 0.15,
    "embedding_coverage": 0.10,
    "review_compliance": 0.10,
}

_THRESHOLDS = {
    "inbox_zero_rate": ("<", 50, " Inbox Zero ต่ำกว่า 50% — มีโน้ตค้างใน inbox มากเกินไป"),
    "classification_accuracy": ("<", 70, "ความแม่นยำของการจัดหมวดหมู่ต่ำกว่า 70% — ควรตรวจสอบ feedback"),
    "graph_connectivity": ("<", 30, "การเชื่อมต่อกราฟต่ำกว่า 30% — โน้ตส่วนใหญ่ยังไม่มีลิงก์"),
    "staleness_index": (">", 30, "โปรเจกต์มากกว่า 30% ไม่มีความเคลื่อนไหว — ควรทบทวน"),
    "task_completion_rate": ("<", 50, "อัตราการทำงานเสร็จต่ำกว่า 50% — มีงานค้างจำนวนมาก"),
    "embedding_coverage": ("<", 50, "ความครอบคลุม embedding ต่ำกว่า 50% — การค้นหาเชิงความหมายยังจำกัด"),
    "review_compliance": ("<", 50, "การทบทวนรายสัปดาห์ต่ำกว่า 50% — ขาดการ review สม่ำเสมอ"),
}


def _using_pg() -> bool:
    return bool(settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL)


# ── PostgreSQL implementations ──────────────────────────────────────────────


async def _pg_inbox_zero_rate(session) -> float:
    total = (await session.execute(
        select(func.count()).select_from(PgNote)
        .where(PgNote.created_at < datetime.utcnow() - timedelta(days=1))
    )).scalar_one()
    if total == 0:
        return 100.0
    moved = (await session.execute(
        select(func.count()).select_from(PgNote)
        .where(and_(
            PgNote.para_category != "inbox",
            PgNote.created_at < datetime.utcnow() - timedelta(days=1),
        ))
    )).scalar_one()
    return round(float(moved) / float(total) * 100, 2)


async def _pg_classification_accuracy(session) -> float:
    total = (await session.execute(
        select(func.count()).select_from(PgFeedback)
        .where(PgFeedback.field == "para_category")
    )).scalar_one()
    if total == 0:
        return 100.0
    matched = (await session.execute(
        select(func.count()).select_from(PgFeedback)
        .where(and_(
            PgFeedback.field == "para_category",
            PgFeedback.llm_value == PgFeedback.user_value,
        ))
    )).scalar_one()
    return round(float(matched) / float(total) * 100, 2)


async def _pg_graph_connectivity(session) -> float:
    active = (await session.execute(
        select(func.count()).select_from(PgNote).where(PgNote.status != "archived")
    )).scalar_one()
    if active == 0:
        return 100.0
    has_link = (
        select(PgLink.from_note_id)
        .where(PgLink.from_note_id == PgNote.id)
        .union_all(select(PgLink.to_note_id).where(PgLink.to_note_id == PgNote.id))
    ).exists()
    linked = (await session.execute(
        select(func.count(func.distinct(PgNote.id))).select_from(PgNote)
        .where(and_(PgNote.status != "archived", has_link))
    )).scalar_one()
    return round(float(linked) / float(active) * 100, 2)


async def _pg_staleness_index(session) -> float:
    total = (await session.execute(
        select(func.count()).select_from(PgNote)
        .where(and_(PgNote.para_category == "projects", PgNote.status == "active"))
    )).scalar_one()
    if total == 0:
        return 0.0
    cutoff = datetime.utcnow() - timedelta(days=settings.NOTIFY_STALE_DAYS)
    stale = (await session.execute(
        select(func.count()).select_from(PgNote)
        .where(and_(
            PgNote.para_category == "projects",
            PgNote.status == "active",
            PgNote.updated_at < cutoff,
        ))
    )).scalar_one()
    return round(float(stale) / float(total) * 100, 2)


async def _pg_task_completion_rate(session) -> float:
    total = (await session.execute(select(func.count()).select_from(PgTask))).scalar_one()
    if total == 0:
        return 100.0
    completed = (await session.execute(
        select(func.count()).select_from(PgTask).where(PgTask.status == "completed")
    )).scalar_one()
    return round(float(completed) / float(total) * 100, 2)


async def _pg_embedding_coverage(session) -> float:
    total = (await session.execute(
        select(func.count()).select_from(PgNote).where(PgNote.status != "archived")
    )).scalar_one()
    if total == 0:
        return 100.0
    done = (await session.execute(
        select(func.count()).select_from(PgNote)
        .where(and_(PgNote.status != "archived", PgNote.embedding_status == "done"))
    )).scalar_one()
    return round(float(done) / float(total) * 100, 2)


async def _pg_review_compliance(session) -> float:
    # Notifications live only in the legacy table; for now we approximate from
    # PgTask rows of type review (none in current code), so a hard-coded 100%
    # would be wrong. Best signal: count of archived review notes from the last
    # 28 days, treating >= 4 as full compliance.
    cutoff = datetime.utcnow() - timedelta(days=28)
    sent = (await session.execute(
        select(func.count()).select_from(PgNote)
        .where(and_(
            PgNote.source == "review",
            PgNote.created_at >= cutoff,
        ))
    )).scalar_one()
    return round(min(float(sent) / 4.0 * 100, 100.0), 2)


# ── SQLite implementations (legacy / local-dev) ─────────────────────────────


async def _inbox_zero_rate(db) -> float:
    total = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE created_at < datetime('now', '-1 day')"
    )).fetchone())["c"]
    if total == 0:
        return 100.0
    moved = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes "
        "WHERE para_category != 'inbox' AND created_at < datetime('now', '-1 day')"
    )).fetchone())["c"]
    return round(moved / total * 100, 2)


async def _classification_accuracy(db) -> float:
    total = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM feedback WHERE field = 'para_category'"
    )).fetchone())["c"]
    if total == 0:
        return 100.0
    matched = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM feedback "
        "WHERE field = 'para_category' AND llm_value = user_value"
    )).fetchone())["c"]
    return round(matched / total * 100, 2)


async def _graph_connectivity(db) -> float:
    active = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE status != 'archived'"
    )).fetchone())["c"]
    if active == 0:
        return 100.0
    linked = (await (await db.execute(
        "SELECT COUNT(DISTINCT n.id) AS c FROM notes n "
        "WHERE n.status != 'archived' AND ("
        "  EXISTS (SELECT 1 FROM links l WHERE l.from_note_id = n.id) "
        "  OR EXISTS (SELECT 1 FROM links l WHERE l.to_note_id = n.id)"
        ")"
    )).fetchone())["c"]
    return round(linked / active * 100, 2)


async def _staleness_index(db) -> float:
    cutoff = f"-{settings.NOTIFY_STALE_DAYS} days"
    total = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE para_category = 'projects' AND status = 'active'"
    )).fetchone())["c"]
    if total == 0:
        return 0.0
    stale = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes "
        "WHERE para_category = 'projects' AND status = 'active' AND updated_at < datetime('now', ?)",
        (cutoff,),
    )).fetchone())["c"]
    return round(stale / total * 100, 2)


async def _task_completion_rate(db) -> float:
    total = (await (await db.execute("SELECT COUNT(*) AS c FROM tasks")).fetchone())["c"]
    if total == 0:
        return 100.0
    completed = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status = 'completed'"
    )).fetchone())["c"]
    return round(completed / total * 100, 2)


async def _embedding_coverage(db) -> float:
    total = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE status != 'archived'"
    )).fetchone())["c"]
    if total == 0:
        return 100.0
    done = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE embedding_status = 'done' AND status != 'archived'"
    )).fetchone())["c"]
    return round(done / total * 100, 2)


async def _review_compliance(db) -> float:
    sent = (await (await db.execute(
        "SELECT COUNT(*) AS c FROM notifications "
        "WHERE type = 'review' AND status = 'sent' AND scheduled_at >= datetime('now', '-28 days')"
    )).fetchone())["c"]
    return round(min(sent / 4 * 100, 100.0), 2)


# ── Public entry point ──────────────────────────────────────────────────────


async def compute_health() -> dict:
    metrics: dict = {}
    totals = {"total_notes": 0, "active_projects": 0, "pending_tasks": 0}

    if _using_pg():
        async with async_session_factory() as session:
            for name, fn in (
                ("inbox_zero_rate", _pg_inbox_zero_rate),
                ("classification_accuracy", _pg_classification_accuracy),
                ("graph_connectivity", _pg_graph_connectivity),
                ("staleness_index", _pg_staleness_index),
                ("task_completion_rate", _pg_task_completion_rate),
                ("embedding_coverage", _pg_embedding_coverage),
                ("review_compliance", _pg_review_compliance),
            ):
                try:
                    metrics[name] = await fn(session)
                except Exception:
                    logger.warning("Failed to compute metric %s", name, exc_info=True)
                    metrics[name] = None
            try:
                totals["total_notes"] = int((await session.execute(
                    select(func.count()).select_from(PgNote)
                )).scalar_one() or 0)
            except Exception:
                pass
            try:
                totals["active_projects"] = int((await session.execute(
                    select(func.count()).select_from(PgNote)
                    .where(and_(PgNote.para_category == "projects", PgNote.status == "active"))
                )).scalar_one() or 0)
            except Exception:
                pass
            try:
                totals["pending_tasks"] = int((await session.execute(
                    select(func.count()).select_from(PgTask).where(PgTask.status == "pending")
                )).scalar_one() or 0)
            except Exception:
                pass
    else:
        from app.database import get_connection
        async with get_connection() as db:
            for name, fn in (
                ("inbox_zero_rate", _inbox_zero_rate),
                ("classification_accuracy", _classification_accuracy),
                ("graph_connectivity", _graph_connectivity),
                ("staleness_index", _staleness_index),
                ("task_completion_rate", _task_completion_rate),
                ("embedding_coverage", _embedding_coverage),
                ("review_compliance", _review_compliance),
            ):
                try:
                    metrics[name] = await fn(db)
                except Exception:
                    logger.warning("Failed to compute metric %s", name, exc_info=True)
                    metrics[name] = None
            try:
                totals["total_notes"] = (await (await db.execute("SELECT COUNT(*) AS c FROM notes")).fetchone())["c"]
            except Exception:
                pass
            try:
                totals["active_projects"] = (await (await db.execute(
                    "SELECT COUNT(*) AS c FROM notes WHERE para_category = 'projects' AND status = 'active'"
                )).fetchone())["c"]
            except Exception:
                pass
            try:
                totals["pending_tasks"] = (await (await db.execute(
                    "SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending'"
                )).fetchone())["c"]
            except Exception:
                pass

    weighted_sum = 0.0
    weight_total = 0.0
    for name, weight in _WEIGHTS.items():
        value = metrics.get(name)
        if value is None:
            continue
        score = (100 - value) if name == "staleness_index" else value
        weighted_sum += score * weight
        weight_total += weight
    overall_score = round(weighted_sum / weight_total, 2) if weight_total else 0.0

    alerts = []
    for name, (op, threshold, message) in _THRESHOLDS.items():
        value = metrics.get(name)
        if value is None:
            continue
        if (op == "<" and value < threshold) or (op == ">" and value > threshold):
            alerts.append(message)

    return {
        "computed_at": datetime.now().isoformat(),
        "metrics": metrics,
        "overall_score": overall_score,
        "alerts": alerts,
        "trends": totals,
    }
