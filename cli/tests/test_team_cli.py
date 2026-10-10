"""Team commands against the same mocked HTTP transport as campaign commands."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli import api as api_mod  # noqa: E402
from moment_cli.cli import app  # noqa: E402

API_URL = "http://localhost:3000"


class Recorder:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.method} {request.url.path}"
        answer = self.routes.get(key)
        if answer is None:
            raise AssertionError(f"unexpected request: {key}")
        status, body = answer
        return httpx.Response(status, json=body)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    for name in ("MOMENT_API_KEY", "MOMENT_MCP_API_KEY", "MOMENT_MCP_KEY",
                 "MOMENT_HUMAN_TOKEN", "MOMENT_API_URL", "MOMENT_MCP_API_URL",
                 "MCP_MOMENT_API_URL", "MOMENT_PROJECT", "MOMENT_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    config = work / ".moment.json"
    config.write_text(json.dumps({"apiUrl": API_URL}))
    (home / ".moment.json").write_text(json.dumps({
        "humanToken": "mh_team_test",
        "humanTokenExpiresAt": "2099-01-01T00:00:00.000Z",
        "humanUser": {"id": 7},
    }))
    return config, home


def run(recorder, monkeypatch, config: Path, *args):
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", recorder.transport())
    return CliRunner().invoke(app, ["--config", str(config), "--quiet", *args])


def test_list_formats_teams_and_filters(setup, monkeypatch):
    config, _home = setup
    recorder = Recorder({"GET /api/teams": (200, {"teams": [
        {"name": "Aurora", "slug": "aurora", "memberCount": 3,
         "joinPolicy": "OPEN"},
        {"name": "Night Sky", "slug": "night-sky", "memberCount": 1,
         "joinPolicy": "INVITE_ONLY"},
    ]})})
    result = run(recorder, monkeypatch, config, "team", "list", "--campaign", "first-light", "--q", "sky", "--open")
    assert result.exit_code == 0, result.output
    assert "Aurora · aurora · 3 members · open to join" in result.output
    assert "Night Sky · night-sky · 1 member\n" in result.output
    assert dict(recorder.calls[0].url.params) == {"campaign": "first-light", "q": "sky", "open": "true"}
    assert recorder.calls[0].headers["authorization"] == "Bearer mh_team_test"


@pytest.mark.parametrize("target,route,body", [
    ("https://example.org/teams/join/" + "A" * 43, "/api/teams/join/" + "A" * 43, {}),
    ("A" * 43, "/api/teams/join/" + "A" * 43, {}),
    ("aurora", "/api/teams/aurora/join-requests", {"note": "I can help"}),
])
def test_join_routes(setup, monkeypatch, target, route, body):
    config, _home = setup
    recorder = Recorder({f"POST {route}": (202, {"ok": True, "joined": False})})
    args = ("team", "join", target) + (("--message", "I can help") if target == "aurora" else ())
    result = run(recorder, monkeypatch, config, *args)
    assert result.exit_code == 0, result.output
    assert recorder.calls[0].url.path == route
    assert json.loads(recorder.calls[0].content) == body


def test_create_sends_idempotency_key(setup, monkeypatch):
    config, _home = setup
    recorder = Recorder({"POST /api/campaigns/first-light/teams": (201, {
        "team": {"slug": "aurora"}, "project": {"id": 42}, "replayed": False,
    })})
    result = run(recorder, monkeypatch, config, "team", "create", "--campaign", "first-light", "--name", "Aurora")
    assert result.exit_code == 0, result.output
    body = json.loads(recorder.calls[0].content)
    assert body["name"] == "Aurora"
    assert UUID(body["createKey"]).version == 4
    assert "http://localhost:3000/teams/aurora" in result.output
    assert "moment team invite-link aurora" in result.output


def test_show_names_open_roles_and_entry_project(setup, monkeypatch):
    config, _home = setup
    recorder = Recorder({"GET /api/teams/aurora": (200, {
        "team": {"name": "Aurora", "headline": "Watch the sky", "memberCount": 3, "eventProjectId": 42},
        "roles": [{"title": "Data analyst", "isOpen": True}, {"title": "Lead", "isOpen": False}],
    })})
    result = run(recorder, monkeypatch, config, "team", "show", "aurora")
    assert result.exit_code == 0, result.output
    assert "Headline: Watch the sky" in result.output
    assert "Members: 3" in result.output
    assert "Open roles: Data analyst" in result.output
    assert "Entry project: 42" in result.output


def test_rename_uses_patch_and_prints_both_names(setup, monkeypatch):
    config, _home = setup
    recorder = Recorder({
        "GET /api/teams/aurora": (200, {"team": {"name": "Aurora"}}),
        "PATCH /api/teams/aurora": (200, {"team": {"name": "Aurora Lab"}}),
    })
    result = run(recorder, monkeypatch, config, "team", "rename", "aurora", "Aurora Lab")
    assert result.exit_code == 0, result.output
    assert "Aurora → Aurora Lab" in result.output
    assert json.loads(recorder.calls[1].content) == {"name": "Aurora Lab"}


def test_agent_key_alone_cannot_run_team_commands(setup, monkeypatch):
    config, home = setup
    (home / ".moment.json").write_text(json.dumps({"apiKey": "agent_test"}))
    recorder = Recorder({})
    result = run(recorder, monkeypatch, config, "team", "list", "--campaign", "first-light")
    assert result.exit_code == 2
    assert "Team commands act as you. Sign in with: moment auth login" in result.output
    assert recorder.calls == []


def test_delete_prints_server_refusal(setup, monkeypatch):
    config, _home = setup
    recorder = Recorder({"DELETE /api/teams/aurora": (409, {
        "code": "TEAM_HAS_OTHERS", "error": "Others are on this team. Leave it instead.",
        "message": "Others are on this team. Leave it instead.",
    })})
    result = run(recorder, monkeypatch, config, "team", "delete", "aurora")
    assert result.exit_code == 1
    assert "Others are on this team. Leave it instead." in result.output


def test_json_prints_raw_server_payload(setup, monkeypatch):
    config, _home = setup
    payload = {"link": {"url": "https://example.org/teams/join/token"}}
    recorder = Recorder({"POST /api/teams/aurora/invite-links": (201, payload)})
    result = run(recorder, monkeypatch, config, "--json", "team", "invite-link", "aurora")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
    assert json.loads(recorder.calls[0].content) == {
        "admitOnOpen": True, "autoAccept": True, "expiresInDays": 30, "maxUses": None,
    }


def test_invite_link_stdout_is_only_the_url(setup, monkeypatch):
    config, _home = setup
    url = "https://example.org/teams/join/token"
    recorder = Recorder({"POST /api/teams/aurora/invite-links": (201, {"link": {"url": url}})})
    result = run(recorder, monkeypatch, config, "team", "invite-link", "aurora")
    assert result.exit_code == 0, result.output
    assert result.stdout == url + "\n"
    assert "Share this link" in result.stderr
