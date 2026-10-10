"""Tests for orient brief rendering, staleness rules, and API view/search options.

Covers:
- Fresh rawMarkdown renders verbatim without structured overlay
- Stale rawMarkdown renders structured fields first, separator, then rawMarkdown
- Footer displays brief stamped date when rawMarkdownUpdatedAt is present
- Absent sections (mustRead, doorTasks, nextCalls, handoff) never crash
- Must read panel formats title/slug and clamps at 3000 chars
- Doors panel formats #<id> <title> · <type> · <estimatedMinutes or '—'> min
- Next: <nextCalls> line at the end
- get_project_context appends ?view=<view> when given, omitting when None
- orient --full CLI flag passes view="full"
- find_context passes team, types, since as query params only when given
"""

from __future__ import annotations

import io
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

# Make moment_cli importable whether or not the package is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from typer.testing import CliRunner

import moment_cli.display as display
from moment_cli.api import MomentApiClient
from moment_cli.cli import app
from moment_cli.config import Config
from moment_cli.display import (
    format_structured_handoff,
    raw_markdown_is_stale,
    render_brief,
)

runner = CliRunner()


def _capture_render(payload: dict, quiet: bool = False) -> str:
    """Helper to render brief and capture console text output."""
    buf = io.StringIO()
    old_console = display.console
    display.console = Console(file=buf, force_terminal=False, width=120)
    try:
        render_brief(payload, quiet=quiet)
        return buf.getvalue()
    finally:
        display.console = old_console


# --- Tests ---


def test_fresh_raw_markdown_renders_raw() -> None:
    """Fresh rawMarkdown (stamp matches updatedAt or within slack) renders verbatim."""
    payload = {
        "project": {"name": "Demo", "id": 1},
        "handoff": {
            "version": 4,
            "updatedAt": "2026-08-29T10:00:02Z",
            "rawMarkdownUpdatedAt": "2026-08-29T10:00:00Z",
            "updatedBy": "agent-smith",
            "rawMarkdown": "# Architecture\n\nAll services operational.",
            "nextStep": "This structured nextStep should NOT shadow the fresh brief",
        },
    }
    output = _capture_render(payload)

    assert "Architecture" in output
    assert "All services operational." in output
    assert "Earlier brief (superseded by the patches above)" not in output
    assert "This structured nextStep should NOT shadow the fresh brief" not in output
    assert "v4 · 2026-08-29 · agent-smith · brief stamped 2026-08-29" in output


def test_stale_raw_markdown_renders_structured_first() -> None:
    """Stale rawMarkdown renders structured fields above a separator rule, then raw."""
    payload = {
        "project": {"name": "Demo", "id": 1},
        "handoff": {
            "version": 5,
            "updatedAt": "2026-08-29T10:05:00Z",
            "rawMarkdownUpdatedAt": "2026-08-29T10:00:00Z",
            "updatedBy": "claude",
            "rawMarkdown": "# Old Brief\n\nOld roadmap text.",
            "nextStep": "Apply migration 002",
            "worksNow": "Auth login endpoint",
            "inProgress": "Session cookies",
            "blocked": "DB connection timeout",
            "watchOut": "Do not truncate .next cache",
            "readFirst": [{"label": "spec.md", "why": "authoritative requirements"}],
        },
    }
    output = _capture_render(payload)

    assert "→ Next: Apply migration 002" in output
    assert "Works now" in output
    assert "Auth login endpoint" in output
    assert "In progress" in output
    assert "Session cookies" in output
    assert "Blocked" in output
    assert "DB connection timeout" in output
    assert "Watch out" in output
    assert "Do not truncate .next cache" in output
    assert "spec.md" in output
    assert "authoritative requirements" in output
    assert "Earlier brief (superseded by the patches above)" in output
    assert "Old Brief" in output
    assert "Old roadmap text." in output

    # Structured fields appear before raw brief
    idx_next = output.index("Apply migration 002")
    idx_rule = output.index("Earlier brief (superseded by the patches above)")
    idx_raw = output.index("Old Brief")
    assert idx_next < idx_rule < idx_raw

    # Footer includes brief stamped date
    assert "v5 · 2026-08-29 · claude · brief stamped 2026-08-29" in output


def test_absent_sections_dont_crash() -> None:
    """Missing or empty optional sections (handoff, mustRead, doorTasks, etc.) never crash."""
    # Completely empty payload
    out1 = _capture_render({})
    assert "(untitled)" in out1
    assert "No handoff yet" in out1

    # Missing handoff, quiet=True
    out2 = _capture_render({"project": {"name": "P1"}}, quiet=True)
    assert "No handoff yet" in out2

    # None values for all optional S2 additions
    out3 = _capture_render({
        "project": {"name": "P2"},
        "handoff": None,
        "mustRead": None,
        "doorTasks": None,
        "nextCalls": None,
    })
    assert "No handoff yet" in out3

    # Empty lists for all optional S2 additions
    out4 = _capture_render({
        "project": {"name": "P3"},
        "handoff": {"version": 1, "rawMarkdown": "Short"},
        "mustRead": [],
        "doorTasks": [],
        "nextCalls": [],
    })
    assert "Short" in out4


def test_must_read_section_and_clamping() -> None:
    """Must read panel lists pages as '<title> (<slug>)' and clamps body at 3000 chars."""
    long_body = "x" * 3500
    payload = {
        "project": {"name": "Demo"},
        "mustRead": [
            {
                "slug": "roadmap",
                "title": "Roadmap 2026",
                "body": "Short page body",
            },
            {
                "slug": "architecture",
                "title": "Architecture Overview",
                "body": long_body,
            },
        ],
    }
    output = _capture_render(payload)

    assert "Roadmap 2026 (roadmap)" in output
    assert "Short page body" in output
    assert "Architecture Overview (architecture)" in output
    assert "… (moment page-read architecture for the rest)" in output


def test_doors_section() -> None:
    """Doors panel lists doorTasks as '#<id> <title> · <type> · <estimatedMinutes or '—'> min'."""
    payload = {
        "project": {"name": "Demo"},
        "doorTasks": [
            {
                "id": 101,
                "title": "Fix auth token expiration",
                "type": "BUG",
                "estimatedMinutes": 30,
            },
            {
                "id": 102,
                "title": "Document API contracts",
                "type": "TASK",
                "estimatedMinutes": None,
            },
        ],
    }
    output = _capture_render(payload)

    assert "Doors" in output
    assert "#101 Fix auth token expiration · BUG · 30 min" in output
    assert "#102 Document API contracts · TASK · — min" in output


def test_next_calls_section() -> None:
    """Next calls line renders dim 'Next: <nextCalls joined by ' · '>' at the very end."""
    payload = {
        "project": {"name": "Demo"},
        "metadata": {"tokenEstimate": 1200},
        "nextCalls": [
            "claim_task(101)",
            "read_page('roadmap')",
            "search_context('auth')",
        ],
    }
    output = _capture_render(payload)

    assert "Next: claim_task(101) · read_page('roadmap') · search_context('auth')" in output


def test_api_get_project_context_view_param() -> None:
    """get_project_context appends ?view=<view> when given, omits when None."""
    cfg = Config(
        api_key="dummy_key",
        api_url="http://test.local",
        config_path=None,
        mcp_path=None,
        fingerprint="test",
        project_id=1,
    )
    client = MomentApiClient(cfg)

    recorded_routes: list[str] = []

    def mock_get(route: str) -> dict:
        recorded_routes.append(route)
        return {"ok": True}

    client.get = mock_get  # type: ignore[assignment]

    # Default (view=None)
    client.get_project_context(42)
    assert recorded_routes[-1] == "/api/v1/agents/projects/42/context"

    # Explicit view=None
    client.get_project_context(42, view=None)
    assert recorded_routes[-1] == "/api/v1/agents/projects/42/context"

    # Full view
    client.get_project_context(42, view="full")
    assert recorded_routes[-1] == "/api/v1/agents/projects/42/context?view=full"

    # Brief view
    client.get_project_context(42, view="brief")
    assert recorded_routes[-1] == "/api/v1/agents/projects/42/context?view=brief"


def test_orient_cli_full_flag() -> None:
    """orient --full passes view='full'. Piped with no view, it asks for the lean
    spinup view; --json keeps the server default (view=None)."""
    mock_client = MagicMock()
    mock_client.get_project_context.return_value = {
        "project": {"name": "Test"},
        "metadata": {},
    }

    with patch("moment_cli.cli._get_client", return_value=mock_client), \
         patch("moment_cli.cli._resolve_project_id", return_value=99):

        # Piped (CliRunner is not a terminal) -> the lean view
        res = runner.invoke(app, ["orient"])
        assert res.exit_code == 0
        mock_client.get_project_context.assert_called_with(99, view="spinup")

        res_json = runner.invoke(app, ["--json", "orient"])
        assert res_json.exit_code == 0
        mock_client.get_project_context.assert_called_with(99, view=None)

        # orient --full -> view="full"
        res_full = runner.invoke(app, ["orient", "--full"])
        assert res_full.exit_code == 0
        mock_client.get_project_context.assert_called_with(99, view="full")


def test_api_find_context_query_params() -> None:
    """find_context passes team, types, since as query params only when given."""
    cfg = Config(
        api_key="dummy_key",
        api_url="http://test.local",
        config_path=None,
        mcp_path=None,
        fingerprint="test",
        project_id=1,
    )
    client = MomentApiClient(cfg)

    calls: list[tuple[str, dict]] = []

    def mock_post(route: str, body: Any = None) -> dict:
        calls.append((route, body))
        return {"results": []}

    client.post = mock_post  # type: ignore[assignment]

    # No filters given
    client.find_context("auth", project_id=10, limit=5)
    route, body = calls[-1]
    assert route == "/api/v1/agents/find-context"
    assert body == {"query": "auth", "projectId": 10, "limit": 5}

    # team given
    client.find_context("auth", team="core")
    route, _ = calls[-1]
    assert route == "/api/v1/agents/find-context?team=core"

    # types given
    client.find_context("auth", types="PAGE,RESOURCE")
    route, _ = calls[-1]
    assert "types=PAGE,RESOURCE" in route

    # since given
    client.find_context("auth", since="30d")
    route, _ = calls[-1]
    assert "since=30d" in route

    # all three given
    client.find_context("auth", team="core", types="PAGE,RESOURCE", since="30d")
    route, _ = calls[-1]
    assert "team=core" in route
    assert "types=PAGE,RESOURCE" in route
    assert "since=30d" in route


def test_find_cli_options() -> None:
    """find forwards one scope plus --types/--since; two scopes are refused locally,
    because the server rejects projectId/team/campaign/user together."""
    mock_client = MagicMock()
    mock_client.find_context.return_value = {"results": []}

    with patch("moment_cli.cli._get_client", return_value=mock_client):
        res = runner.invoke(app, [
            "find", "auth", "--team", "infra", "--types", "PAGE,RESOURCE", "--since", "14d",
        ])
        assert res.exit_code == 0, res.output
        mock_client.find_context.assert_called_with(
            "auth", None, None, team="infra", types="PAGE,RESOURCE", since="14d",
            campaign=None, user=None,
        )

        res = runner.invoke(app, ["find", "auth", "--project", "7", "--since", "14d"])
        assert res.exit_code == 0, res.output
        mock_client.find_context.assert_called_with(
            "auth", 7, None, team=None, types=None, since="14d", campaign=None, user=None,
        )

        mock_client.find_context.reset_mock()
        res = runner.invoke(app, ["find", "auth", "--project", "7", "--team", "infra"])
        assert res.exit_code == 2
        mock_client.find_context.assert_not_called()


def test_raw_markdown_is_stale_unit() -> None:
    """Unit test raw_markdown_is_stale with various timestamp combinations."""
    # Stale: structured patch > 5s after rawMarkdown
    assert raw_markdown_is_stale("2026-08-29T10:05:00Z", "2026-08-29T10:00:00Z") is True

    # Fresh: exactly equal or within same-write slack
    assert raw_markdown_is_stale("2026-08-29T10:00:00Z", "2026-08-29T10:00:00Z") is False
    assert raw_markdown_is_stale("2026-08-29T10:00:03Z", "2026-08-29T10:00:00Z") is False

    # rawMarkdownUpdatedAt is None = legacy row (pre-backfill) -> NOT stale (server parity)
    assert raw_markdown_is_stale("2026-08-29T10:05:00Z", None) is False

    # Missing updatedAt -> cannot be stale
    assert raw_markdown_is_stale(None, "2026-08-29T10:00:00Z") is False
    assert raw_markdown_is_stale(None, None) is False

    # Garbage dates -> not stale
    assert raw_markdown_is_stale("invalid-date", "2026-08-29T10:00:00Z") is False

    # Dict argument call
    assert raw_markdown_is_stale({
        "updatedAt": "2026-08-29T10:05:00Z",
        "rawMarkdownUpdatedAt": "2026-08-29T10:00:00Z",
    }) is True
    assert raw_markdown_is_stale({
        "updatedAt": "2026-08-29T10:00:00Z",
        "rawMarkdownUpdatedAt": "2026-08-29T10:00:00Z",
    }) is False


def main() -> int:
    """Standalone runner for test_orient_render."""
    tests = [
        ("fresh rawMarkdown renders raw", test_fresh_raw_markdown_renders_raw),
        ("stale rawMarkdown renders structured first", test_stale_raw_markdown_renders_structured_first),
        ("absent sections don't crash", test_absent_sections_dont_crash),
        ("must read section and clamping", test_must_read_section_and_clamping),
        ("doors section", test_doors_section),
        ("next calls section", test_next_calls_section),
        ("api get_project_context view param", test_api_get_project_context_view_param),
        ("orient cli --full flag", test_orient_cli_full_flag),
        ("api find_context query params", test_api_find_context_query_params),
        ("find cli options", test_find_cli_options),
        ("raw_markdown_is_stale unit", test_raw_markdown_is_stale_unit),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            failed += 1

    print()
    print("RESULT: " + ("ALL PASS" if failed == 0 else f"{failed} FAILURE(S)"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
