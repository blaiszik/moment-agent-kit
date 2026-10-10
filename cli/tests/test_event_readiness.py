from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from moment_cli import api as api_mod
from moment_cli import cli as cli_mod
from moment_cli import config as config_mod
from moment_cli import human_auth
from moment_cli import __version__
from moment_cli.api import ApiError


API_URL = "http://moment.test"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    for name in (
        "MOMENT_API_KEY",
        "MOMENT_HUMAN_TOKEN",
        "MOMENT_API_URL",
        "MOMENT_MCP_API_KEY",
        "MOMENT_MCP_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    (work / ".moment.json").write_text(json.dumps({"apiUrl": API_URL}))
    return home


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are unavailable on Windows")
def test_home_config_write_and_read_repair_mode(isolated_home: Path) -> None:
    path = config_mod.update_home_config({"apiKey": "mk_secret"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o644)
    assert config_mod.read_home_config()["apiKey"] == "mk_secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _login_transport(agent_status: int, *, existing_key: str | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/auth/device/start":
            return httpx.Response(201, json={
                "deviceCode": "device-code",
                "userCode": "ABCD-EFGH",
                "verificationUriComplete": f"{API_URL}/auth/device?code=ABCD-EFGH",
                "expiresIn": 60,
                "interval": 0,
            })
        if request.url.path == "/api/auth/device/token":
            return httpx.Response(200, json={
                "token": "mh_person",
                "expiresAt": "2099-01-01T00:00:00.000Z",
                "user": {"id": 7, "name": "Ada", "email": "ada@example.org"},
            })
        if request.url.path == "/api/agents":
            assert request.headers["authorization"] == "Bearer mh_person"
            body = json.loads(request.content)
            assert body["type"] == "LLM"
            assert body["version"]
            assert body["name"]
            if agent_status == 201:
                return httpx.Response(201, json={"agent": {"id": 9}, "apiKey": "mk_minted"})
            return httpx.Response(agent_status, json={"error": "This person cannot mint an agent."})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    return httpx.MockTransport(handler), calls


def _run_login(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    *,
    existing_key: str | None = None,
):
    # The repo's .moment.json names the test server; the person has trusted it,
    # as they must before credentials go to any non-lab, non-loopback host.
    config_mod.update_home_config({"trustedApiUrls": [API_URL]})
    if existing_key:
        config_mod.update_home_config({"apiKey": existing_key, "apiKeySource": "supplied"})
    transport, calls = _login_transport(status, existing_key=existing_key)
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", transport)
    monkeypatch.setattr(human_auth, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(human_auth, "_open_browser", lambda _url: True)
    result = CliRunner().invoke(cli_mod.app, ["--quiet", "auth", "login", "--no-browser"])
    return result, json.loads((home / ".moment.json").read_text()), calls


def test_login_mints_and_saves_agent_key(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, saved, calls = _run_login(isolated_home, monkeypatch, 201)
    assert result.exit_code == 0, result.output
    assert saved["apiKey"] == "mk_minted"
    assert saved["apiKeySource"] == "minted by login"
    assert "Agent key minted for this machine" in result.output
    assert sum(call.url.path == "/api/agents" for call in calls) == 1


def test_login_403_continues_without_agent_key(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, saved, calls = _run_login(isolated_home, monkeypatch, 403)
    assert result.exit_code == 0, result.output
    assert "apiKey" not in saved
    assert "This person cannot mint an agent." in result.output
    assert sum(call.url.path == "/api/agents" for call in calls) == 1


def test_login_existing_agent_key_skips_mint(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, saved, calls = _run_login(isolated_home, monkeypatch, 201, existing_key="mk_existing")
    assert result.exit_code == 0, result.output
    assert saved["apiKey"] == "mk_existing"
    assert not any(call.url.path == "/api/agents" for call in calls)


@pytest.mark.parametrize(
    "failure",
    [
        ApiError("Unauthorized", 401),
        ApiError("Rate limited", 429),
        httpx.ReadTimeout("slow", request=httpx.Request("GET", API_URL)),
    ],
)
def test_hook_mode_is_silent_for_expected_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    class Client:
        def get_capture_watermark(self, _project_id: int, _repo: str):
            raise failure

    def client(*_args, **_kwargs):
        cli_mod._state["_config"] = SimpleNamespace(project_id=42)
        return Client()

    monkeypatch.setattr(cli_mod, "_get_client", client)
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: Path.cwd())
    monkeypatch.setattr(cli_mod, "_repo_slug", lambda _root: "lab/project")
    result = CliRunner().invoke(cli_mod.app, ["capture", "--hook"])
    assert result.exit_code == 0
    assert result.output == ""


def test_hook_no_change_uses_one_watermark_request_and_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str]] = []

    class Client:
        def get_capture_watermark(self, project_id: int, repo: str):
            calls.append((project_id, repo))
            return {"lastCapture": {"sourceRef": "lab/project@abc123"}}

        def post_capture(self, *_args, **_kwargs):
            raise AssertionError("a fresh watermark must not trigger a second request")

    def client(*_args, **_kwargs):
        cli_mod._state["_config"] = SimpleNamespace(project_id=42)
        return Client()

    monkeypatch.setattr(cli_mod, "_get_client", client)
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: Path.cwd())
    monkeypatch.setattr(cli_mod, "_repo_slug", lambda _root: "lab/project")
    monkeypatch.setattr(cli_mod, "_collect_commits", lambda *_args: [])
    monkeypatch.setattr(
        cli_mod,
        "_git",
        lambda args, _root: "main" if args[:2] == ["rev-parse", "--abbrev-ref"] else "",
    )
    result = CliRunner().invoke(cli_mod.app, ["--json", "capture", "--hook"])
    assert result.exit_code == 0
    assert result.output == ""
    assert calls == [(42, "lab/project")]


def test_version_is_one_plain_line() -> None:
    result = CliRunner().invoke(cli_mod.app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"moment {__version__}\n"
