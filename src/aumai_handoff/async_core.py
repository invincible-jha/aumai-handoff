"""Async API for aumai-handoff using aumai-async-core."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from aumai_async_core import AsyncEventEmitter, AsyncService, AsyncServiceConfig

from .models import (
    HandoffRecord,
    HandoffRequest,
    HandoffResponse,
    HandoffStatus,
)

__all__ = [
    "AsyncHandoffManager",
    "AsyncHandoffManagerConfig",
]


class AsyncHandoffManagerConfig(AsyncServiceConfig):
    """Configuration for :class:`AsyncHandoffManager`.

    Inherits all fields from :class:`~aumai_async_core.AsyncServiceConfig`:
    - ``name`` — service name
    - ``max_concurrency`` — max concurrent operations
    - ``shutdown_timeout_seconds`` — graceful shutdown window
    - ``health_check_interval_seconds`` — health-check cadence
    """


class AsyncHandoffManager(AsyncService):
    """Async lifecycle manager for agent handoff records.

    Wraps :class:`~aumai_handoff.core.HandoffManager` operations as
    ``async`` methods and emits events on every state transition via
    :class:`~aumai_async_core.AsyncEventEmitter`.

    Events emitted:
    - ``handoff.created`` — new record created
    - ``handoff.accepted`` — record moved to ACCEPTED
    - ``handoff.started`` — record moved to IN_PROGRESS
    - ``handoff.completed`` — record moved to COMPLETED
    - ``handoff.rejected`` — record moved to REJECTED
    - ``handoff.failed`` — record moved to FAILED

    Example::

        config = AsyncHandoffManagerConfig(name="handoff-manager")
        manager = AsyncHandoffManager(config)
        await manager.start()

        request = HandoffRequest(
            from_agent="agent-a",
            to_agent="agent-b",
            task_description="Process the dataset",
        )
        record = await manager.initiate(request)
        record = await manager.accept(record.record_id)
        record = await manager.complete(record.record_id, {"rows": 42})
        await manager.stop()
    """

    def __init__(self, config: AsyncHandoffManagerConfig) -> None:
        super().__init__(config)
        self._records: dict[str, HandoffRecord] = {}
        self._emitter: AsyncEventEmitter = AsyncEventEmitter()

    # ------------------------------------------------------------------
    # AsyncService hooks
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        """Called by :meth:`start`; no-op for the in-memory backend."""

    async def on_stop(self) -> None:
        """Called by :meth:`stop`; no-op for the in-memory backend."""

    async def health_check(self) -> bool:
        """Always healthy for the in-memory backend."""
        return True

    # ------------------------------------------------------------------
    # Event emitter access
    # ------------------------------------------------------------------

    @property
    def emitter(self) -> AsyncEventEmitter:
        """The underlying :class:`~aumai_async_core.AsyncEventEmitter`."""
        return self._emitter

    # ------------------------------------------------------------------
    # Async handoff operations
    # ------------------------------------------------------------------

    async def initiate(self, request: HandoffRequest) -> HandoffRecord:
        """Create a new handoff record in PENDING state.

        Args:
            request: The handoff request describing source agent, target
                agent, and task.

        Returns:
            The freshly created :class:`~aumai_handoff.models.HandoffRecord`.
        """
        await self.increment_request_count()
        record = HandoffRecord(
            record_id=str(uuid.uuid4()),
            request=request,
            status=HandoffStatus.pending,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        self._records[record.record_id] = record
        await self._emitter.emit(
            "handoff.created",
            record_id=record.record_id,
            from_agent=request.from_agent,
            to_agent=request.to_agent,
            task=request.task_description,
            priority=request.priority,
        )
        return record

    async def accept(self, record_id: str) -> HandoffRecord:
        """Accept a PENDING handoff, transitioning it to ACCEPTED.

        Args:
            record_id: ID of the handoff to accept.

        Returns:
            The updated :class:`~aumai_handoff.models.HandoffRecord`.

        Raises:
            KeyError: If no record exists for *record_id*.
            ValueError: If the record is not in PENDING state.
        """
        await self.increment_request_count()
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
        record.updated_at = datetime.now(tz=timezone.utc)
        await self._emitter.emit(
            "handoff.accepted",
            record_id=record_id,
            from_agent=record.request.from_agent,
            to_agent=record.request.to_agent,
        )
        return record

    async def reject(self, record_id: str, reason: str) -> HandoffRecord:
        """Reject a PENDING handoff.

        Args:
            record_id: ID of the handoff to reject.
            reason: Human-readable rejection reason.

        Returns:
            The updated :class:`~aumai_handoff.models.HandoffRecord`.

        Raises:
            KeyError: If no record exists for *record_id*.
            ValueError: If the record is not in PENDING state.
        """
        await self.increment_request_count()
        record = self._get_or_raise(record_id)
        if record.status != HandoffStatus.pending:
            raise ValueError(
                f"Cannot reject handoff in state {record.status.value!r}."
            )
        record.response = HandoffResponse(accepted=False, reason=reason)
        record.status = HandoffStatus.rejected
        record.updated_at = datetime.now(tz=timezone.utc)
        await self._emitter.emit(
            "handoff.failed",
            record_id=record_id,
            reason=reason,
            final_status=HandoffStatus.rejected.value,
        )
        return record

    async def complete(
        self, record_id: str, result: dict[str, Any]
    ) -> HandoffRecord:
        """Mark an ACCEPTED or IN_PROGRESS handoff as COMPLETED.

        Args:
            record_id: ID of the handoff to complete.
            result: Arbitrary result payload produced by the receiving agent.

        Returns:
            The updated :class:`~aumai_handoff.models.HandoffRecord`.

        Raises:
            KeyError: If no record exists for *record_id*.
            ValueError: If the record is not in ACCEPTED or IN_PROGRESS state.
        """
        await self.increment_request_count()
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
        record.updated_at = datetime.now(tz=timezone.utc)
        await self._emitter.emit(
            "handoff.completed",
            record_id=record_id,
            from_agent=record.request.from_agent,
            to_agent=record.request.to_agent,
            result=result,
        )
        return record

    async def start_work(self, record_id: str) -> HandoffRecord:
        """Transition an ACCEPTED handoff to IN_PROGRESS.

        Args:
            record_id: ID of the handoff to start.

        Returns:
            The updated :class:`~aumai_handoff.models.HandoffRecord`.

        Raises:
            KeyError: If no record exists for *record_id*.
            ValueError: If the record is not in ACCEPTED state.
        """
        await self.increment_request_count()
        record = self._get_or_raise(record_id)
        if record.status != HandoffStatus.accepted:
            raise ValueError(
                f"Cannot start handoff in state {record.status.value!r}."
            )
        record.status = HandoffStatus.in_progress
        record.updated_at = datetime.now(tz=timezone.utc)
        await self._emitter.emit(
            "handoff.started",
            record_id=record_id,
            to_agent=record.request.to_agent,
        )
        return record

    async def fail(self, record_id: str, reason: str) -> HandoffRecord:
        """Mark an ACCEPTED or IN_PROGRESS handoff as FAILED.

        Args:
            record_id: ID of the handoff to fail.
            reason: Human-readable failure reason.

        Returns:
            The updated :class:`~aumai_handoff.models.HandoffRecord`.

        Raises:
            KeyError: If no record exists for *record_id*.
            ValueError: If the record is not in ACCEPTED or IN_PROGRESS state.
        """
        await self.increment_request_count()
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
        record.updated_at = datetime.now(tz=timezone.utc)
        await self._emitter.emit(
            "handoff.failed",
            record_id=record_id,
            reason=reason,
            final_status=HandoffStatus.failed.value,
        )
        return record

    async def get(self, record_id: str) -> HandoffRecord:
        """Retrieve a single handoff record by ID.

        Args:
            record_id: Unique ID of the record.

        Returns:
            The :class:`~aumai_handoff.models.HandoffRecord`.

        Raises:
            KeyError: If the record does not exist.
        """
        return self._get_or_raise(record_id)

    async def list_records(
        self,
        status: HandoffStatus | None = None,
    ) -> list[HandoffRecord]:
        """Return all records, optionally filtered by *status*.

        Args:
            status: When provided, only records in this state are returned.

        Returns:
            Records sorted ascending by ``created_at``.
        """
        records = list(self._records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: r.created_at)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_raise(self, record_id: str) -> HandoffRecord:
        record = self._records.get(record_id)
        if record is None:
            raise KeyError(
                f"No handoff record found with id {record_id!r}."
            )
        return record
