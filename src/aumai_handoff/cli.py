"""CLI entry point for aumai-handoff."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .core import HandoffManager
from .models import HandoffRequest, HandoffStatus


@click.group()
@click.version_option()
def main() -> None:
    """AumAI Handoff — standardized agent-to-agent task handoff protocol."""


@main.command("create")
@click.option("--from", "from_agent", required=True, help="Sending agent ID.")
@click.option("--to", "to_agent", required=True, help="Receiving agent ID.")
@click.option("--task", required=True, help="Task description.")
@click.option("--priority", default=5, show_default=True, type=int, help="Priority 1–10.")
@click.option(
    "--context",
    "context_json",
    default=None,
    help="Optional JSON string of context key-value pairs.",
)
@click.option(
    "--store",
    "store_path",
    default="handoffs.json",
    show_default=True,
    help="Path to the handoff store JSON file.",
)
def create_command(
    from_agent: str,
    to_agent: str,
    task: str,
    priority: int,
    context_json: str | None,
    store_path: str,
) -> None:
    """Create a new handoff from one agent to another."""
    context: dict[str, object] = {}
    if context_json:
        try:
            context = json.loads(context_json)
        except json.JSONDecodeError as exc:
            click.echo(f"Error: invalid JSON for --context: {exc}", err=True)
            sys.exit(1)

    manager = HandoffManager()
    _load_store(manager, store_path)

    request = HandoffRequest(
        from_agent=from_agent,
        to_agent=to_agent,
        task_description=task,
        context=context,
        priority=priority,
    )
    record = manager.create_handoff(request)
    _save_store(manager, store_path)

    click.echo(f"Created handoff {record.record_id}")
    click.echo(f"  From   : {from_agent}")
    click.echo(f"  To     : {to_agent}")
    click.echo(f"  Task   : {task}")
    click.echo(f"  Status : {record.status.value}")


@main.command("status")
@click.option("--id", "record_id", required=True, help="Handoff record ID.")
@click.option(
    "--store",
    "store_path",
    default="handoffs.json",
    show_default=True,
    help="Path to the handoff store JSON file.",
)
def status_command(record_id: str, store_path: str) -> None:
    """Show the status of a handoff record."""
    manager = HandoffManager()
    _load_store(manager, store_path)

    try:
        record = manager.get(record_id)
    except KeyError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Handoff ID : {record.record_id}")
    click.echo(f"Status     : {record.status.value}")
    click.echo(f"From       : {record.request.from_agent}")
    click.echo(f"To         : {record.request.to_agent}")
    click.echo(f"Task       : {record.request.task_description}")
    click.echo(f"Priority   : {record.request.priority}")
    click.echo(f"Created    : {record.created_at.isoformat()}")
    click.echo(f"Updated    : {record.updated_at.isoformat()}")
    if record.response:
        click.echo(f"Response   : accepted={record.response.accepted}, reason={record.response.reason!r}")
    if record.result:
        click.echo(f"Result     : {json.dumps(record.result)}")


@main.command("accept")
@click.option("--id", "record_id", required=True, help="Handoff record ID.")
@click.option("--store", "store_path", default="handoffs.json", show_default=True)
def accept_command(record_id: str, store_path: str) -> None:
    """Accept a pending handoff."""
    manager = HandoffManager()
    _load_store(manager, store_path)
    try:
        record = manager.accept(record_id)
    except (KeyError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    _save_store(manager, store_path)
    click.echo(f"Handoff {record.record_id} accepted.")


@main.command("complete")
@click.option("--id", "record_id", required=True, help="Handoff record ID.")
@click.option("--result", "result_json", default="{}", help="JSON result payload.")
@click.option("--store", "store_path", default="handoffs.json", show_default=True)
def complete_command(
    record_id: str, result_json: str, store_path: str
) -> None:
    """Mark a handoff as completed."""
    manager = HandoffManager()
    _load_store(manager, store_path)
    try:
        result = json.loads(result_json)
        record = manager.complete(record_id, result)
    except json.JSONDecodeError as exc:
        click.echo(f"Error: invalid JSON for --result: {exc}", err=True)
        sys.exit(1)
    except (KeyError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    _save_store(manager, store_path)
    click.echo(f"Handoff {record.record_id} completed.")


@main.command("list")
@click.option(
    "--status",
    "filter_status",
    default=None,
    type=click.Choice([s.value for s in HandoffStatus]),
    help="Filter by status.",
)
@click.option("--store", "store_path", default="handoffs.json", show_default=True)
def list_command(filter_status: str | None, store_path: str) -> None:
    """List all handoff records."""
    manager = HandoffManager()
    _load_store(manager, store_path)
    status_filter = HandoffStatus(filter_status) if filter_status else None
    records = manager.list_records(status=status_filter)
    if not records:
        click.echo("No handoff records found.")
        return
    for record in records:
        click.echo(
            f"{record.record_id[:8]}  {record.status.value:<12}  "
            f"{record.request.from_agent} -> {record.request.to_agent}  "
            f"{record.request.task_description[:40]}"
        )


def _load_store(manager: HandoffManager, path: str) -> None:
    """Load persisted handoff records from *path* if it exists."""
    p = Path(path)
    if p.exists():
        data = json.loads(p.read_text())
        manager.import_records(data)


def _save_store(manager: HandoffManager, path: str) -> None:
    """Persist handoff records to *path*."""
    Path(path).write_text(json.dumps(manager.export(), indent=2))


if __name__ == "__main__":
    main()
