"""SQLAlchemy 2.0 ORM models for the PARA Organizer PostgreSQL schema.

All models use the ``Mapped[]`` annotation style and are designed to work with
the async engine created in :mod:`app.database_v2`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    """Declarative base for all PARA Organizer models."""


# ── Note ─────────────────────────────────────────────────────────────────────

class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    para_category: Mapped[str] = mapped_column(
        String, nullable=False, default="inbox", index=True
    )
    sub_category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", index=True
    )
    priority: Mapped[str] = mapped_column(
        String, nullable=False, default="medium", index=True
    )
    deadline: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="manual", index=True
    )
    source_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, default=dict
    )
    llm_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    llm_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    llm_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embedding_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )
    recurrence: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    embedding: Mapped[Optional[list[float]]] = mapped_column(
        Vector(768),
        nullable=True,
    )
    search_vector: Mapped[Optional[Any]] = mapped_column(
        TSVECTOR,
        # GENERATED ALWAYS AS ... STORED — added via Alembic migration
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Relationships ──
    links_from: Mapped[list["Link"]] = relationship(
        "Link", foreign_keys="Link.from_note_id", back_populates="from_note",
        cascade="all, delete-orphan",
    )
    links_to: Mapped[list["Link"]] = relationship(
        "Link", foreign_keys="Link.to_note_id", back_populates="to_note",
        cascade="all, delete-orphan",
    )
    history_entries: Mapped[list["History"]] = relationship(
        "History", back_populates="note", cascade="all, delete-orphan",
    )
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="note", cascade="all, delete-orphan",
    )
    llm_usages: Mapped[list["LlmUsage"]] = relationship(
        "LlmUsage", back_populates="note", cascade="all, delete-orphan",
    )
    tasks: Mapped[list["Task"]] = relationship(
        "Task", back_populates="note", cascade="all, delete-orphan",
    )
    items: Mapped[list["Item"]] = relationship(
        "Item", back_populates="note", cascade="all, delete-orphan",
    )


# ── Link ─────────────────────────────────────────────────────────────────────

class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_note_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_note_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    link_type: Mapped[str] = mapped_column(
        String, nullable=False, default="related"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    from_note: Mapped["Note"] = relationship(
        "Note", foreign_keys=[from_note_id], back_populates="links_from"
    )
    to_note: Mapped["Note"] = relationship(
        "Note", foreign_keys=[to_note_id], back_populates="links_to"
    )


# ── History ───────────────────────────────────────────────────────────────────

class History(Base):
    __tablename__ = "history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    note: Mapped["Note"] = relationship("Note", back_populates="history_entries")


# ── Notification ─────────────────────────────────────────────────────────────

class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False, default="telegram")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    note: Mapped[Optional["Note"]] = relationship(
        "Note", back_populates="notifications"
    )


# ── Setting ───────────────────────────────────────────────────────────────────

class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


# ── ChatMessage ───────────────────────────────────────────────────────────────

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ── LlmUsage ─────────────────────────────────────────────────────────────────

class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    task: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="SET NULL"), nullable=True
    )

    note: Mapped[Optional["Note"]] = relationship("Note", back_populates="llm_usages")


# ── Event ─────────────────────────────────────────────────────────────────────

class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    note_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ── Task ─────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", index=True
    )
    note_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    note: Mapped[Optional["Note"]] = relationship("Note", back_populates="tasks")


# ── Item ─────────────────────────────────────────────────────────────────────

class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    note: Mapped["Note"] = relationship("Note", back_populates="items")


# ── Feedback ─────────────────────────────────────────────────────────────────

class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    field: Mapped[str] = mapped_column(String, nullable=False)
    llm_value: Mapped[str] = mapped_column(String, nullable=False)
    user_value: Mapped[str] = mapped_column(String, nullable=False)
    note_content_snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
