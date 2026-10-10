"""Vocabulary contract tests for the Typer tree and record/task wire shapes."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx
from typer.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli import api as api_mod  # noqa: E402
from moment_cli.cli import app  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]


def _canonical_cli_names() -> list[str]:
    document = (ROOT / "docs/agent-contracts/vocabulary.md").read_text()
    block = re.search(r"```json\s*(.*?)```", document, re.DOTALL)
    assert block is not None
    return json.loads(block.group(1))["cli"]


def _registered_cli_names(typer_app, prefix: str = "") -> set[str]:
    names: set[str] = set()
    for command in typer_app.registered_commands:
        if command.hidden is True:
            continue
        name = command.name or command.callback.__name__.replace("_", "-")
        names.add(f"{prefix}{name}")
    for group in typer_app.registered_groups:
        if group.typer_instance.info.hidden is True:
            continue
        name = group.name or group.typer_instance.info.name
        names.update(_registered_cli_names(group.typer_instance, f"{prefix}{name} "))
    return names


def test_canonical_cli_names_and_visible_typer_commands_match_bidirectionally():
    registered = _registered_cli_names(app)
    canonical = set(_canonical_cli_names())
    assert canonical - registered == set(), "vocabulary names missing from Typer"
    assert registered - canonical == set(), "visible Typer commands missing from vocabulary"


def _write_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    (home / ".moment.json").write_text(json.dumps({"apiKey": "mk_test_key_long_enough"}))
    config = repo / ".moment.json"
    config.write_text(json.dumps({"apiUrl": "http://localhost:3000", "projectId": 42}))
    return config


def test_task_list_and_hidden_work_list_share_the_endpoint(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "repo")
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"tasks": []})

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    runner = CliRunner()
    task = runner.invoke(app, ["--config", str(config), "--quiet", "task", "list"])
    work = runner.invoke(app, ["--config", str(config), "--quiet", "work", "list"])

    assert task.exit_code == 0, task.output
    assert work.exit_code == 0, work.output
    assert calls == [
        ("GET", "/api/v1/agents/tasks"),
        ("GET", "/api/v1/agents/tasks"),
    ]
    assert "moment work is now moment task" in work.stderr


def test_record_prints_event_lines_and_json_passthrough(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "repo")
    page = {
        "scope": {"kind": "project", "projectId": 42, "title": "Aurora"},
        "items": [
            {
                "id": "PAGE_EDITED:8",
                "kind": "PAGE_EDITED",
                "at": "2026-09-19T10:30:00.000Z",
                "actor": {"name": "Keeper", "isAgent": True, "operator": "Ben Blaiszik"},
                "title": "Protocol updated",
                "snippet": "The calibration note changed.",
                "href": "/projects/42/pages/protocol",
            },
            {
                "id": "SESSION_NOTE:7",
                "kind": "SESSION_NOTE",
                "at": "2026-09-19T09:15:00.000Z",
                "actor": {"name": "Ada", "isAgent": False, "operator": None},
                "title": "Session closed",
                "snippet": None,
                "href": "/projects/42",
            },
        ],
        "nextCursor": "older-cursor",
        "hasMore": True,
        "generatedAt": "2026-09-19T10:31:00.000Z",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    runner = CliRunner()
    rendered = runner.invoke(app, ["--config", str(config), "--quiet", "record", "--project", "42"])
    assert rendered.exit_code == 0, rendered.output
    assert "2026-09-19" in rendered.stdout
    assert "10:30  Keeper (agent, for Ben Blaiszik)  Protocol updated  — The calibration note changed." in rendered.stdout
    assert "09:15  Ada  Session closed" in rendered.stdout

    machine = runner.invoke(app, ["--config", str(config), "--quiet", "--json", "record", "--project", "42"])
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout) == page
