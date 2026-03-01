"""SQLite-backed persistence for aumai-handoff using aumai-store."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from aumai_store import Repository, Store, StoreConfig
from pydantic import BaseModel, Field, model_validator

from .models import HandoffRecord, HandoffStatus

__all__ = [
    "HandoffStore",
    "HandoffStoreConfig",
    "HandoffMetrics",
]


class HandoffStoreConfig(BaseModel):
    """Configuration for :class:`HandoffStore`.

    Args:
        database_url: SQLite URL (default: in-memory ``sqlite://``).
        table_prefix: Prefix for all created tables.
        backend: Storage backend — ``"memory"`` or ``"sqlite"``.
    """

    database_url: str = "sqlite:///aumai_handoff.db"
    table_prefix: str = "handoff_"
    backend: str = "sqlite"

    model_config = {"frozen": False}


class HandoffMetrics(BaseModel):
    """Aggregate metrics across stored handoff records.

    Attributes:
        total: Total number of records.
        by_status: Count per :class:`~aumai_handoff.models.HandoffStatus`.
        avg_duration_seconds: Average seconds from creation to completion
            for completed records (``None`` when no completions exist).
        completion_rate: Fraction of terminal records that ended in
            COMPLETED (0.0–1.0, or ``None`` when no terminal records).
    """

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    avg_duration_seconds: float | None = None
    completion_rate: float | None = None


class _StoredHandoff(BaseModel):
    """Flat representation persisted to the store backend.

    ``HandoffRecord`` contains nested Pydantic models which the generic
    :class:`~aumai_store.Repository` cannot introspect column-by-column.
    We serialize the full record to a JSON string and store it in a
    single ``payload`` column, alongside indexed scalar fields.
    """

    id: str
    from_agent: str
    to_agent: str
    status: str
    priority: int
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601
    payload: str     # full HandoffRecord JSON

    model_config = {"frozen": False}

    @model_validator(mode="before")
    @classmethod
    def _reserialize_payload(cls, data: Any) -> Any:  # noqa: ANN401
        """Re-serialize payload if the store backend auto-parsed it to dict."""
        import json as _json

        if isinstance(data, dict):
            val = data.get("payload")
            if isinstance(val, dict):
                data["payload"] = _json.dumps(val)
        return data


def _to_stored(record: HandoffRecord) -> _StoredHandoff:
    """Convert a :class:`HandoffRecord` to a :class:`_StoredHandoff`."""
    return _StoredHandoff(
        id=record.record_id,
        from_agent=record.request.from_agent,
        to_agent=record.request.to_agent,
        status=record.status.value,
        priority=record.request.priority,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        payload=record.model_dump_json(),
    )


def _from_stored(stored: _StoredHandoff) -> HandoffRecord:
    """Convert a :class:`_StoredHandoff` back to a :class:`HandoffRecord`."""
    return HandoffRecord.model_validate_json(stored.payload)


class HandoffStore:
    """Persists :class:`~aumai_handoff.models.HandoffRecord` instances.

    Backed by :class:`~aumai_store.Store` (SQLite by default, or in-memory
    for testing via ``StoreConfig(backend="memory")``).

    Use :meth:`Store.memory` for a zero-configuration in-memory instance::

        store = HandoffStore.memory()
        record = await store.save(record)

    Example with SQLite::

        config = HandoffStoreConfig(database_url="sqlite:///handoffs.db")
        store = HandoffStore(config)
        await store.initialize()

        record = await store.save(record)
        records = await store.get_pending_handoffs()
        metrics = await store.get_handoff_metrics()
    """

    _TERMINAL_STATUSES = frozenset(
        [HandoffStatus.completed, HandoffStatus.rejected, HandoffStatus.failed]
    )

    def __init__(
        self,
        config: HandoffStoreConfig | None = None,
    ) -> None:
        effective = config or HandoffStoreConfig()
        self._store_config = StoreConfig(
            backend=effective.backend,  # type: ignore[arg-type]
            database_url=effective.database_url,
            table_prefix=effective.table_prefix,
            auto_migrate=True,
        )
        self._store: Store = Store(self._store_config)
        self._repo: Repository[_StoredHandoff] | None = None

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def memory(cls) -> "HandoffStore":
        """Return a fully initialized in-memory store (no I/O).

        Useful in unit tests — no file or network access required.
        """
        instance = cls(HandoffStoreConfig(backend="memory", database_url="sqlite://"))
        return instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the backing store and prepare the repository.

        Must be called once before any read/write operations.
        """
        await self._store.initialize()
        self._repo = await self._store.prepare_repository(
            _StoredHandoff, table_name="handoffs"
        )

    async def close(self) -> None:
        """Release store resources."""
        await self._store.close()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def save(self, record: HandoffRecord) -> HandoffRecord:
        """Persist or update a handoff record.

        If the record already exists it is overwritten (upsert semantics).

        Args:
            record: The :class:`~aumai_handoff.models.HandoffRecord` to save.

        Returns:
            The same record (passthrough for chaining).

        Raises:
            RuntimeError: If :meth:`initialize` has not been called.
        """
        repo = self._require_repo()
        stored = _to_stored(record)
        await repo.upsert(stored.id, stored)
        return record

    async def delete(self, record_id: str) -> bool:
        """Delete a record by ID.

        Args:
            record_id: ID of the record to delete.

        Returns:
            ``True`` if the record was deleted, ``False`` if not found.
        """
        repo = self._require_repo()
        return await repo.delete(record_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get(self, record_id: str) -> HandoffRecord | None:
        """Retrieve a single record by ID.

        Args:
            record_id: Unique record ID.

        Returns:
            The :class:`~aumai_handoff.models.HandoffRecord`, or ``None``.
        """
        repo = self._require_repo()
        stored = await repo.get(record_id)
        return _from_stored(stored) if stored else None

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HandoffRecord]:
        """Return all records with optional pagination.

        Args:
            limit: Maximum number of records to return.
            offset: Number of records to skip.

        Returns:
            Records sorted ascending by ``created_at``.
        """
        repo = self._require_repo()
        stored_list = await repo.all(limit=limit, offset=offset)
        records = [_from_stored(s) for s in stored_list]
        return sorted(records, key=lambda r: r.created_at)

    async def get_handoffs_by_agent(
        self,
        agent_id: str,
        role: str = "either",
    ) -> list[HandoffRecord]:
        """Return all records involving *agent_id*.

        Args:
            agent_id: The agent identifier to filter on.
            role: One of ``"from"`` (sender), ``"to"`` (receiver), or
                ``"either"`` (default — matches both roles).

        Returns:
            Records sorted ascending by ``created_at``.
        """
        repo = self._require_repo()
        results: list[HandoffRecord] = []

        if role in ("from", "either"):
            stored = await repo.find(from_agent=agent_id)
            results.extend(_from_stored(s) for s in stored)

        if role in ("to", "either"):
            stored = await repo.find(to_agent=agent_id)
            for s in stored:
                record = _from_stored(s)
                # Avoid duplicates when agent is both from and to.
                if all(r.record_id != record.record_id for r in results):
                    results.append(record)

        return sorted(results, key=lambda r: r.created_at)

    async def get_pending_handoffs(self) -> list[HandoffRecord]:
        """Return all records in PENDING state.

        Returns:
            Records sorted ascending by ``created_at``.
        """
        repo = self._require_repo()
        stored = await repo.find(status=HandoffStatus.pending.value)
        records = [_from_stored(s) for s in stored]
        return sorted(records, key=lambda r: r.created_at)

    async def get_handoff_history(
        self,
        agent_id: str | None = None,
        status: HandoffStatus | None = None,
        limit: int = 50,
    ) -> list[HandoffRecord]:
        """Return handoff history with optional filters.

        Args:
            agent_id: If provided, only records where this agent is the
                sender or receiver.
            status: If provided, only records with this status.
            limit: Maximum number of records.

        Returns:
            Records sorted *descending* by ``updated_at`` (most recent first).
        """
        repo = self._require_repo()

        filters: dict[str, Any] = {}
        if status is not None:
            filters["status"] = status.value

        if agent_id is not None:
            # Fetch by from_agent and to_agent separately, then merge.
            from_stored = await repo.find(from_agent=agent_id, **filters)
            to_stored = await repo.find(to_agent=agent_id, **filters)
            seen: set[str] = set()
            all_stored: list[_StoredHandoff] = []
            for s in from_stored + to_stored:
                if s.id not in seen:
                    seen.add(s.id)
                    all_stored.append(s)
        else:
            all_stored = await repo.find(**filters)

        records = [_from_stored(s) for s in all_stored]
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:limit]

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def get_handoff_metrics(self) -> HandoffMetrics:
        """Compute aggregate metrics across all stored records.

        Returns:
            A :class:`HandoffMetrics` snapshot.
        """
        repo = self._require_repo()
        all_stored = await repo.all(limit=10_000)

        if not all_stored:
            return HandoffMetrics()

        by_status: dict[str, int] = {}
        durations: list[float] = []
        terminal_count = 0
        completed_count = 0

        for stored in all_stored:
            status_value = stored.status
            by_status[status_value] = by_status.get(status_value, 0) + 1

            status = HandoffStatus(status_value)
            if status in self._TERMINAL_STATUSES:
                terminal_count += 1
                if status == HandoffStatus.completed:
                    completed_count += 1
                    try:
                        created = datetime.fromisoformat(stored.created_at)
                        updated = datetime.fromisoformat(stored.updated_at)
                        duration = (updated - created).total_seconds()
                        if duration >= 0:
                            durations.append(duration)
                    except ValueError:
                        pass  # skip malformed timestamps

        avg_duration = (
            sum(durations) / len(durations) if durations else None
        )
        completion_rate = (
            completed_count / terminal_count if terminal_count > 0 else None
        )

        return HandoffMetrics(
            total=len(all_stored),
            by_status=by_status,
            avg_duration_seconds=avg_duration,
            completion_rate=completion_rate,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_repo(self) -> Repository[_StoredHandoff]:
        if self._repo is None:
            raise RuntimeError(
                "HandoffStore has not been initialized. "
                "Call await store.initialize() first."
            )
        return self._repo
