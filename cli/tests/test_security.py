"""Where credentials may go, and what server text may do to a terminal."""

from __future__ import annotations

import json
import os
import stat
import sys

import httpx
import pytest
import typer
from typer.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli import api as api_mod  # noqa: E402
from moment_cli import campaign as campaign_mod  # noqa: E402
from moment_cli import close_out  # noqa: E402
from moment_cli.cli import _hoist_global_options, _watermark_base_sha, app  # noqa: E402

EVIL = "https://evil.example"
ENV_NAMES = (
    "MOMENT_API_KEY", "MOMENT_MCP_API_KEY", "MOMENT_MCP_KEY", "MOMENT_HUMAN_TOKEN",
    "MOMENT_API_URL", "MOMENT_MCP_API_URL", "MCP_MOMENT_API_URL",
    "MOMENT_PROJECT", "MOMENT_PROJECT_ID", "MOMENT_OUTPUT",
)


def _env(tmp_path, monkeypatch, *, repo_url=None, home_extra=None, mcp_url=None):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    binding = {"defaultProject": "detector", "projects": {"detector": {"id": 42}}}
    if repo_url:
        binding["apiUrl"] = repo_url
    (repo / ".moment.json").write_text(json.dumps(binding))
    if mcp_url:
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"moment-research": {
            "command": "node", "args": ["server.js", "--api-url", mcp_url],
        }}}))
    (home / ".moment.json").write_text(json.dumps({"apiKey": "mk_secret_key_long_enough", **(home_extra or {})}))
    return home, repo


def _serve(monkeypatch, body=None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body if body is not None else {"page": {"slug": "readme", "body": "ok"}})

    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    return seen


# --- A checkout chooses the server; only the person vouches for it ------------


def test_a_repo_chosen_server_never_receives_the_key(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, repo_url=EVIL)
    seen = _serve(monkeypatch)
    result = CliRunner().invoke(app, ["--json", "page-read", "readme"])
    assert result.exit_code == 2
    assert seen == []
    error = json.loads(result.stderr)
    assert "Not sending your Moment credentials to https://evil.example" in error["error"]
    assert "trustedApiUrls" in error["hint"]


def test_a_walked_up_mcp_json_cannot_vouch_either(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, mcp_url=EVIL)
    seen = _serve(monkeypatch)
    assert CliRunner().invoke(app, ["page-read", "readme"]).exit_code == 2
    assert seen == []


@pytest.mark.parametrize("how", ["flag", "env", "trusted-list"])
def test_the_person_can_choose_the_server(tmp_path, monkeypatch, how):
    home_extra = {"trustedApiUrls": [EVIL + "/"]} if how == "trusted-list" else None
    _env(tmp_path, monkeypatch, repo_url=EVIL, home_extra=home_extra)
    if how == "env":
        monkeypatch.delenv("MOMENT_API_URL", raising=False)
    seen = _serve(monkeypatch)
    args = ["page-read", "readme"]
    if how == "flag":
        args = ["--api-url", EVIL, *args]
    if how == "env":
        # The repo file outranks the environment for *selection*, so the env
        # value only vouches when it is the one that wins.
        (tmp_path / "repo" / ".moment.json").write_text(json.dumps({"projects": {"d": {"id": 42}}}))
        monkeypatch.setenv("MOMENT_API_URL", EVIL)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen and seen[0].headers["authorization"] == "Bearer mk_secret_key_long_enough"


def test_loopback_and_the_lab_are_trusted_by_default(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, repo_url="http://localhost:3000")
    seen = _serve(monkeypatch)
    assert CliRunner().invoke(app, ["page-read", "readme"]).exit_code == 0
    assert seen[0].url.host == "localhost"


def test_sign_in_refuses_a_repo_chosen_server(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, repo_url=EVIL)
    seen = _serve(monkeypatch)
    result = CliRunner().invoke(app, ["auth", "login", "--no-browser"])
    assert result.exit_code == 2
    assert seen == []


def test_init_with_an_explicit_server_records_the_trust(tmp_path, monkeypatch):
    home, _repo = _env(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, [
        "--json", "init", "-p", "7", "--alias", "lab", "--api-url", "https://moment.mylab.org",
        "--no-claude", "--no-hooks",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["trustedApiUrl"] == "https://moment.mylab.org"
    assert json.loads((home / ".moment.json").read_text())["trustedApiUrls"] == ["https://moment.mylab.org"]


# --- Server and credential options stay in front of the subcommand ------------


def test_server_and_credential_options_are_never_hoisted():
    root = typer.main.get_command(app)
    for args in (
        ["find", "q", "--api-url=https://evil.example"],
        ["find", "q", "--api-key", "mk_attacker"],
        ["page-read", "readme", "--config=/tmp/evil.json"],
        ["checklist", "add", "-p7 is a parameter"],
    ):
        assert _hoist_global_options(root, list(args)) == args


# --- Server text is data, not terminal control --------------------------------


def test_escape_sequences_and_invisible_text_are_stripped(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    hostile = (
        "Safe line\x1b]52;c;ZXZpbA==\x07 then\x1b[8m hidden\x1b[0m "
        "tag\U000E0041\U000E0042 bidi‮evil‬ ünïcödé — kept\ttab\nnewline"
    )
    _serve(monkeypatch, {"page": {"slug": "readme", "title": "T", "body": hostile}})
    plain = CliRunner().invoke(app, ["page-read", "readme"])
    as_json = CliRunner().invoke(app, ["--json", "page-read", "readme"])
    for output in (plain.stdout, json.loads(as_json.stdout)["page"]["body"]):
        assert "\x1b" not in output and "\U000E0041" not in output and "‮" not in output
        assert "Safe line then hidden tag bidievil ünïcödé — kept\ttab\nnewline" in output


def test_hostile_markup_in_server_text_neither_crashes_nor_links(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _serve(monkeypatch, {"project": {"title": "Use [/] to close [link=https://evil.example]here[/link]"}})
    result = CliRunner().invoke(app, ["project", "show"])
    assert result.exit_code == 0, result.output
    assert "Use [/] to close" in result.stdout
    assert "link=" not in result.stdout


# --- Files and argv -------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are unavailable on Windows")
def test_a_readable_home_config_is_made_private_on_first_use(tmp_path, monkeypatch):
    home, _repo = _env(tmp_path, monkeypatch)
    (home / ".moment.json").chmod(0o644)
    _serve(monkeypatch)
    CliRunner().invoke(app, ["page-read", "readme"])
    assert stat.S_IMODE((home / ".moment.json").stat().st_mode) == 0o600


def test_git_arguments_from_flags_and_the_server_cannot_become_options():
    assert close_out.commits_summary("--output=/tmp/pwned") is None
    assert _watermark_base_sha("owner/repo@--output=/tmp/pwned") is None
    assert _watermark_base_sha("owner/repo@abc1234..def5678") == "def5678"


def test_the_submission_draft_lives_in_a_private_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = campaign_mod._document_path(42, "../../etc/first light")
    assert path.parent == tmp_path / ".cache" / "moment"
    assert path.name == "moment-submission-42-etc-first-light.md"
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
