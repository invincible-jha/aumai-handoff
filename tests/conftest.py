"""Shared test fixtures for aumai-handoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aumai_handoff.core import AgentCapabilityRegistry, HandoffManager, HandoffRouter
from aumai_handoff.models import HandoffRecord, HandoffRequest, HandoffStatus


@pytest.fixture()
def manager() -> HandoffManager:
    return HandoffManager()


@pytest.fixture()
def registry() -> AgentCapabilityRegistry:
    reg = AgentCapabilityRegistry()
    reg.register("agent-alpha", ["python", "data-analysis", "reporting"])
    reg.register("agent-beta", ["java", "data-analysis"])
    reg.register("agent-gamma", ["reporting", "visualization"])
    return reg


@pytest.fixture()
def router(registry: AgentCapabilityRegistry) -> HandoffRouter:
    return HandoffRouter(registry)


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
def pending_record(
    manager: HandoffManager, basic_request: HandoffRequest
) -> HandoffRecord:
    return manager.create_handoff(basic_request)


@pytest.fixture()
def accepted_record(
    manager: HandoffManager, pending_record: HandoffRecord
) -> HandoffRecord:
    return manager.accept(pending_record.record_id)


@pytest.fixture()
def in_progress_record(
    manager: HandoffManager, accepted_record: HandoffRecord
) -> HandoffRecord:
    return manager.start(accepted_record.record_id)


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "handoffs.json"


@pytest.fixture()
def populated_store(
    manager: HandoffManager,
    basic_request: HandoffRequest,
    store_path: Path,
) -> Path:
    """Create two handoffs and persist them to store_path."""
    manager.create_handoff(basic_request)
    req2 = HandoffRequest(
        from_agent="agent-beta",
        to_agent="agent-gamma",
        task_description="Visualize the report data.",
        priority=3,
    )
    manager.create_handoff(req2)
    store_path.write_text(json.dumps(manager.export(), indent=2))
    return store_path
