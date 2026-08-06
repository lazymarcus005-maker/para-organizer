"""feedback_note_id

Add feedback.note_id so classification corrections can be correlated back to
the note they were recorded against (needed by app/feedback.py's
get_few_shot_examples join and by the move_note/reclassify_note feedback
calls in app/routes_v2.py).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("note_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_feedback_note_id", "feedback", "notes", ["note_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("idx_feedback_note", "feedback", ["note_id"])


def downgrade() -> None:
    op.drop_index("idx_feedback_note", table_name="feedback")
    op.drop_constraint("fk_feedback_note_id", "feedback", type_="foreignkey")
    op.drop_column("feedback", "note_id")
