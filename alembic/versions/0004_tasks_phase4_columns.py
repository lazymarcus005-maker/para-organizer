"""tasks_phase4_columns

Add the SB-02 task-delegation columns to the ``tasks`` table that the SQLite
``app/tasks.py`` writes (and that ``app/worker.py``'s future ``handle_task`` /
``app/routes/tasks.py``'s POST /api/tasks endpoints will need to read):

- ``task_type``       — kind of work to do ('general' / 'research' / 'code' / 'deploy' / 'review' / 'automation')
- ``result``          — text result the agent returns via POST /api/tasks/{id}/complete
- ``agent_id``        — which agent picked up the task (e.g. 'hermes-cron', 'hermes-primary')
- ``hermes_job_id``   — opaque id the dispatcher uses to correlate the task back to the remote job
- ``completed_at``    — when the task transitioned to a terminal state (completed / failed)

All columns are nullable (default null) so existing rows stay valid and the
table is forward-compatible with both the v4 SQLite schema (which already has
these) and any in-flight tasks that pre-date the migration.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("task_type", sa.String(), nullable=False, server_default="general"),
    )
    op.add_column("tasks", sa.Column("result", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("agent_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("hermes_job_id", sa.String(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_tasks_note", "tasks", ["note_id"])
    op.create_index("idx_tasks_agent", "tasks", ["agent_id"])


def downgrade() -> None:
    op.drop_index("idx_tasks_agent", table_name="tasks")
    op.drop_index("idx_tasks_note", table_name="tasks")
    op.drop_column("tasks", "completed_at")
    op.drop_column("tasks", "hermes_job_id")
    op.drop_column("tasks", "agent_id")
    op.drop_column("tasks", "result")
    op.drop_column("tasks", "task_type")
