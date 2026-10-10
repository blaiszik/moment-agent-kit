"""The bound-project status screen, over mocked agent and human endpoints."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from typer.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli import api as api_mod  # noqa: E402
from moment_cli.cli import app  # noqa: E402


def test_status_renders_one_screen_and_stable_json_shape(tmp_path, monkeypatch):
    api_url = "http://localhost:3000"
    project_id = 42
    campaign = "first-light"
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    for name in (
        "MOMENT_API_KEY",
        "MOMENT_MCP_API_KEY",
        "MOMENT_MCP_KEY",
        "MOMENT_HUMAN_TOKEN",
        "MOMENT_API_URL",
        "MOMENT_MCP_API_URL",
        "MCP_MOMENT_API_URL",
        "MOMENT_PROJECT",
        "MOMENT_PROJECT_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    repo_config = repo / ".moment.json"
    repo_config.write_text(json.dumps({
        "apiUrl": api_url,
        "defaultProject": "detector",
        "projects": {"detector": {"id": project_id, "title": "Aurora detector"}},
    }))
    (home / ".moment.json").write_text(json.dumps({
        "apiKey": "mk_test_agent_key_long_enough_for_status",
        "humanToken": "mh_test_human",
        "humanTokenExpiresAt": "2099-01-01T00:00:00.000Z",
        "humanUser": {"id": 7, "name": "Ada"},
        "campaignContext": {f"{api_url}|7|{project_id}": campaign},
    }))

    responses = {
        f"/api/v1/agents/projects/{project_id}/context": {
            "project": {"id": project_id, "title": "Aurora detector", "status": "ACTIVE"},
            "handoff": {
                "updatedAt": "2026-09-18T14:30:00.000Z",
                "version": 7,
                "nextStep": "Calibrate the second sensor.",
            },
            "contextFreshness": {"tasksCompletedSince": 2},
        },
        "/api/v1/agents/tasks/my-claims": {
            "claims": [{"claimId": 9, "title": "Check calibration"}],
            "total": 1,
            "active": 1,
            "submitted": 0,
            "recentlyDecided": 0,
        },
        "/api/v1/agents/whats-new": {
            "promptContext": "Aurora detector moved.",
            "feed": [{
                "projectId": project_id,
                "title": "Aurora detector",
                "counts": {"pages": 1, "sessionNotes": 2},
            }],
            "metadata": {"scanned": 1},
        },
        f"/api/projects/{project_id}/submission": {
            "status": "DRAFT",
            "missing": ["SOCIAL_POST_URL"],
            "campaign": {"slug": campaign, "title": "First Light"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = responses.get(request.url.path)
        assert body is not None, f"unexpected request: {request.method} {request.url}"
        if request.url.path.endswith("/submission"):
            assert request.url.params["campaign"] == campaign
        return httpx.Response(200, json=body)

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    runner = CliRunner()
    args = ["--config", str(repo_config), "--quiet"]

    rendered = runner.invoke(app, [*args, "status"])
    assert rendered.exit_code == 0, rendered.output
    assert "Aurora detector (#42) · ACTIVE" in rendered.output
    assert "Brief: v7 · 2026-09-18 14:30 UTC · 2 tasks completed since" in rendered.output
    assert "Next: Calibrate the second sensor." in rendered.output
    assert "My claims: 1 active · 0 submitted · 0 recently decided" in rendered.output
    assert "Since you were here, across your projects: 1 project changed" in rendered.output
    assert "Aurora detector (#42): 3 changes" in rendered.output
    assert "Submission: DRAFT · 1 requirement missing" in rendered.output
    assert "Active campaign: first-light" in rendered.output

    machine = runner.invoke(app, [*args, "--json", "status"])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert set(payload) == {
        "project",
        "brief",
        "nextStep",
        "myClaims",
        "sinceYouWereHere",
        "submission",
        "activeCampaign",
        "skipped",
    }
    assert payload["project"] == {
        "id": project_id,
        "title": "Aurora detector",
        "status": "ACTIVE",
    }
    assert payload["brief"] == {
        "updatedAt": "2026-09-18T14:30:00.000Z",
        "version": 7,
        "tasksCompletedSince": 2,
    }
    assert payload["nextStep"] == "Calibrate the second sensor."
    assert payload["myClaims"]["active"] == 1
    assert payload["sinceYouWereHere"]["metadata"]["scanned"] == 1
    assert payload["submission"]["status"] == "DRAFT"
    assert payload["activeCampaign"] == campaign
    assert payload["skipped"] == []


def _log_env(tmp_path, monkeypatch, api_url="http://localhost:3000", project_id=42):
    """A bound repo + home with an agent key, ready for `moment log`."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    for name in (
        "MOMENT_API_KEY",
        "MOMENT_MCP_API_KEY",
        "MOMENT_MCP_KEY",
        "MOMENT_HUMAN_TOKEN",
        "MOMENT_API_URL",
        "MOMENT_MCP_API_URL",
        "MCP_MOMENT_API_URL",
        "MOMENT_PROJECT",
        "MOMENT_PROJECT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    repo_config = repo / ".moment.json"
    repo_config.write_text(json.dumps({
        "apiUrl": api_url,
        "defaultProject": "detector",
        "projects": {"detector": {"id": project_id, "title": "Aurora detector"}},
    }))
    (home / ".moment.json").write_text(json.dumps({
        "apiKey": "mk_test_agent_key_long_enough_for_status",
    }))
    return repo_config, project_id


def _conflict(expected, current):
    """The server's 409 body (agentError envelope + the shared extras)."""
    return {
        "success": False,
        "error": "conflict",
        "code": "version_conflict",
        "resource": "handoff",
        "expectedVersion": expected,
        "currentVersion": current,
        "current": {"version": current, "decisions": "Theirs."},
        "noteSaved": False,
    }


def test_log_retries_once_against_the_version_that_won(tmp_path, monkeypatch):
    """A lost race is re-sent ONCE with the new version, and then succeeds."""
    repo_config, project_id = _log_env(tmp_path, monkeypatch)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/agents/projects/{project_id}/session-notes"
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(409, json=_conflict(3, 5))
        return httpx.Response(201, json={
            "success": True,
            "message": "Session note logged and handoff refreshed.",
            "indexed": "failed",
        })

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    result = CliRunner().invoke(app, [
        "--config", str(repo_config), "--quiet",
        "log", "--note", "Did the thing.", "--no-tree",
        "--works-now", "Sensor reads.", "--if-version", "3",
    ])

    assert result.exit_code == 0, result.output
    assert len(seen) == 2, seen
    assert seen[0]["handoffIfVersion"] == 3
    # The SAME patch, re-aimed at the version that won the race.
    assert seen[1]["handoffIfVersion"] == 5
    assert seen[1]["handoff"] == seen[0]["handoff"]
    assert seen[1]["note"] == seen[0]["note"]
    # A dead embedding provider is reported as such, never as "skipped".
    # (Rich hard-wraps the console, so compare on collapsed whitespace.)
    flat = " ".join(result.output.split())
    assert (
        "Not searchable yet — the embedding provider refused; "
        "the next write to this project retries it." in flat
    )


def test_log_stops_after_a_second_conflict_and_prints_both_versions(tmp_path, monkeypatch):
    """Two collisions in a row: stop, name every version, write nothing."""
    repo_config, project_id = _log_env(tmp_path, monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(409, json=_conflict(3, 5 if calls["n"] == 1 else 6))

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    result = CliRunner().invoke(app, [
        "--config", str(repo_config), "--quiet",
        "log", "--note", "Did the thing.", "--no-tree",
        "--works-now", "Sensor reads.", "--if-version", "3",
    ])

    assert result.exit_code != 0
    assert calls["n"] == 2, "retry exactly once, never a third attempt"
    out = " ".join((result.output + (result.stderr if result.stderr_bytes else "")).split())
    assert "version 3" in out and "version 5" in out and "version 6" in out
    assert "Nothing was written" in out
    assert "--if-version 6" in out


def test_log_without_if_version_never_sends_the_guard(tmp_path, monkeypatch):
    """EMPTY state: no --if-version means last-write-wins, as before."""
    repo_config, project_id = _log_env(tmp_path, monkeypatch)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"success": True, "message": "Session note logged."})

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    result = CliRunner().invoke(app, [
        "--config", str(repo_config), "--quiet",
        "log", "--note", "Did the thing.", "--no-tree", "--works-now", "Sensor reads.",
    ])

    assert result.exit_code == 0, result.output
    assert "handoffIfVersion" not in seen[0]
