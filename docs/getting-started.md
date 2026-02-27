# Getting Started with aumai-handoff

This guide takes you from a fresh install to running your first handoff chain in under five
minutes, then covers the most common real-world patterns.

---

## Prerequisites

- Python 3.11 or later
- `pip` (any recent version)

No external services, databases, or API keys are required.
`aumai-handoff` runs entirely in-process, with optional JSON file persistence.

---

## Installation

### From PyPI (recommended)

```bash
pip install aumai-handoff
```

### From source

```bash
git clone https://github.com/aumai/aumai-handoff
cd aumai-handoff
pip install -e ".[dev]"
```

### Verify the installation

```bash
aumai-handoff --version
# aumai-handoff, version 0.1.0

python -c "from aumai_handoff.core import HandoffManager; print('OK')"
# OK
```

---

## Step-by-Step Tutorial

### Step 1 — Create a HandoffManager

The `HandoffManager` is the central object. It holds all records in memory and provides
methods for every lifecycle transition.

```python
from aumai_handoff.core import HandoffManager

manager = HandoffManager()
```

### Step 2 — Build a HandoffRequest

A `HandoffRequest` specifies who is handing off, to whom, what the task is, and how urgent
it is.

```python
from aumai_handoff.models import HandoffRequest

request = HandoffRequest(
    from_agent="agent-planner",
    to_agent="agent-executor",
    task_description="Generate the Q3 financial summary report.",
    priority=8,              # 1 (low) to 10 (critical)
    context={
        "quarter": "Q3",
        "output_format": "PDF",
        "due_by": "2026-03-31",
    },
)
```

### Step 3 — Create the handoff record

```python
record = manager.create_handoff(request)

print(f"Record ID : {record.record_id}")
print(f"Status    : {record.status.value}")    # "pending"
print(f"Created   : {record.created_at}")
```

### Step 4 — Accept the handoff

The receiving agent signals it will handle the task.

```python
record = manager.accept(record.record_id)
print(f"Status: {record.status.value}")  # "accepted"
print(f"Response: {record.response.reason}")  # "Accepted by receiving agent."
```

### Step 5 — Start work

```python
record = manager.start(record.record_id)
print(f"Status: {record.status.value}")  # "in_progress"
```

### Step 6 — Complete with a result

```python
record = manager.complete(
    record.record_id,
    result={"output_path": "s3://reports/q3-summary.pdf", "page_count": 12},
)
print(f"Status : {record.status.value}")  # "completed"
print(f"Result : {record.result}")
```

### Step 7 — Query records

```python
from aumai_handoff.models import HandoffStatus

all_records = manager.list_records()
completed = manager.list_records(status=HandoffStatus.completed)

print(f"Total records    : {len(all_records)}")
print(f"Completed records: {len(completed)}")
```

### Step 8 — Use the CLI

```bash
# Create a handoff
aumai-handoff create \
  --from agent-planner \
  --to agent-executor \
  --task "Generate Q3 report" \
  --priority 8 \
  --context '{"quarter": "Q3"}'

# List pending handoffs
aumai-handoff list --status pending

# Accept the handoff (use the ID printed by create)
aumai-handoff accept --id <record-id>

# Complete it
aumai-handoff complete --id <record-id> --result '{"output": "report.pdf"}'

# Verify
aumai-handoff status --id <record-id>
```

By default the CLI reads and writes a `handoffs.json` file in the current directory.
Use `--store /path/to/custom.json` to specify a different location.

---

## Common Patterns and Recipes

### Pattern 1 — Reject a handoff with a reason

When an agent cannot accept a task, it calls `reject()` with a human-readable reason.
This is the correct way to signal "I cannot do this" rather than just ignoring the request.

```python
request = HandoffRequest(
    from_agent="coordinator",
    to_agent="analyst",
    task_description="Translate legal document to Mandarin.",
    priority=6,
)
record = manager.create_handoff(request)

# analyst doesn't have translation capabilities
manager.reject(record.record_id, reason="No Mandarin translation capability.")

rejected = manager.get(record.record_id)
print(rejected.status.value)        # "rejected"
print(rejected.response.reason)     # "No Mandarin translation capability."
```

### Pattern 2 — Mark a handoff as failed

If work starts but cannot be completed, use `fail()`.

```python
record = manager.create_handoff(HandoffRequest(
    from_agent="a", to_agent="b",
    task_description="Fetch data from external API.",
    priority=5,
))
manager.accept(record.record_id)
manager.start(record.record_id)

# External API is down
manager.fail(record.record_id, reason="External API returned 503 after 3 retries.")

failed = manager.get(record.record_id)
print(failed.status.value)  # "failed"
```

### Pattern 3 — Capability-based routing

Instead of hardcoding `to_agent`, use `HandoffRouter` to find the best available agent.

```python
from aumai_handoff.core import AgentCapabilityRegistry, HandoffRouter
from aumai_handoff.models import HandoffRequest

registry = AgentCapabilityRegistry()
registry.register("agent-alice", ["analyze", "summarize", "translate"])
registry.register("agent-bob",   ["execute", "report"])
registry.register("agent-carol", ["analyze", "visualize", "report"])

router = HandoffRouter(registry)

request = HandoffRequest(
    from_agent="coordinator",
    to_agent="",            # will be filled by the router
    task_description="Analyze customer sentiment and produce visualizations.",
    priority=7,
)

# Router extracts keywords from task_description and matches capabilities
best_agent = router.route(request)
print(f"Routing to: {best_agent}")  # "agent-carol" (has analyze + visualize)

# Now create the actual record
if best_agent:
    request.to_agent = best_agent
    record = manager.create_handoff(request)
```

### Pattern 4 — Export and restore records across process restarts

The `HandoffManager` is in-memory only. Use `export()` and `import_records()` to persist
state to disk.

```python
import json
from pathlib import Path

# Save
snapshot = manager.export()
Path("handoffs.json").write_text(json.dumps(snapshot, indent=2))

# Restore in a new process or after restart
new_manager = HandoffManager()
data = json.loads(Path("handoffs.json").read_text())
new_manager.import_records(data)

restored = new_manager.list_records()
print(f"Restored {len(restored)} records.")
```

### Pattern 5 — Validate state before transitioning

Always check the current status before calling lifecycle methods if your code may receive
records in any state.

```python
from aumai_handoff.models import HandoffStatus

record = manager.get(record_id)

if record.status == HandoffStatus.pending:
    manager.accept(record.record_id)
elif record.status == HandoffStatus.accepted:
    manager.start(record.record_id)
elif record.status in (HandoffStatus.completed,
                       HandoffStatus.rejected,
                       HandoffStatus.failed):
    print(f"Handoff {record.record_id} is already terminal: {record.status.value}")
```

---

## Troubleshooting FAQ

**Q: `ValueError: Cannot accept handoff in state 'accepted'.`**

A: You attempted a transition from a state that does not allow it. Refer to the lifecycle
diagram in the README. `accept()` can only be called when the record is in `pending` state.

---

**Q: `KeyError: No handoff record found with id '...'`**

A: The record ID does not exist in this `HandoffManager` instance. This usually means
you created a new `HandoffManager` without restoring records from the JSON store. Call
`import_records()` with the previously exported data before querying.

---

**Q: The CLI creates a new `handoffs.json` every run instead of appending.**

A: The CLI loads the existing file before making changes and saves after. Make sure you
are passing the same `--store` path on every command (default is `handoffs.json` in the
current working directory). Running the command from different directories creates separate
stores.

---

**Q: `route()` returns `None` even though agents are registered.**

A: This happens when no registered agent has capabilities matching the extracted keywords
from the task description, AND all other agents have the same `from_agent` ID as the
request. Check that your agent IDs are different from `request.from_agent` and that
their capabilities contain words present in `task_description`. You can also pass
`preferred_capabilities` explicitly to `route()` instead of relying on keyword extraction.

---

**Q: The `priority` field is being rejected with a validation error.**

A: Priority must be an integer between 1 and 10 inclusive. Values outside this range are
rejected by Pydantic's `ge=1, le=10` constraints.

---

**Q: Can I have two `HandoffManager` instances share the same JSON store?**

A: Not safely in concurrent access scenarios. For concurrent use, add file-level locking
or use a proper database. For sequential use (one process at a time), the export/import
pattern works correctly.

---

## Next Steps

- Read the [API Reference](api-reference.md) for complete class and method documentation.
- Explore the [examples/](../examples/) directory for runnable demos.
- See [CONTRIBUTING.md](../CONTRIBUTING.md) to contribute tests or features.
