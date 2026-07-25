"""Pydantic request/response models."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

PARA_CATEGORIES = ["inbox", "projects", "areas", "resources", "archives"]
STATUSES = ["active", "completed", "archived"]
PRIORITIES = ["low", "medium", "high", "urgent"]


class Note(BaseModel):
    id: int
    title: str
    content: str
    para_category: str = "inbox"
    sub_category: Optional[str] = None
    status: str = "active"
    priority: str = "medium"
    deadline: Optional[date] = None
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    source_metadata: dict = Field(default_factory=dict)
    llm_model: Optional[str] = None
    llm_confidence: float = 0.0
    llm_reasoning: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    archived_at: Optional[datetime] = None


class NoteCreate(BaseModel):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: str = "manual"
    auto_classify: bool = True
    tags_override: Optional[list[str]] = None


class CronNoteCreate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1)
    content: str = Field(min_length=1)
    source: str = "cron"
    auto_classify: bool = True
    tags_override: Optional[list[str]] = None


class NoteUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1)
    content: Optional[str] = Field(default=None, min_length=1)
    para_category: Optional[str] = None
    sub_category: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    deadline: Optional[date] = None
    tags: Optional[list[str]] = None


class NoteMove(BaseModel):
    para_category: str


class Link(BaseModel):
    id: int
    from_note_id: int
    to_note_id: int
    link_type: str = "related"
    created_at: datetime


class LinkCreate(BaseModel):
    from_note_id: int
    to_note_id: int
    link_type: str = "related"


class History(BaseModel):
    id: int
    note_id: int
    action: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    reason: Optional[str] = None
    timestamp: datetime


class Notification(BaseModel):
    id: int
    note_id: Optional[int] = None
    type: str
    channel: str = "telegram"
    status: str = "pending"
    scheduled_at: datetime
    sent_at: Optional[datetime] = None
    payload: Optional[dict] = None


class Stats(BaseModel):
    total_notes: int
    by_category: dict[str, int]
    by_status: dict[str, int]
    by_priority: dict[str, int]
    upcoming_deadlines: int
    avg_confidence: float


class Deadline(BaseModel):
    id: int
    title: str
    deadline: date
    days_left: int
    priority: str
