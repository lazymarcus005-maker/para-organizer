"""initial_schema

Create all tables for the PARA Organizer PostgreSQL schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enable pgvector extension ──
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── Notes ──
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("para_category", sa.String(), nullable=False, server_default="inbox"),
        sa.Column("sub_category", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("priority", sa.String(), nullable=False, server_default="medium"),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("llm_model", sa.String(), nullable=True),
        sa.Column("llm_confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("llm_reasoning", sa.Text(), nullable=True),
        sa.Column("embedding_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("recurrence", postgresql.JSONB(), nullable=True),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),  # placeholder; real vector(768) below
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_notes_category", "notes", ["para_category"])
    op.create_index("idx_notes_status", "notes", ["status"])
    op.create_index("idx_notes_deadline", "notes", ["deadline"])
    op.create_index("idx_notes_source", "notes", ["source"])
    op.create_index("idx_notes_priority", "notes", ["priority"])

    # ── Links ──
    op.create_table(
        "links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_note_id", sa.Integer(), nullable=False),
        sa.Column("to_note_id", sa.Integer(), nullable=False),
        sa.Column("link_type", sa.String(), nullable=False, server_default="related"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["from_note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_links_from", "links", ["from_note_id"])
    op.create_index("idx_links_to", "links", ["to_note_id"])

    # ── History ──
    op.create_table(
        "history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_history_note", "history", ["note_id"])

    # ── Notifications ──
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False, server_default="telegram"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_notif_status", "notifications", ["status"])
    op.create_index("idx_notif_scheduled", "notifications", ["scheduled_at"])

    # ── Settings ──
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    # ── Chat Messages ──
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_chat_messages_chat", "chat_messages", ["chat_id", "id"])

    # ── LLM Usage ──
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_llm_usage_ts", "llm_usage", ["ts"])

    # ── Events ──
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_events_type", "events", ["event_type"])
    op.create_index("idx_events_status", "events", ["status"])

    # ── Tasks ──
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_tasks_status", "tasks", ["status"])

    # ── Items ──
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_items_note", "items", ["note_id"])

    # ── Feedback ──
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("llm_value", sa.String(), nullable=False),
        sa.Column("user_value", sa.String(), nullable=False),
        sa.Column("note_content_snippet", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── pgvector embedding column ──
    op.execute(
        "ALTER TABLE notes ADD COLUMN embedding vector(768)"
    )

    # ── Full-text search vector (tsvector) ──
    op.execute(
        "ALTER TABLE notes ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS ("
        "  to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))"
        ") STORED"
    )
    op.create_index("idx_notes_search_vector", "notes", ["search_vector"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("items")
    op.drop_table("tasks")
    op.drop_table("events")
    op.drop_table("llm_usage")
    op.drop_table("chat_messages")
    op.drop_table("settings")
    op.drop_table("notifications")
    op.drop_table("history")
    op.drop_table("links")
    op.drop_table("notes")
    op.execute("DROP EXTENSION IF EXISTS vector")
