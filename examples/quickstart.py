"""aumai-handoff quickstart examples.

Run this file directly to verify your installation and see aumai-handoff in action:

    python examples/quickstart.py

Each demo function illustrates a different aspect of the library.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from aumai_handoff.core import AgentCapabilityRegistry, HandoffManager, HandoffRouter
from aumai_handoff.models import HandoffRecord, HandoffRequest, HandoffStatus


# ---------------------------------------------------------------------------
# Demo 1 — Full happy-path lifecycle
# ---------------------------------------------------------------------------


def demo_happy_path() -> None:
    """Walk a handoff through every non-terminal state to completed."""
    print("=" * 60)
    print("Demo 1: Full happy-path lifecycle")
    print("=" * 60)

    manager = HandoffManager()

    request = HandoffRequest(
        from_agent="agent-planner",
        to_agent="agent-executor",
        task_description="Generate the Q3 financial summary report.",
        priority=8,
        context={
            "quarter": "Q3",
            "output_format": "PDF",
        },
    )

    # Create
    record = manager.create_handoff(request)
    _print_record(record)

    # Accept
    record = manager.accept(record.record_id)
    _print_record(record)

    # Start work
    record = manager.start(record.record_id)
    _print_record(record)

    # Complete with result payload
    record = manager.complete(
        record.record_id,
        result={"output_path": "s3://reports/q3-summary.pdf", "page_count": 12},
    )
    _print_record(record)
    print()


# ---------------------------------------------------------------------------
# Demo 2 — Rejection path
# ---------------------------------------------------------------------------


def demo_rejection() -> None:
    """Create a handoff and have the receiving agent reject it."""
    print("=" * 60)
    print("Demo 2: Rejection path")
    print("=" * 60)

    manager = HandoffManager()

    request = HandoffRequest(
        from_agent="coordinator",
        to_agent="analyst",
        task_description="Translate legal contract to Mandarin Chinese.",
        priority=6,
    )

    record = manager.create_handoff(request)
    print(f"Created  : {record.record_id[:8]}  status={record.status.value}")

    # Analyst does not have translation capabilities
    record = manager.reject(
        record.record_id,
        reason="No Mandarin translation capability available on this agent.",
    )
    print(f"Rejected : {record.record_id[:8]}  status={record.status.value}")
    print(f"Reason   : {record.response.reason}")

    # Attempting to accept a rejected record raises ValueError
    try:
        manager.accept(record.record_id)
    except ValueError as exc:
        print(f"Expected error on re-accept: {exc}")
    print()


# ---------------------------------------------------------------------------
# Demo 3 — Failure path
# ---------------------------------------------------------------------------


def demo_failure() -> None:
    """Accept a handoff, start it, then mark it failed."""
    print("=" * 60)
    print("Demo 3: Failure path")
    print("=" * 60)

    manager = HandoffManager()

    request = HandoffRequest(
        from_agent="coordinator",
        to_agent="data-fetcher",
        task_description="Fetch live data from external partner API.",
        priority=7,
        context={"endpoint": "https://partner.example.com/api/v2/data"},
    )

    record = manager.create_handoff(request)
    manager.accept(record.record_id)
    manager.start(record.record_id)

    # External API is unavailable
    record = manager.fail(
        record.record_id,
        reason="Partner API returned HTTP 503 after 3 retry attempts.",
    )
    print(f"Record  : {record.record_id[:8]}")
    print(f"Status  : {record.status.value}")
    print(f"Reason  : {record.response.reason}")
    print()


# ---------------------------------------------------------------------------
# Demo 4 — Capability-based routing
# ---------------------------------------------------------------------------


def demo_capability_routing() -> None:
    """Use HandoffRouter to select the best agent for a task automatically."""
    print("=" * 60)
    print("Demo 4: Capability-based routing")
    print("=" * 60)

    registry = AgentCapabilityRegistry()
    registry.register("agent-alice", ["analyze", "summarize", "translate"])
    registry.register("agent-bob",   ["execute", "report", "monitor"])
    registry.register("agent-carol", ["analyze", "visualize", "report"])

    router = HandoffRouter(registry)
    manager = HandoffManager()

    tasks = [
        ("Analyze and summarize the quarterly sales data.", None),
        ("Please visualize and report on system performance.", None),
        ("Execute the batch job and monitor for errors.", None),
        ("Translate the user manual to French.", ["translate"]),
    ]

    for description, explicit_caps in tasks:
        request = HandoffRequest(
            from_agent="coordinator",
            to_agent="",          # will be filled by router
            task_description=description,
            priority=5,
        )
        best_agent = router.route(request, preferred_capabilities=explicit_caps)
        print(f"Task   : {description[:55]}")
        print(f"Routed : {best_agent}")
        print()


# ---------------------------------------------------------------------------
# Demo 5 — Export and import (persistence round-trip)
# ---------------------------------------------------------------------------


def demo_persistence() -> None:
    """Serialize all records to JSON and restore them in a new manager."""
    print("=" * 60)
    print("Demo 5: Export and import round-trip")
    print("=" * 60)

    manager = HandoffManager()

    # Create a few records in different states
    r1 = manager.create_handoff(HandoffRequest(
        from_agent="a", to_agent="b",
        task_description="Task one — will be completed.",
        priority=5,
    ))
    manager.accept(r1.record_id)
    manager.complete(r1.record_id, result={"done": True})

    r2 = manager.create_handoff(HandoffRequest(
        from_agent="a", to_agent="c",
        task_description="Task two — will be rejected.",
        priority=3,
    ))
    manager.reject(r2.record_id, reason="Capacity exceeded.")

    r3 = manager.create_handoff(HandoffRequest(
        from_agent="b", to_agent="c",
        task_description="Task three — still pending.",
        priority=8,
    ))

    print(f"Original manager: {len(manager.list_records())} records")

    # Serialize to JSON string
    serialized = json.dumps(manager.export(), indent=2)

    # Restore in a fresh manager
    new_manager = HandoffManager()
    new_manager.import_records(json.loads(serialized))

    restored = new_manager.list_records()
    print(f"Restored manager: {len(restored)} records")

    for record in restored:
        print(f"  {record.record_id[:8]}  {record.status.value:<12}  "
              f"{record.request.task_description[:40]}")
    print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_record(record: HandoffRecord) -> None:
    print(f"  [{record.status.value:<12}] id={record.record_id[:8]}  "
          f"priority={record.request.priority}  "
          f"updated={record.updated_at.strftime('%H:%M:%S')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run all quickstart demos in sequence."""
    demo_happy_path()
    demo_rejection()
    demo_failure()
    demo_capability_routing()
    demo_persistence()
    print("All demos completed successfully.")


if __name__ == "__main__":
    main()
