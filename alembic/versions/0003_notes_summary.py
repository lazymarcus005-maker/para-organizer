"""notes_summary

Add notes.summary — declared on models_v2.Note (used by app/worker.py's
handle_distill for archive-time note distillation) but never added to the
Postgres schema by 0001_initial_schema.py. Every SELECT * FROM notes (i.e.
every ORM query against Note) fails with UndefinedColumnError without it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-06
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("notes", "summary")
