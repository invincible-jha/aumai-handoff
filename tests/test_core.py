"""Tests for aumai-handoff core module."""

from __future__ import annotations

import pytest

from aumai_handoff.core import (
    AgentCapabilityRegistry,
    HandoffManager,
    HandoffRouter,
    _extract_keywords,
)
from aumai_handoff.models import HandoffRecord, HandoffRequest, HandoffResponse, HandoffStatus


# ---------------------------------------------------------------------------
# AgentCapabilityRegistry tests
# ---------------------------------------------------------------------------


class TestAgentCapabilityRegistry:
    def test_register_adds_agent(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        assert "agent-alpha" in registry.agents
        assert "agent-beta" in registry.agents

    def test_register_overwrites_capabilities(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        registry.register("agent-alpha", ["only-this"])
        assert registry.agents["agent-alpha"] == ["only-this"]

    def test_unregister_removes_agent(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        registry.unregister("agent-beta")
        assert "agent-beta" not in registry.agents

    def test_unregister_unknown_agent_is_silent(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        registry.unregister("nonexistent")  # No exception

    def test_find_capable_exact_match(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        result = registry.find_capable(["data-analysis"])
        assert "agent-alpha" in result
        assert "agent-beta" in result

    def test_find_capable_multiple_requirements(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        result = registry.find_capable(["python", "data-analysis"])
        assert result == ["agent-alpha"]
        assert "agent-beta" not in result

    def test_find_capable_no_match_returns_empty(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        result = registry.find_capable(["quantum-computing"])
        assert result == []

    def test_find_capable_returns_all_matching(self) -> None:
        reg = AgentCapabilityRegistry()
        reg.register("weak", ["a"])
        reg.register("strong", ["a", "b"])
        result = reg.find_capable(["a"])
        # Both satisfy ["a"], both should be in result
        assert set(result) == {"weak", "strong"}

    def test_find_capable_empty_requirements_matches_all(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        result = registry.find_capable([])
        assert set(result) == {"agent-alpha", "agent-beta", "agent-gamma"}

    def test_registry_is_pydantic_model(
        self, registry: AgentCapabilityRegistry
    ) -> None:
        dumped = registry.model_dump()
        assert "agents" in dumped


# ---------------------------------------------------------------------------
# HandoffManager lifecycle tests
# ---------------------------------------------------------------------------


class TestHandoffManagerCreate:
    def test_create_returns_record(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        record = manager.create_handoff(basic_request)
        assert isinstance(record, HandoffRecord)
        assert record.status == HandoffStatus.pending

    def test_create_assigns_unique_ids(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        r1 = manager.create_handoff(basic_request)
        r2 = manager.create_handoff(basic_request)
        assert r1.record_id != r2.record_id

    def test_create_stores_request_fields(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        record = manager.create_handoff(basic_request)
        assert record.request.from_agent == "agent-alpha"
        assert record.request.to_agent == "agent-beta"
        assert record.request.priority == 5


class TestHandoffManagerAccept:
    def test_accept_transitions_to_accepted(
        self, manager: HandoffManager, pending_record: HandoffRecord
    ) -> None:
        record = manager.accept(pending_record.record_id)
        assert record.status == HandoffStatus.accepted
        assert record.response is not None
        assert record.response.accepted is True

    def test_accept_already_accepted_raises(
        self, manager: HandoffManager, accepted_record: HandoffRecord
    ) -> None:
        with pytest.raises(ValueError, match="Cannot accept"):
            manager.accept(accepted_record.record_id)

    def test_accept_unknown_id_raises(
        self, manager: HandoffManager
    ) -> None:
        with pytest.raises(KeyError):
            manager.accept("nonexistent-id")


class TestHandoffManagerStart:
    def test_start_transitions_to_in_progress(
        self, manager: HandoffManager, accepted_record: HandoffRecord
    ) -> None:
        record = manager.start(accepted_record.record_id)
        assert record.status == HandoffStatus.in_progress

    def test_start_pending_raises(
        self, manager: HandoffManager, pending_record: HandoffRecord
    ) -> None:
        with pytest.raises(ValueError, match="Cannot start"):
            manager.start(pending_record.record_id)

    def test_start_unknown_id_raises(self, manager: HandoffManager) -> None:
        with pytest.raises(KeyError):
            manager.start("no-such-id")


class TestHandoffManagerComplete:
    def test_complete_from_in_progress(
        self, manager: HandoffManager, in_progress_record: HandoffRecord
    ) -> None:
        result_data = {"output": "done", "rows_processed": 100}
        record = manager.complete(in_progress_record.record_id, result_data)
        assert record.status == HandoffStatus.completed
        assert record.result["rows_processed"] == 100

    def test_complete_from_accepted(
        self, manager: HandoffManager, accepted_record: HandoffRecord
    ) -> None:
        record = manager.complete(accepted_record.record_id, {"skipped_start": True})
        assert record.status == HandoffStatus.completed

    def test_complete_pending_raises(
        self, manager: HandoffManager, pending_record: HandoffRecord
    ) -> None:
        with pytest.raises(ValueError, match="Cannot complete"):
            manager.complete(pending_record.record_id, {})

    def test_complete_unknown_raises(self, manager: HandoffManager) -> None:
        with pytest.raises(KeyError):
            manager.complete("ghost", {})


class TestHandoffManagerReject:
    def test_reject_pending_transitions(
        self, manager: HandoffManager, pending_record: HandoffRecord
    ) -> None:
        record = manager.reject(pending_record.record_id, "Agent unavailable")
        assert record.status == HandoffStatus.rejected
        assert record.response is not None
        assert record.response.accepted is False
        assert "unavailable" in record.response.reason

    def test_reject_accepted_raises(
        self, manager: HandoffManager, accepted_record: HandoffRecord
    ) -> None:
        with pytest.raises(ValueError, match="Cannot reject"):
            manager.reject(accepted_record.record_id, "too late")

    def test_reject_unknown_raises(self, manager: HandoffManager) -> None:
        with pytest.raises(KeyError):
            manager.reject("nope", "reason")


class TestHandoffManagerFail:
    def test_fail_in_progress(
        self, manager: HandoffManager, in_progress_record: HandoffRecord
    ) -> None:
        record = manager.fail(in_progress_record.record_id, "Timeout")
        assert record.status == HandoffStatus.failed
        assert record.response is not None
        assert record.response.accepted is False

    def test_fail_accepted(
        self, manager: HandoffManager, accepted_record: HandoffRecord
    ) -> None:
        record = manager.fail(accepted_record.record_id, "Dependencies missing")
        assert record.status == HandoffStatus.failed

    def test_fail_pending_raises(
        self, manager: HandoffManager, pending_record: HandoffRecord
    ) -> None:
        with pytest.raises(ValueError, match="Cannot fail"):
            manager.fail(pending_record.record_id, "reason")


class TestHandoffManagerQuery:
    def test_get_returns_record(
        self, manager: HandoffManager, pending_record: HandoffRecord
    ) -> None:
        fetched = manager.get(pending_record.record_id)
        assert fetched.record_id == pending_record.record_id

    def test_get_unknown_raises(self, manager: HandoffManager) -> None:
        with pytest.raises(KeyError):
            manager.get("does-not-exist")

    def test_list_records_all(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        manager.create_handoff(basic_request)
        manager.create_handoff(basic_request)
        records = manager.list_records()
        assert len(records) == 2

    def test_list_records_filter_by_status(
        self,
        manager: HandoffManager,
        basic_request: HandoffRequest,
    ) -> None:
        r1 = manager.create_handoff(basic_request)
        r2 = manager.create_handoff(basic_request)
        manager.accept(r2.record_id)

        pending = manager.list_records(status=HandoffStatus.pending)
        accepted = manager.list_records(status=HandoffStatus.accepted)
        assert len(pending) == 1
        assert len(accepted) == 1

    def test_list_records_sorted_by_created_at(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        for _ in range(3):
            manager.create_handoff(basic_request)
        records = manager.list_records()
        timestamps = [r.created_at for r in records]
        assert timestamps == sorted(timestamps)

    @pytest.mark.parametrize(
        "status",
        [
            HandoffStatus.pending,
            HandoffStatus.accepted,
            HandoffStatus.in_progress,
            HandoffStatus.completed,
            HandoffStatus.rejected,
            HandoffStatus.failed,
        ],
    )
    def test_all_status_values_are_strings(self, status: HandoffStatus) -> None:
        assert isinstance(status.value, str)


class TestHandoffManagerPersistence:
    def test_export_returns_list_of_dicts(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        manager.create_handoff(basic_request)
        exported = manager.export()
        assert isinstance(exported, list)
        assert len(exported) == 1
        assert "record_id" in exported[0]

    def test_import_records_restores_state(
        self, manager: HandoffManager, basic_request: HandoffRequest
    ) -> None:
        manager.create_handoff(basic_request)
        exported = manager.export()

        new_manager = HandoffManager()
        new_manager.import_records(exported)
        records = new_manager.list_records()
        assert len(records) == 1
        assert records[0].request.from_agent == "agent-alpha"

    def test_export_import_roundtrip_preserves_status(
        self,
        manager: HandoffManager,
        pending_record: HandoffRecord,
    ) -> None:
        manager.accept(pending_record.record_id)
        exported = manager.export()

        new_manager = HandoffManager()
        new_manager.import_records(exported)
        assert new_manager.get(pending_record.record_id).status == HandoffStatus.accepted


# ---------------------------------------------------------------------------
# HandoffRouter tests
# ---------------------------------------------------------------------------


class TestHandoffRouter:
    def test_route_with_preferred_capabilities(
        self, router: HandoffRouter
    ) -> None:
        request = HandoffRequest(
            from_agent="agent-gamma",
            to_agent="",
            task_description="need python help",
        )
        agent_id = router.route(request, preferred_capabilities=["python"])
        assert agent_id == "agent-alpha"

    def test_route_falls_back_to_any_agent(
        self, router: HandoffRouter
    ) -> None:
        request = HandoffRequest(
            from_agent="agent-alpha",
            to_agent="",
            task_description="do something obscure",
        )
        # No agent has "quantum-computing"; should fall back to any other agent
        agent_id = router.route(
            request, preferred_capabilities=["quantum-computing"]
        )
        assert agent_id is not None
        assert agent_id != "agent-alpha"

    def test_route_returns_none_when_no_agents(self) -> None:
        empty_registry = AgentCapabilityRegistry()
        router = HandoffRouter(empty_registry)
        request = HandoffRequest(
            from_agent="solo",
            to_agent="",
            task_description="anything",
        )
        result = router.route(request, preferred_capabilities=["x"])
        assert result is None

    def test_route_without_preferred_uses_keyword_extraction(
        self, router: HandoffRouter
    ) -> None:
        request = HandoffRequest(
            from_agent="agent-gamma",
            to_agent="",
            task_description="perform analysis on large dataset",
        )
        # "analysis" (7 chars) and "dataset" (7 chars) extracted
        # agent-alpha and agent-beta both have data-analysis
        agent_id = router.route(request)
        assert agent_id is not None


# ---------------------------------------------------------------------------
# _extract_keywords tests
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_extracts_words_longer_than_4_chars(self) -> None:
        result = _extract_keywords("run this task today with vigor")
        assert "vigor" in result
        assert "today" in result
        # "this", "run", "with" are <=4 chars
        assert "this" not in result
        assert "run" not in result

    def test_strips_punctuation(self) -> None:
        result = _extract_keywords("please analyze data, quickly!")
        assert "analyze" in result
        assert "quickly" in result
        # Punctuation stripped
        assert "data," not in result
        assert "quickly!" not in result

    def test_lowercases_words(self) -> None:
        result = _extract_keywords("ANALYZE THIS Dataset")
        assert "analyze" in result
        assert "dataset" in result

    def test_empty_string_returns_empty(self) -> None:
        assert _extract_keywords("") == []

    def test_all_short_words(self) -> None:
        result = _extract_keywords("a an the to by")
        assert result == []


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


class TestHandoffModels:
    def test_request_priority_bounds(self) -> None:
        with pytest.raises(Exception):
            HandoffRequest(
                from_agent="a",
                to_agent="b",
                task_description="t",
                priority=0,  # below minimum of 1
            )
        with pytest.raises(Exception):
            HandoffRequest(
                from_agent="a",
                to_agent="b",
                task_description="t",
                priority=11,  # above maximum of 10
            )

    def test_request_default_priority(self) -> None:
        req = HandoffRequest(
            from_agent="a", to_agent="b", task_description="t"
        )
        assert req.priority == 5

    def test_record_default_status_is_pending(
        self, basic_request: HandoffRequest
    ) -> None:
        from aumai_handoff.models import HandoffRecord
        record = HandoffRecord(
            record_id="test-id",
            request=basic_request,
        )
        assert record.status == HandoffStatus.pending

    def test_response_default_reason_is_empty(self) -> None:
        resp = HandoffResponse(accepted=True)
        assert resp.reason == ""

    def test_record_serialization_roundtrip(
        self, pending_record: HandoffRecord
    ) -> None:
        dumped = pending_record.model_dump(mode="json")
        restored = HandoffRecord.model_validate(dumped)
        assert restored.record_id == pending_record.record_id
        assert restored.status == pending_record.status
