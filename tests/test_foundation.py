"""Tests for the four foundation-library integrations in aumai-handoff.

Covers:
- async_core.py  : AsyncHandoffManager (25 tests)
- store.py       : HandoffStore (15 tests)
- smart_routing.py : SmartRouter + RoutingDecision (15 tests)
- integration.py : HandoffIntegration + AumOS + EventBus (10 tests)

Total: 65 tests.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aumai_integration import AumOS, Event, EventBus
from aumai_llm_core import ProviderRegistry

from aumai_handoff.async_core import AsyncHandoffManager, AsyncHandoffManagerConfig
from aumai_handoff.integration import (
    EVENT_COMPLETED,
    EVENT_FAILED,
    EVENT_INITIATED,
    EVENT_REJECTED,
    HandoffIntegration,
    HandoffIntegrationConfig,
)
from aumai_handoff.models import (
    HandoffRecord,
    HandoffRequest,
    HandoffResponse,
    HandoffStatus,
)
from aumai_handoff.smart_routing import (
    RoutingDecision,
    SmartRouter,
    SmartRouterConfig,
    _MOCK_PROVIDER_KEY,
    _register_mock_provider,
    make_mock_smart_router,
)
from aumai_handoff.store import HandoffMetrics, HandoffStore, HandoffStoreConfig


# ===========================================================================
# Shared fixtures
# ===========================================================================


@pytest.fixture()
def basic_request() -> HandoffRequest:
    return HandoffRequest(
        from_agent="agent-alpha",
        to_agent="agent-beta",
        task_description="Analyze the sales dataset and produce a report.",
        priority=5,
    )


@pytest.fixture()
def high_priority_request() -> HandoffRequest:
    return HandoffRequest(
        from_agent="agent-alpha",
        to_agent="agent-beta",
        task_description="Critical: process payroll immediately.",
        priority=10,
    )


@pytest.fixture()
def async_manager_config() -> AsyncHandoffManagerConfig:
    return AsyncHandoffManagerConfig(name="test-handoff-manager")


@pytest.fixture()
def async_manager(async_manager_config: AsyncHandoffManagerConfig) -> AsyncHandoffManager:
    return AsyncHandoffManager(async_manager_config)


@pytest.fixture()
async def started_async_manager(async_manager: AsyncHandoffManager) -> AsyncHandoffManager:
    await async_manager.start()
    yield async_manager
    await async_manager.stop()


@pytest.fixture()
async def memory_store() -> HandoffStore:
    store = HandoffStore.memory()
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture()
def agent_registry() -> dict[str, list[str]]:
    return {
        "agent-alpha": ["python", "data-analysis", "reporting"],
        "agent-beta": ["java", "data-analysis", "database"],
        "agent-gamma": ["reporting", "visualization", "pdf"],
    }


@pytest.fixture()
def aumos() -> AumOS:
    return AumOS()


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def integration(aumos: AumOS, bus: EventBus) -> HandoffIntegration:
    config = HandoffIntegrationConfig(subscribe_to_capability_events=True)
    return HandoffIntegration(aumos=aumos, bus=bus, config=config)


# ===========================================================================
# 1. AsyncHandoffManager — 25 tests
# ===========================================================================


class TestAsyncHandoffManagerConfig:
    def test_config_name_is_required(self) -> None:
        config = AsyncHandoffManagerConfig(name="my-manager")
        assert config.name == "my-manager"

    def test_config_defaults(self) -> None:
        config = AsyncHandoffManagerConfig(name="x")
        assert config.max_concurrency == 100
        assert config.shutdown_timeout_seconds == 30.0

    def test_config_custom_concurrency(self) -> None:
        config = AsyncHandoffManagerConfig(name="x", max_concurrency=10)
        assert config.max_concurrency == 10


class TestAsyncHandoffManagerLifecycle:
    async def test_start_and_stop(self, async_manager: AsyncHandoffManager) -> None:
        await async_manager.start()
        await async_manager.stop()

    async def test_health_check_returns_true(
        self, async_manager: AsyncHandoffManager
    ) -> None:
        result = await async_manager.health_check()
        assert result is True

    async def test_emitter_is_accessible(
        self, async_manager: AsyncHandoffManager
    ) -> None:
        from aumai_async_core import AsyncEventEmitter
        assert isinstance(async_manager.emitter, AsyncEventEmitter)


class TestAsyncHandoffManagerInitiate:
    async def test_initiate_returns_pending_record(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        assert isinstance(record, HandoffRecord)
        assert record.status == HandoffStatus.pending

    async def test_initiate_assigns_unique_ids(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        r1 = await started_async_manager.initiate(basic_request)
        r2 = await started_async_manager.initiate(basic_request)
        assert r1.record_id != r2.record_id

    async def test_initiate_stores_request_fields(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        assert record.request.from_agent == "agent-alpha"
        assert record.request.to_agent == "agent-beta"
        assert record.request.priority == 5

    async def test_initiate_emits_created_event(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        emitted: list[dict[str, Any]] = []

        @started_async_manager.emitter.on_event("handoff.created")
        async def handler(**kwargs: Any) -> None:
            emitted.append(kwargs)

        await started_async_manager.initiate(basic_request)
        assert len(emitted) == 1
        assert emitted[0]["from_agent"] == "agent-alpha"

    async def test_initiate_increments_request_count(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        before = started_async_manager._request_count
        await started_async_manager.initiate(basic_request)
        assert started_async_manager._request_count == before + 1


class TestAsyncHandoffManagerAccept:
    async def test_accept_transitions_to_accepted(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        accepted = await started_async_manager.accept(record.record_id)
        assert accepted.status == HandoffStatus.accepted
        assert accepted.response is not None
        assert accepted.response.accepted is True

    async def test_accept_emits_event(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        emitted: list[dict[str, Any]] = []

        @started_async_manager.emitter.on_event("handoff.accepted")
        async def handler(**kwargs: Any) -> None:
            emitted.append(kwargs)

        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        assert len(emitted) == 1

    async def test_accept_unknown_id_raises_key_error(
        self,
        started_async_manager: AsyncHandoffManager,
    ) -> None:
        with pytest.raises(KeyError):
            await started_async_manager.accept("does-not-exist")

    async def test_accept_already_accepted_raises_value_error(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        with pytest.raises(ValueError, match="Cannot accept"):
            await started_async_manager.accept(record.record_id)


class TestAsyncHandoffManagerReject:
    async def test_reject_transitions_to_rejected(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        rejected = await started_async_manager.reject(
            record.record_id, "Agent unavailable"
        )
        assert rejected.status == HandoffStatus.rejected
        assert rejected.response is not None
        assert rejected.response.accepted is False
        assert "unavailable" in rejected.response.reason

    async def test_reject_accepted_raises_value_error(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        with pytest.raises(ValueError, match="Cannot reject"):
            await started_async_manager.reject(record.record_id, "too late")

    async def test_reject_emits_failed_event(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        emitted: list[dict[str, Any]] = []

        @started_async_manager.emitter.on_event("handoff.failed")
        async def handler(**kwargs: Any) -> None:
            emitted.append(kwargs)

        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.reject(record.record_id, "no capacity")
        assert len(emitted) == 1
        assert emitted[0]["reason"] == "no capacity"


class TestAsyncHandoffManagerComplete:
    async def test_complete_from_accepted(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        completed = await started_async_manager.complete(
            record.record_id, {"rows": 42}
        )
        assert completed.status == HandoffStatus.completed
        assert completed.result["rows"] == 42

    async def test_complete_from_in_progress(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        await started_async_manager.start_work(record.record_id)
        completed = await started_async_manager.complete(
            record.record_id, {"status": "ok"}
        )
        assert completed.status == HandoffStatus.completed

    async def test_complete_pending_raises(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        with pytest.raises(ValueError, match="Cannot complete"):
            await started_async_manager.complete(record.record_id, {})

    async def test_complete_emits_event(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        emitted: list[dict[str, Any]] = []

        @started_async_manager.emitter.on_event("handoff.completed")
        async def handler(**kwargs: Any) -> None:
            emitted.append(kwargs)

        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        await started_async_manager.complete(record.record_id, {"done": True})
        assert len(emitted) == 1
        assert emitted[0]["result"] == {"done": True}


class TestAsyncHandoffManagerStartWork:
    async def test_start_work_transitions_to_in_progress(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        in_prog = await started_async_manager.start_work(record.record_id)
        assert in_prog.status == HandoffStatus.in_progress

    async def test_start_work_pending_raises(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        with pytest.raises(ValueError, match="Cannot start"):
            await started_async_manager.start_work(record.record_id)

    async def test_start_work_emits_started_event(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        emitted: list[dict[str, Any]] = []

        @started_async_manager.emitter.on_event("handoff.started")
        async def handler(**kwargs: Any) -> None:
            emitted.append(kwargs)

        record = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(record.record_id)
        await started_async_manager.start_work(record.record_id)
        assert len(emitted) == 1


class TestAsyncHandoffManagerListAndGet:
    async def test_get_existing_record(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        record = await started_async_manager.initiate(basic_request)
        fetched = await started_async_manager.get(record.record_id)
        assert fetched.record_id == record.record_id

    async def test_get_missing_raises(
        self, started_async_manager: AsyncHandoffManager
    ) -> None:
        with pytest.raises(KeyError):
            await started_async_manager.get("no-such-id")

    async def test_list_records_all(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        await started_async_manager.initiate(basic_request)
        await started_async_manager.initiate(basic_request)
        records = await started_async_manager.list_records()
        assert len(records) == 2

    async def test_list_records_filtered_by_status(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        r1 = await started_async_manager.initiate(basic_request)
        r2 = await started_async_manager.initiate(basic_request)
        await started_async_manager.accept(r2.record_id)

        pending = await started_async_manager.list_records(
            status=HandoffStatus.pending
        )
        accepted = await started_async_manager.list_records(
            status=HandoffStatus.accepted
        )
        assert len(pending) == 1
        assert len(accepted) == 1
        assert pending[0].record_id == r1.record_id

    async def test_list_records_sorted_by_created_at(
        self,
        started_async_manager: AsyncHandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        for _ in range(4):
            await started_async_manager.initiate(basic_request)
        records = await started_async_manager.list_records()
        timestamps = [r.created_at for r in records]
        assert timestamps == sorted(timestamps)


# ===========================================================================
# 2. HandoffStore — 15 tests
# ===========================================================================


class TestHandoffStoreMeta:
    def test_memory_factory_returns_store(self) -> None:
        store = HandoffStore.memory()
        assert isinstance(store, HandoffStore)

    def test_config_defaults(self) -> None:
        config = HandoffStoreConfig()
        assert config.backend == "sqlite"
        assert "aumai_handoff" in config.database_url

    async def test_initialize_prepares_repo(
        self, memory_store: HandoffStore
    ) -> None:
        # Initialized in fixture; repo should be accessible.
        assert memory_store._repo is not None

    async def test_close_is_idempotent(
        self, memory_store: HandoffStore
    ) -> None:
        await memory_store.close()
        await memory_store.close()  # Should not raise.

    async def test_operations_before_init_raise(self) -> None:
        store = HandoffStore.memory()
        record = HandoffRecord(
            record_id="x",
            request=HandoffRequest(
                from_agent="a",
                to_agent="b",
                task_description="t",
            ),
        )
        with pytest.raises(RuntimeError, match="initialize"):
            await store.save(record)


class TestHandoffStoreSaveAndGet:
    async def test_save_and_get_roundtrip(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        record = HandoffRecord(
            record_id="r-001",
            request=basic_request,
        )
        await memory_store.save(record)
        retrieved = await memory_store.get("r-001")
        assert retrieved is not None
        assert retrieved.record_id == "r-001"
        assert retrieved.status == HandoffStatus.pending

    async def test_get_nonexistent_returns_none(
        self, memory_store: HandoffStore
    ) -> None:
        result = await memory_store.get("ghost")
        assert result is None

    async def test_save_updates_existing_record(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        record = HandoffRecord(record_id="r-002", request=basic_request)
        await memory_store.save(record)
        record.status = HandoffStatus.accepted
        record.response = HandoffResponse(accepted=True, reason="ok")
        await memory_store.save(record)

        retrieved = await memory_store.get("r-002")
        assert retrieved is not None
        assert retrieved.status == HandoffStatus.accepted

    async def test_delete_existing_record(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        record = HandoffRecord(record_id="r-del", request=basic_request)
        await memory_store.save(record)
        deleted = await memory_store.delete("r-del")
        assert deleted is True
        assert await memory_store.get("r-del") is None

    async def test_delete_nonexistent_returns_false(
        self, memory_store: HandoffStore
    ) -> None:
        result = await memory_store.delete("no-such")
        assert result is False


class TestHandoffStoreQueries:
    async def _seed(
        self, store: HandoffStore, request: HandoffRequest, count: int = 3
    ) -> list[HandoffRecord]:
        records = []
        for i in range(count):
            r = HandoffRecord(record_id=f"r-seed-{i}", request=request)
            await store.save(r)
            records.append(r)
        return records

    async def test_get_pending_handoffs(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        records = await self._seed(memory_store, basic_request, 3)
        # Mark one as accepted.
        records[0].status = HandoffStatus.accepted
        records[0].response = HandoffResponse(accepted=True)
        await memory_store.save(records[0])

        pending = await memory_store.get_pending_handoffs()
        assert len(pending) == 2
        assert all(r.status == HandoffStatus.pending for r in pending)

    async def test_get_handoffs_by_agent_from(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        await self._seed(memory_store, basic_request, 2)
        other_req = HandoffRequest(
            from_agent="agent-gamma",
            to_agent="agent-alpha",
            task_description="Other task.",
        )
        r = HandoffRecord(record_id="r-other", request=other_req)
        await memory_store.save(r)

        results = await memory_store.get_handoffs_by_agent(
            "agent-alpha", role="from"
        )
        assert all(
            r.request.from_agent == "agent-alpha" for r in results
        )

    async def test_get_handoffs_by_agent_either(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        await self._seed(memory_store, basic_request, 2)
        results = await memory_store.get_handoffs_by_agent(
            "agent-beta", role="either"
        )
        assert len(results) >= 2

    async def test_get_handoff_history_limit(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        await self._seed(memory_store, basic_request, 5)
        history = await memory_store.get_handoff_history(limit=2)
        assert len(history) <= 2

    async def test_get_all_with_pagination(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        await self._seed(memory_store, basic_request, 5)
        page = await memory_store.get_all(limit=2, offset=0)
        assert len(page) <= 2


class TestHandoffStoreMetrics:
    async def test_metrics_empty_store(
        self, memory_store: HandoffStore
    ) -> None:
        metrics = await memory_store.get_handoff_metrics()
        assert isinstance(metrics, HandoffMetrics)
        assert metrics.total == 0
        assert metrics.avg_duration_seconds is None
        assert metrics.completion_rate is None

    async def test_metrics_counts_by_status(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        for i in range(3):
            r = HandoffRecord(record_id=f"m-{i}", request=basic_request)
            await memory_store.save(r)

        r_completed = HandoffRecord(record_id="m-c", request=basic_request)
        r_completed.status = HandoffStatus.completed
        r_completed.response = HandoffResponse(accepted=True)
        await memory_store.save(r_completed)

        metrics = await memory_store.get_handoff_metrics()
        assert metrics.total == 4
        assert metrics.by_status.get("pending", 0) == 3
        assert metrics.by_status.get("completed", 0) == 1

    async def test_metrics_completion_rate(
        self,
        memory_store: HandoffStore,
        basic_request: HandoffRequest,
    ) -> None:
        completed = HandoffRecord(record_id="met-c1", request=basic_request)
        completed.status = HandoffStatus.completed
        completed.response = HandoffResponse(accepted=True)
        await memory_store.save(completed)

        failed = HandoffRecord(record_id="met-f1", request=basic_request)
        failed.status = HandoffStatus.failed
        failed.response = HandoffResponse(accepted=False, reason="err")
        await memory_store.save(failed)

        metrics = await memory_store.get_handoff_metrics()
        assert metrics.completion_rate == pytest.approx(0.5)


# ===========================================================================
# 3. SmartRouter — 15 tests
# ===========================================================================


class TestRoutingDecision:
    def test_routing_decision_valid(self) -> None:
        decision = RoutingDecision(
            target_agent="agent-alpha",
            confidence=0.9,
            reasoning="Best match.",
        )
        assert decision.target_agent == "agent-alpha"
        assert decision.confidence == 0.9

    def test_routing_decision_confidence_bounds_low(self) -> None:
        with pytest.raises(Exception):
            RoutingDecision(
                target_agent="a", confidence=-0.1, reasoning="x"
            )

    def test_routing_decision_confidence_bounds_high(self) -> None:
        with pytest.raises(Exception):
            RoutingDecision(
                target_agent="a", confidence=1.5, reasoning="x"
            )

    def test_routing_decision_default_fallback_agents(self) -> None:
        d = RoutingDecision(
            target_agent="a", confidence=0.5, reasoning="r"
        )
        assert d.fallback_agents == []


class TestSmartRouterConfig:
    def test_defaults(self) -> None:
        config = SmartRouterConfig()
        assert config.provider == "mock"
        assert config.temperature == 0.2

    def test_custom_system_prompt(self) -> None:
        config = SmartRouterConfig(system_prompt="Custom prompt.")
        assert config.system_prompt == "Custom prompt."


class TestMakeMockSmartRouter:
    def test_factory_returns_smart_router(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(agent_ids)
        assert isinstance(router, SmartRouter)

    def test_factory_with_empty_agents(self) -> None:
        router = make_mock_smart_router([])
        assert isinstance(router, SmartRouter)

    async def test_route_returns_routing_decision(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(
            agent_ids=agent_ids,
            target_agent="agent-beta",
            confidence=0.88,
            reasoning="Beta has database capabilities.",
        )
        request = HandoffRequest(
            from_agent="agent-alpha",
            to_agent="",
            task_description="Query the production database.",
        )
        decision = await router.route(request)
        assert isinstance(decision, RoutingDecision)
        assert decision.target_agent == "agent-beta"
        assert decision.confidence == pytest.approx(0.88)

    async def test_route_decision_includes_reasoning(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(
            agent_ids=agent_ids,
            target_agent="agent-gamma",
            reasoning="Gamma specializes in visualization.",
        )
        request = HandoffRequest(
            from_agent="agent-alpha",
            to_agent="",
            task_description="Create a chart of sales data.",
        )
        decision = await router.route(request)
        assert "visualization" in decision.reasoning

    async def test_route_includes_fallback_agents(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(
            agent_ids=agent_ids,
            target_agent=agent_ids[0],
        )
        request = HandoffRequest(
            from_agent="agent-outside",
            to_agent="",
            task_description="Process data.",
        )
        decision = await router.route(request)
        # fallback_agents should include all agents except the target.
        assert decision.target_agent not in decision.fallback_agents


class TestSmartRouterRegistryManagement:
    def test_register_agent(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(agent_ids)
        router.register_agent("agent-delta", ["rust", "systems"])
        assert "agent-delta" in router.list_agents()
        assert router.list_agents()["agent-delta"] == ["rust", "systems"]

    def test_unregister_agent(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(agent_ids)
        router.unregister_agent("agent-alpha")
        assert "agent-alpha" not in router.list_agents()

    def test_unregister_nonexistent_is_silent(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(agent_ids)
        router.unregister_agent("no-such-agent")  # No exception.

    def test_list_agents_returns_copy(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(agent_ids)
        listing = router.list_agents()
        listing["injected"] = ["fake"]
        assert "injected" not in router.list_agents()


class TestSmartRouterFallback:
    async def test_route_with_fallback_uses_heuristic_on_failure(
        self, agent_registry: dict[str, list[str]]
    ) -> None:
        """When LLM fails, heuristic fallback must still return a decision."""
        agent_ids = list(agent_registry.keys())
        router = make_mock_smart_router(agent_ids, target_agent=agent_ids[0])

        # Patch client to raise so fallback is exercised.
        original_route = router.route

        async def _bad_route(
            request: HandoffRequest, extra_context: dict | None = None
        ) -> RoutingDecision:
            raise RuntimeError("LLM unavailable")

        router.route = _bad_route  # type: ignore[method-assign]

        request = HandoffRequest(
            from_agent="agent-alpha",
            to_agent="",
            task_description="Visualize the data.",
        )
        decision = await router.route_with_fallback(request)
        assert isinstance(decision, RoutingDecision)
        assert decision.target_agent != ""
        assert decision.confidence == pytest.approx(0.4)

        router.route = original_route  # type: ignore[method-assign]


# ===========================================================================
# 4. HandoffIntegration — 10 tests
# ===========================================================================


class TestHandoffIntegrationRegistration:
    def test_register_adds_service_to_aumos(
        self, integration: HandoffIntegration, aumos: AumOS
    ) -> None:
        integration.register()
        service = aumos.get_service("handoff")
        assert service is not None
        assert "agent-handoff" in service.capabilities
        assert "smart-routing" in service.capabilities

    def test_register_is_idempotent(
        self, integration: HandoffIntegration, aumos: AumOS
    ) -> None:
        integration.register()
        integration.register()  # Should not raise or double-register.
        assert aumos.get_service("handoff") is not None

    def test_unregister_removes_service(
        self, integration: HandoffIntegration, aumos: AumOS
    ) -> None:
        integration.register()
        integration.unregister()
        assert aumos.get_service("handoff") is None

    def test_unregister_before_register_is_silent(
        self, integration: HandoffIntegration
    ) -> None:
        integration.unregister()  # Should not raise.

    def test_custom_config_name(
        self, aumos: AumOS, bus: EventBus
    ) -> None:
        config = HandoffIntegrationConfig(
            service_name="handoff-v2",
            subscribe_to_capability_events=False,
        )
        integ = HandoffIntegration(aumos=aumos, bus=bus, config=config)
        integ.register()
        assert aumos.get_service("handoff-v2") is not None


class TestHandoffIntegrationEventPublishing:
    def _make_record(
        self, request: HandoffRequest, status: HandoffStatus = HandoffStatus.pending
    ) -> HandoffRecord:
        record = HandoffRecord(
            record_id="evt-test-001",
            request=request,
            status=status,
        )
        return record

    async def test_publish_initiated_event(
        self,
        integration: HandoffIntegration,
        bus: EventBus,
        basic_request: HandoffRequest,
    ) -> None:
        integration.register()
        received: list[Event] = []

        async def _on_event(e: Event) -> None:
            received.append(e)

        bus.subscribe(EVENT_INITIATED, _on_event)

        record = self._make_record(basic_request)
        await integration.publish_initiated(record)

        assert len(received) == 1
        event = received[0]
        assert event.event_type == EVENT_INITIATED
        assert event.data["record_id"] == "evt-test-001"
        assert event.data["from_agent"] == "agent-alpha"

    async def test_publish_completed_event(
        self,
        integration: HandoffIntegration,
        bus: EventBus,
        basic_request: HandoffRequest,
    ) -> None:
        integration.register()
        received: list[Event] = []

        async def _on_event(e: Event) -> None:
            received.append(e)

        bus.subscribe(EVENT_COMPLETED, _on_event)

        record = self._make_record(basic_request, status=HandoffStatus.completed)
        await integration.publish_completed(record, result={"rows": 100})

        assert len(received) == 1
        assert received[0].data["result"] == {"rows": 100}

    async def test_publish_failed_event(
        self,
        integration: HandoffIntegration,
        bus: EventBus,
        basic_request: HandoffRequest,
    ) -> None:
        integration.register()
        received: list[Event] = []

        async def _on_event(e: Event) -> None:
            received.append(e)

        bus.subscribe(EVENT_FAILED, _on_event)

        record = self._make_record(basic_request, status=HandoffStatus.failed)
        record.response = HandoffResponse(accepted=False, reason="Timeout")
        await integration.publish_failed(record, reason="Timeout")

        assert len(received) == 1
        assert received[0].data["reason"] == "Timeout"

    async def test_publish_rejected_event(
        self,
        integration: HandoffIntegration,
        bus: EventBus,
        basic_request: HandoffRequest,
    ) -> None:
        integration.register()
        received: list[Event] = []

        async def _on_event(e: Event) -> None:
            received.append(e)

        bus.subscribe(EVENT_REJECTED, _on_event)

        record = self._make_record(basic_request, status=HandoffStatus.rejected)
        await integration.publish_rejected(record, reason="No capacity")

        assert len(received) == 1
        assert received[0].data["reason"] == "No capacity"


class TestHandoffIntegrationCapabilityCache:
    async def test_capability_events_update_cache(
        self, integration: HandoffIntegration, bus: EventBus
    ) -> None:
        integration.register()

        await bus.publish_simple(
            "agent.capability.registered",
            source="registry",
            agent_id="agent-new",
            capabilities=["python", "ml"],
        )

        known = integration.get_known_agent_capabilities()
        assert "agent-new" in known
        assert "python" in known["agent-new"]

    async def test_find_agents_with_capability(
        self, integration: HandoffIntegration, bus: EventBus
    ) -> None:
        integration.register()

        await bus.publish_simple(
            "agent.capability.registered",
            source="registry",
            agent_id="ml-agent",
            capabilities=["python", "ml", "training"],
        )
        await bus.publish_simple(
            "agent.capability.registered",
            source="registry",
            agent_id="data-agent",
            capabilities=["python", "data-analysis"],
        )

        matches = integration.find_agents_with_capability("python")
        assert "ml-agent" in matches
        assert "data-agent" in matches

    async def test_unregister_event_removes_from_cache(
        self, integration: HandoffIntegration, bus: EventBus
    ) -> None:
        integration.register()

        await bus.publish_simple(
            "agent.capability.registered",
            source="registry",
            agent_id="temp-agent",
            capabilities=["temporary"],
        )
        assert "temp-agent" in integration.get_known_agent_capabilities()

        await bus.publish_simple(
            "agent.capability.unregistered",
            source="registry",
            agent_id="temp-agent",
        )
        assert "temp-agent" not in integration.get_known_agent_capabilities()
