"""Pydantic models for aumai-handoff."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "HandoffStatus",
    "HandoffRequest",
    "HandoffResponse",
    "HandoffRecord",
]


class HandoffStatus(str, Enum):
    """Lifecycle states for a handoff record."""

    pending = "pending"
    accepted = "accepted"
    in_progress = "in_progress"
    completed = "completed"
    rejected = "rejected"
    failed = "failed"


class HandoffRequest(BaseModel):
    """A request to hand off a task from one agent to another."""

    from_agent: str
    to_agent: str
    task_description: str
    context: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    deadline: datetime | None = None


class HandoffResponse(BaseModel):
    """The receiving agent's response to a handoff request."""

    accepted: bool
    reason: str = ""
    estimated_completion: datetime | None = None


class HandoffRecord(BaseModel):
    """Full lifecycle record of a handoff."""

    record_id: str
    request: HandoffRequest
    response: HandoffResponse | None = None
    status: HandoffStatus = HandoffStatus.pending
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"frozen": False}
