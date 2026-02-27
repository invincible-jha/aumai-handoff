"""Tests for aumai-handoff CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aumai_handoff.cli import main
from aumai_handoff.models import HandoffStatus


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def store(tmp_path: Path) -> str:
    """Return a path to a temporary store file (does not exist yet)."""
    return str(tmp_path / "handoffs.json")


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def test_cli_version(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


# ---------------------------------------------------------------------------
# create command
# ---------------------------------------------------------------------------


def test_create_command_basic(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main,
        [
            "create",
            "--from", "agent-a",
            "--to", "agent-b",
            "--task", "Process the dataset",
            "--store", store,
        ],
    )
    assert result.exit_code == 0
    assert "Created handoff" in result.output
    assert "agent-a" in result.output
    assert "agent-b" in result.output
    assert "Process the dataset" in result.output
    assert "pending" in result.output


def test_create_command_persists_to_store(
    runner: CliRunner, store: str
) -> None:
    runner.invoke(
        main,
        [
            "create",
            "--from", "a",
            "--to", "b",
            "--task", "Do something",
            "--store", store,
        ],
    )
    data = json.loads(Path(store).read_text())
    assert len(data) == 1
    assert data[0]["request"]["from_agent"] == "a"


def test_create_command_with_context(runner: CliRunner, store: str) -> None:
    ctx = json.dumps({"env": "production", "batch_size": 500})
    result = runner.invoke(
        main,
        [
            "create",
            "--from", "a",
            "--to", "b",
            "--task", "Deploy",
            "--context", ctx,
            "--store", store,
        ],
    )
    assert result.exit_code == 0


def test_create_command_invalid_context_json(
    runner: CliRunner, store: str
) -> None:
    result = runner.invoke(
        main,
        [
            "create",
            "--from", "a",
            "--to", "b",
            "--task", "t",
            "--context", "{bad json",
            "--store", store,
        ],
    )
    assert result.exit_code == 1
    assert "Error" in result.output


def test_create_command_with_priority(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main,
        [
            "create",
            "--from", "a",
            "--to", "b",
            "--task", "urgent",
            "--priority", "9",
            "--store", store,
        ],
    )
    assert result.exit_code == 0


def test_create_requires_from(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main,
        ["create", "--to", "b", "--task", "t", "--store", store],
    )
    assert result.exit_code != 0


def test_create_requires_to(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main,
        ["create", "--from", "a", "--task", "t", "--store", store],
    )
    assert result.exit_code != 0


def test_create_requires_task(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main,
        ["create", "--from", "a", "--to", "b", "--store", store],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


def _create_record(runner: CliRunner, store: str) -> str:
    """Helper: create a handoff and return its ID."""
    result = runner.invoke(
        main,
        [
            "create",
            "--from", "x",
            "--to", "y",
            "--task", "Do work",
            "--store", store,
        ],
    )
    # Extract "Created handoff <id>" line
    for line in result.output.splitlines():
        if line.startswith("Created handoff "):
            return line.split()[-1]
    raise RuntimeError("Could not extract record ID")


def test_status_command(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    result = runner.invoke(
        main, ["status", "--id", record_id, "--store", store]
    )
    assert result.exit_code == 0
    assert record_id in result.output
    assert "pending" in result.output
    assert "From" in result.output
    assert "To" in result.output


def test_status_unknown_id(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main, ["status", "--id", "ghost-id", "--store", store]
    )
    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# accept command
# ---------------------------------------------------------------------------


def test_accept_command(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    result = runner.invoke(
        main, ["accept", "--id", record_id, "--store", store]
    )
    assert result.exit_code == 0
    assert "accepted" in result.output


def test_accept_unknown_id(runner: CliRunner, store: str) -> None:
    result = runner.invoke(
        main, ["accept", "--id", "no-such", "--store", store]
    )
    assert result.exit_code == 1


def test_accept_already_accepted_fails(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    runner.invoke(main, ["accept", "--id", record_id, "--store", store])
    # Accept again — should fail (wrong state)
    result = runner.invoke(
        main, ["accept", "--id", record_id, "--store", store]
    )
    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# complete command
# ---------------------------------------------------------------------------


def test_complete_command(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    runner.invoke(main, ["accept", "--id", record_id, "--store", store])
    result = runner.invoke(
        main,
        [
            "complete",
            "--id", record_id,
            "--result", '{"rows": 42}',
            "--store", store,
        ],
    )
    assert result.exit_code == 0
    assert "completed" in result.output


def test_complete_invalid_result_json(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    runner.invoke(main, ["accept", "--id", record_id, "--store", store])
    result = runner.invoke(
        main,
        [
            "complete",
            "--id", record_id,
            "--result", "not-json",
            "--store", store,
        ],
    )
    assert result.exit_code == 1
    assert "Error" in result.output


def test_complete_pending_record_fails(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    result = runner.invoke(
        main,
        ["complete", "--id", record_id, "--result", "{}", "--store", store],
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------


def test_list_command_empty_store(runner: CliRunner, store: str) -> None:
    result = runner.invoke(main, ["list", "--store", store])
    assert result.exit_code == 0
    assert "No handoff records found" in result.output


def test_list_command_shows_records(runner: CliRunner, store: str) -> None:
    _create_record(runner, store)
    _create_record(runner, store)
    result = runner.invoke(main, ["list", "--store", store])
    assert result.exit_code == 0
    assert "pending" in result.output


@pytest.mark.parametrize(
    "status",
    ["pending", "accepted", "in_progress", "completed", "rejected", "failed"],
)
def test_list_filter_by_status_accepted_values(
    runner: CliRunner, store: str, status: str
) -> None:
    result = runner.invoke(
        main, ["list", "--status", status, "--store", store]
    )
    assert result.exit_code == 0


def test_list_filter_excludes_wrong_status(runner: CliRunner, store: str) -> None:
    record_id = _create_record(runner, store)
    runner.invoke(main, ["accept", "--id", record_id, "--store", store])
    # Listing pending should show nothing (record is now accepted)
    result = runner.invoke(
        main, ["list", "--status", "pending", "--store", store]
    )
    assert result.exit_code == 0
    assert "No handoff records found" in result.output


# ---------------------------------------------------------------------------
# help text
# ---------------------------------------------------------------------------


def test_help_text(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "create" in result.output
    assert "status" in result.output
    assert "accept" in result.output
    assert "complete" in result.output
    assert "list" in result.output
