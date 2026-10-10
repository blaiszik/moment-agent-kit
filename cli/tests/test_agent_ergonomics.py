"""The CLI as agents use it: flags anywhere, aliases everywhere, plain output
when piped, one error envelope, and close-outs that land where the brief reads."""

from __future__ import annotations

import json
import os
import sys

import httpx
import typer
from rich.panel import Panel
from rich.table import Table
from typer.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli import api as api_mod  # noqa: E402
from moment_cli import display  # noqa: E402
from moment_cli.cli import _hoist_global_options, app  # noqa: E402

ENV_NAMES = (
    "MOMENT_API_KEY", "MOMENT_MCP_API_KEY", "MOMENT_MCP_KEY", "MOMENT_HUMAN_TOKEN",
    "MOMENT_API_URL", "MOMENT_MCP_API_URL", "MCP_MOMENT_API_URL",
    "MOMENT_PROJECT", "MOMENT_PROJECT_ID", "MOMENT_OUTPUT",
)


def _env(tmp_path, monkeypatch, project_id=42):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    (repo / ".moment.json").write_text(json.dumps({
        "apiUrl": "http://localhost:3000",
        "defaultProject": "detector",
        "projects": {"detector": {"id": project_id, "title": "Aurora detector"}},
    }))
    (home / ".moment.json").write_text(json.dumps({"apiKey": "mk_test_agent_key_long_enough"}))
    return repo


def _serve(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(record))
    return seen


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# --- Global options anywhere -------------------------------------------------


def test_global_options_are_hoisted_only_where_the_command_lacks_them():
    root = typer.main.get_command(app)
    hoist = lambda *args: _hoist_global_options(root, list(args))  # noqa: E731

    assert hoist("orient", "--json") == ["--json", "orient"]
    assert hoist("--json", "orient") == ["--json", "orient"]
    # A value of the command's own option is never mistaken for a global flag.
    assert hoist("log", "--note", "-p is fine", "-p", "42") == ["-p", "42", "log", "--note", "-p is fine"]
    assert hoist("page", "--title", "T", "--body", "-", "--json") == ["--json", "page", "--title", "T", "--body", "-"]
    assert hoist("project", "page-read", "readme", "--project=detector", "--quiet") == [
        "--project=detector", "--quiet", "project", "page-read", "readme",
    ]
    # `task list` has its own -p (a filter), so it stays where it is.
    assert hoist("task", "list", "-p", "5") == ["task", "list", "-p", "5"]
    # Unknown commands are left for the parser to report.
    assert hoist("nosuch", "--json") == ["nosuch", "--json"]
    assert hoist("log", "--", "--json") == ["log", "--", "--json"]


def test_orient_json_after_the_subcommand(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"project": {"id": 42}, "promptContext": "brief"}))
    result = CliRunner().invoke(app, ["orient", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["project"]["id"] == 42


# --- Projects by id or alias -------------------------------------------------


def test_project_arguments_take_an_alias_or_fall_back_to_the_binding(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"page": {"slug": "readme", "body": "x"}, "resources": []}))
    runner = CliRunner()

    assert runner.invoke(app, ["project", "page-read", "detector", "readme"]).exit_code == 0
    assert runner.invoke(app, ["page-read", "readme"]).exit_code == 0
    assert runner.invoke(app, ["project", "page-read", "readme"]).exit_code == 0
    assert runner.invoke(app, ["project", "resources"]).exit_code == 0
    assert runner.invoke(app, ["project", "resources", "7"]).exit_code == 0
    assert runner.invoke(app, ["-p", "7", "project", "resources"]).exit_code == 0
    assert [r.url.path for r in seen] == [
        "/api/v1/agents/projects/42/pages/readme",
        "/api/v1/agents/projects/42/pages/readme",
        "/api/v1/agents/projects/42/pages/readme",
        "/api/v1/agents/projects/42/resources",
        "/api/v1/agents/projects/7/resources",
        "/api/v1/agents/projects/7/resources",
    ]

    unknown = runner.invoke(app, ["--json", "project", "resources", "nosuch"])
    assert unknown.exit_code == 2
    error = json.loads(unknown.stderr)
    assert "Unknown project alias" in error["error"]


def test_task_list_filters_by_an_explicit_project_only(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"tasks": []}))
    runner = CliRunner()
    assert runner.invoke(app, ["--json", "task", "list"]).exit_code == 0
    assert runner.invoke(app, ["--json", "-p", "detector", "task", "list"]).exit_code == 0
    assert "projectId" not in str(seen[0].url)
    assert seen[1].url.params["projectId"] == "42"


def test_find_forgives_the_spelling_of_types(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    result = CliRunner().invoke(app, ["find", "close-out", "--type", "pages,session-notes,Task"])
    assert result.exit_code == 0, result.output
    assert _body(seen[0])["types"] == "PAGE,SESSION_NOTE,TASK"


# --- Close-out ---------------------------------------------------------------


def _created(request):
    return httpx.Response(201, json={
        "success": True, "sessionNote": {"id": 9}, "message": "Session note logged and handoff refreshed.",
    })


def test_log_next_updates_the_brief_and_new_handoff_fields_travel(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, _created)
    result = CliRunner().invoke(app, [
        "log", "--note", "Did the thing.", "--no-tree", "--next", "Ship it.",
        "--read-first", "docs/a.md :: the contract", "--read-first", "page:roadmap",
        "--decisions", "Use pytest.", "--decisions", "Keep aliases.",
        "--tried-and-failed", "Mocking the server: drifted.", "--open-questions", "Cap the brief?",
    ])
    assert result.exit_code == 0, result.output
    body = _body(seen[0])
    assert body["nextStep"] == "Ship it."
    assert body["handoff"] == {
        "nextStep": "Ship it.",
        "decisions": "Use pytest.\nKeep aliases.",
        "triedAndFailed": "Mocking the server: drifted.",
        "openQuestions": "Cap the brief?",
        "readFirst": [{"label": "docs/a.md", "why": "the contract"}, {"label": "page:roadmap"}],
    }
    assert "note #9" in " ".join(result.output.split())


def test_a_plain_note_leaves_the_handoff_alone(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, _created)
    assert CliRunner().invoke(app, ["log", "--note", "Read-only look.", "--no-tree"]).exit_code == 0
    assert "handoff" not in _body(seen[0])


def test_project_log_is_log_and_reads_the_note_from_stdin(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, _created)
    result = CliRunner().invoke(
        app, ["project", "log", "detector", "--note", "-", "--no-tree", "--watch-out", "Slow CI."],
        input="Multi-line\nnote from a heredoc.\n",
    )
    assert result.exit_code == 0, result.output
    assert seen[0].url.path == "/api/v1/agents/projects/42/session-notes"
    body = _body(seen[0])
    assert body["note"] == "Multi-line\nnote from a heredoc."
    assert body["handoff"] == {"watchOut": "Slow CI."}


def test_an_oversized_note_is_refused_before_any_request(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, _created)
    result = CliRunner().invoke(app, ["--json", "log", "--note", "x" * 4001, "--no-tree"])
    assert result.exit_code == 2
    assert seen == []
    assert "4000" in json.loads(result.stderr)["error"]


# --- Pages -------------------------------------------------------------------


def test_editing_a_page_does_not_unpin_it(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"success": True}))
    runner = CliRunner()
    assert runner.invoke(app, ["page", "--title", "T", "--body", "B", "--slug", "s"]).exit_code == 0
    assert runner.invoke(app, ["page", "--title", "T", "--body", "B", "--slug", "s", "--pinned"]).exit_code == 0
    assert runner.invoke(app, ["page", "--title", "T", "--body", "B", "--slug", "s", "--unpinned", "--if-version", "3"]).exit_code == 0
    first, second, third = (_body(r) for r in seen)
    assert "pinned" not in first
    assert second["pinned"] is True
    assert third["pinned"] is False and third["ifVersion"] == 3


def test_writes_confirm_in_one_line_and_say_when_queued(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    replies = iter([
        httpx.Response(201, json={"success": True, "created": True, "indexed": "done",
                                  "page": {"slug": "notes", "title": "Notes", "version": 1, "pinned": False}}),
        httpx.Response(201, json={"success": True, "ingesting": True,
                                  "resource": {"id": 131, "kind": "CODE_REPO", "title": "moment-next", "uri": "https://github.com/x/y"}}),
        httpx.Response(200, json={"success": True, "resource": {"id": 131, "kind": "CODE_REPO", "title": "moment-next"}}),
        httpx.Response(202, json={"success": True, "queued": True,
                                  "proposal": {"id": 77, "kind": "PAGE_UPSERT", "summary": "Update page notes"}}),
    ])
    _serve(monkeypatch, lambda r: next(replies))
    runner = CliRunner()
    page = runner.invoke(app, ["page", "--title", "Notes", "--body", "x", "--slug", "notes"])
    assert page.stdout == 'Created page "Notes" (notes · v1). Searchable now.\n'
    added = runner.invoke(app, ["project", "add-resource", "https://github.com/x/y"])
    assert added.stdout == 'Attached #131 CODE_REPO "moment-next" (new). Content is ingesting in the background.\n'
    again = runner.invoke(app, ["project", "add-resource", "https://github.com/x/y"])
    assert "(already on file)" in again.stdout
    queued = runner.invoke(app, ["page", "--title", "Notes", "--body", "y", "--slug", "notes"])
    assert queued.stdout == "Queued for an editor (proposal #77): Update page notes. Not applied yet.\n"


def test_find_names_the_command_that_reads_each_hit(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"results": [
        {"entityType": "PAGE", "entityId": 5, "projectId": 42, "title": "Roadmap", "ref": "roadmap", "snippet": "Done:\n  the loop"},
        {"entityType": "SESSION_NOTE", "entityId": 9, "projectId": 42, "title": "Session note", "ref": None, "snippet": "x"},
    ]}))
    result = CliRunner().invoke(app, ["find", "roadmap"])
    lines = result.stdout.splitlines()
    assert lines[0] == '1. PAGE "Roadmap" · project 42 → moment -p 42 page-read roadmap'
    assert lines[1] == "   Done: the loop"
    assert lines[2] == '2. SESSION_NOTE #9 "Session note" · project 42'
    assert "Done" not in CliRunner().invoke(app, ["--quiet", "find", "roadmap"]).stdout


# --- Errors ------------------------------------------------------------------


def test_api_errors_carry_the_servers_recovery_step(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _serve(monkeypatch, lambda r: httpx.Response(404, json={
        "success": False, "error": "Page not found.", "code": "not_found",
        "nextStep": "List pages with orient.", "retryable": False,
    }))
    result = CliRunner().invoke(app, ["--json", "page-read", "nope"])
    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["error"] == "Page not found."
    assert error["status"] == 404 and error["code"] == "not_found" and error["retryable"] is False
    assert error["hint"] == "List pages with orient."


def test_unreachable_server_names_the_url(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def refuse(request):
        raise httpx.ConnectError("Connection refused", request=request)

    _serve(monkeypatch, refuse)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "http://localhost:3000" in flat and "moment doctor" in flat


def test_non_json_responses_are_named_not_crashed_on(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _serve(monkeypatch, lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    result = CliRunner().invoke(app, ["--json", "page-read", "readme"])
    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["status"] == 502 and "non-JSON" in error["error"]


def test_usage_errors_are_json_under_json(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    runner = CliRunner()
    for args, needle in (
        (["log", "--json"], "--note"),
        (["--json", "page-read"], "Missing argument"),
        (["--json", "nosuch"], "No such command"),
        (["--json", "--bogus", "orient"], "No such option"),
        (["--json", "campaign", "targets"], "No campaign selected"),
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, args
        assert needle in json.loads(result.stderr)["error"], args
    # Without --json, the parser's own plain message stands.
    plain = runner.invoke(app, ["nosuch"])
    assert plain.exit_code == 2 and "No such command" in plain.output


# --- Output ------------------------------------------------------------------


def test_piped_orient_leads_with_the_lean_brief_and_names_the_next_calls(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"project": {"id": 42}, "promptContext": "▶ NEXT STEP: ship."}))
    result = CliRunner().invoke(app, ["orient"])
    assert result.exit_code == 0, result.output
    assert seen[0].url.params["view"] == "spinup"
    first, footer = result.stdout.rstrip("\n").split("\n")
    assert first == "▶ NEXT STEP: ship."
    assert "moment orient --complete" in footer and "moment page-read <slug>" in footer

    quiet = CliRunner().invoke(app, ["--quiet", "orient", "--complete"])
    assert quiet.stdout == "▶ NEXT STEP: ship.\n"
    assert seen[1].url.params["view"] == "brief"


def test_piped_json_is_compact_utf8(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"project": {"id": 42, "title": "Moment — engine"}}))
    result = CliRunner().invoke(app, ["--json", "project", "show"])
    assert result.exit_code == 0, result.output
    assert result.stdout == '{"project":{"id":42,"title":"Moment — engine"}}\n'


def test_plain_mode_flattens_panels_and_tables(monkeypatch):
    monkeypatch.setenv("MOMENT_OUTPUT", "plain")
    table = Table(title="Pages")
    table.add_column("Slug")
    table.add_column("Title")
    table.add_row("readme", "Read | me")
    with display.console.capture() as captured:
        display.console.print(Panel("[bold]Body[/] line", title="Orient", subtitle="v3"))
        display.console.print(table)
    text = captured.get()
    assert text == "## Orient\nBody line\nv3\n## Pages\n| Slug | Title |\n|---|---|\n| readme | Read \\| me |\n"


def test_commands_is_a_small_map_with_details_on_request(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    index = CliRunner().invoke(app, ["--json", "commands"])
    assert index.exit_code == 0, index.output
    catalog = json.loads(index.stdout)
    assert len(index.stdout) < 20_000
    by_name = {entry["command"]: entry for entry in catalog["commands"]}
    assert by_name["page-read"]["usage"] == "moment page-read [PROJECT] SLUG"
    assert "options" not in by_name["log"]
    assert any("--json" in option["flags"] for option in catalog["globalOptions"])

    detail = json.loads(CliRunner().invoke(app, ["--json", "commands", "log"]).stdout)
    log_flags = {flag for option in detail["commands"][0]["options"] for flag in option["flags"]}
    assert {"--note", "--next", "--read-first", "--if-version"} <= log_flags
    assert "--sync" not in log_flags

    text = CliRunner().invoke(app, ["commands"]).stdout
    start = text.index("Start here:")
    assert text.index("moment orient", start) < text.index("Everything else")

    scoped = CliRunner().invoke(app, ["commands", "checklist"])
    assert scoped.exit_code == 0
    assert "moment checklist add [PROJECT] TEXT" in scoped.stdout
    assert "--url <text>" in scoped.stdout
    assert "moment log" not in scoped.stdout

    everything = CliRunner().invoke(app, ["commands", "--all"]).stdout
    assert "--read-first <text>" in everything and "moment checklist add" in everything
    assert CliRunner().invoke(app, ["commands", "nosuch"]).exit_code == 2


def test_init_reports_json_and_accepts_the_global_project(tmp_path, monkeypatch):
    repo = _env(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, [
        "--json", "-p", "140", "init", "--alias", "event", "--no-claude", "--no-hooks",
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["alias"] == "event" and report["projectId"] == 140 and report["default"] is False
    written = json.loads((repo / ".moment.json").read_text())
    assert written["projects"]["event"]["id"] == 140
    assert written["defaultProject"] == "detector"


def test_orient_short_and_complete_name_the_server_views(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={"project": {"id": 42}, "promptContext": "brief"}))
    runner = CliRunner()
    for args, view in (
        (["orient", "--short"], "spinup"),
        (["orient", "--complete"], "brief"),
        (["orient", "--view", "full"], "full"),  # the old names still work, hidden
    ):
        assert runner.invoke(app, args).exit_code == 0, args
        assert seen[-1].url.params["view"] == view, args
    assert "--complete" in runner.invoke(app, ["orient", "--help"]).stdout
    assert "--view" not in runner.invoke(app, ["orient", "--help"]).stdout


def test_onboard_without_credentials_asks_for_a_person_when_not_interactive(tmp_path, monkeypatch):
    repo = _env(tmp_path, monkeypatch)
    (tmp_path / "home" / ".moment.json").write_text("{}")
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json={}))
    result = CliRunner().invoke(app, ["--json", "onboard", "-p", "42", "--no-claude", "--no-hooks"])
    assert result.exit_code == 2
    assert "sign in through the browser" in json.loads(result.stderr)["error"]
    assert seen == []
    assert json.loads((repo / ".moment.json").read_text())["projects"]["detector"]["id"] == 42


def test_onboard_at_a_terminal_signs_in_first(tmp_path, monkeypatch):
    from moment_cli import cli as cli_mod

    _env(tmp_path, monkeypatch)
    (tmp_path / "home" / ".moment.json").write_text("{}")
    calls = []

    monkeypatch.setattr(cli_mod, "_interactive", lambda: True)
    monkeypatch.setattr(cli_mod.human_auth, "run_device_login", lambda cfg, **kw: calls.append(cfg.api_url))
    # After the sign-in the person has no agent key yet, so onboard stops at the key check;
    # the point here is that the browser sign-in ran first.
    CliRunner().invoke(app, ["onboard", "-p", "42", "--no-claude", "--no-hooks", "--yes"])
    assert calls == ["http://localhost:3000"]
