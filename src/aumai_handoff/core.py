"""Core logic for aumai-handoff."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .models import (
    HandoffRecord,
    HandoffRequest,
    HandoffResponse,
    HandoffStatus,
)

__all__ = [
    "AgentCapabilityRegistry",
    "HandoffManager",
    "HandoffRouter",
]


class AgentCapabilityRegistry(BaseModel):
    """Tracks available agents and their capabilities for routing."""

    agents: dict[str, list[str]] = {}

    model_config = {"frozen": False}

    def register(self, agent_id: str, capabilities: list[str]) -> None:
        """Register an agent with its capabilities."""
        self.agents[agent_id] = list(capabilities)

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from the registry."""
        self.agents.pop(agent_id, None)

    def find_capable(self, required_capabilities: list[str]) -> list[str]:
        """
        Return agent IDs that satisfy all required capabilities.

        Sorted by number of matching capabilities descending.
        """
        required = set(required_capabilities)
        results: list[tuple[str, int]] = []
        for agent_id, caps in self.agents.items():
            caps_set = set(caps)
            if required.issubset(caps_set):
                results.append((agent_id, len(caps_set & required)))
        results.sort(key=lambda x: x[1], reverse=True)
        return [agent_id for agent_id, _ in results]


class HandoffManager:
    """
    Creates and manages the lifecycle of handoff records.

    Records are stored in-memory.  Serialize with ``export()`` /
    restore with ``import_records()``.
    """

    def __init__(self) -> None:
        self._records: dict[str, HandoffRecord] = {}

    def create_handoff(self, request: HandoffRequest) -> HandoffRecord:
        """Create a new handoff record in PENDING state."""
        record = HandoffRecord(
            record_id=str(uuid.uuid4()),
            request=request,
            status=HandoffStatus.pending,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._records[record.record_id] = record
        return record

    def accept(self, record_id: str) -> HandoffRecord:
        """Transition a PENDING handoff to ACCEPTED."""
        record = self._get_or_raise(record_id)
        if record.status != HandoffStatus.pending:
            raise ValueError(
                f"Cannot accept handoff in state {record.status.value!r}."
            )
        record.response = HandoffResponse(
            accepted=True,
            reason="Accepted by receiving agent.",
        )
        record.status = HandoffStatus.accepted
        record.updated_at = datetime.utcnow()
        return record

    def start(self, record_id: str) -> HandoffRecord:
        """Transition an ACCEPTED handoff to IN_PROGRESS."""
        record = self._get_or_raise(record_id)
        if record.status != HandoffStatus.accepted:
            raise ValueError(
                f"Cannot start handoff in state {record.status.value!r}."
            )
        record.status = HandoffStatus.in_progress
        record.updated_at = datetime.utcnow()
        return record

    def complete(
        self, record_id: str, result: dict[str, Any]
    ) -> HandoffRecord:
        """Mark a handoff as COMPLETED and store its result."""
        record = self._get_or_raise(record_id)
        if record.status not in (
            HandoffStatus.accepted,
            HandoffStatus.in_progress,
        ):
            raise ValueError(
                f"Cannot complete handoff in state {record.status.value!r}."
            )
        record.result = result
        record.status = HandoffStatus.completed
        record.updated_at = datetime.utcnow()
        return record

    def reject(self, record_id: str, reason: str) -> HandoffRecord:
        """Reject a PENDING handoff with a reason."""
        record = self._get_or_raise(record_id)
        if record.status != HandoffStatus.pending:
            raise ValueError(
                f"Cannot reject handoff in state {record.status.value!r}."
            )
        record.response = HandoffResponse(accepted=False, reason=reason)
        record.status = HandoffStatus.rejected
        record.updated_at = datetime.utcnow()
        return record

    def fail(self, record_id: str, reason: str) -> HandoffRecord:
        """Mark an in-progress handoff as FAILED."""
        record = self._get_or_raise(record_id)
        if record.status not in (
            HandoffStatus.accepted,
            HandoffStatus.in_progress,
        ):
            raise ValueError(
                f"Cannot fail handoff in state {record.status.value!r}."
            )
        record.response = HandoffResponse(accepted=False, reason=reason)
        record.status = HandoffStatus.failed
        record.updated_at = datetime.utcnow()
        return record

    def get(self, record_id: str) -> HandoffRecord:
        """Retrieve a record by ID."""
        return self._get_or_raise(record_id)

    def list_records(
        self, status: HandoffStatus | None = None
    ) -> list[HandoffRecord]:
        """Return all records, optionally filtered by status."""
        records = list(self._records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: r.created_at)

    def export(self) -> list[dict[str, Any]]:
        """Serialize all records to a list of dicts."""
        return [r.model_dump(mode="json") for r in self._records.values()]

    def import_records(self, data: list[dict[str, Any]]) -> None:
        """Restore records from serialized dicts."""
        for item in data:
            record = HandoffRecord.model_validate(item)
            self._records[record.record_id] = record

    def _get_or_raise(self, record_id: str) -> HandoffRecord:
        record = self._records.get(record_id)
        if record is None:
            raise KeyError(f"No handoff record found with id {record_id!r}.")
        return record


class HandoffRouter:
    """
    Routes handoff requests to the best available agent.

    Uses ``AgentCapabilityRegistry`` to match task keywords against
    registered agent capabilities.
    """

    def __init__(self, registry: AgentCapabilityRegistry) -> None:
        self._registry = registry

    def route(
        self,
        request: HandoffRequest,
        preferred_capabilities: list[str] | None = None,
    ) -> str | None:
        """
        Return the agent_id best suited to handle *request*.

        Returns ``None`` when no capable agent is found.
        """
        caps = preferred_capabilities or _extract_keywords(
            request.task_description
        )
        candidates = self._registry.find_capable(caps)
        if not candidates:
            all_agents = list(self._registry.agents.keys())
            candidates = [a for a in all_agents if a != request.from_agent]
        return candidates[0] if candidates else None


def _extract_keywords(text: str) -> list[str]:
    """Return lowercased words longer than 4 characters from *text*."""
    words = text.lower().split()
    return [w.strip(".,;:!?") for w in words if len(w) > 4]
