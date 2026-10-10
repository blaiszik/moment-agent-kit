"""The campaign CLI and human sign-in, against mocked HTTP.

Every case runs on an httpx MockTransport and a throwaway ``$HOME``: nothing
here touches a live server or the developer's real ``~/.moment.json``. The
request log is asserted on directly, because the contract this lane implements
is the shape of the bodies as much as the words on screen.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli import api as api_mod  # noqa: E402
from moment_cli import human_auth  # noqa: E402
from moment_cli.cli import app  # noqa: E402

API_URL = "http://localhost:3000"
SLUG = "first-light"
PROJECT_ID = 42
HUMAN_TOKEN = "mh_test_token"
MOMENT_ENV_VARS = (
    "MOMENT_API_KEY",
    "MOMENT_MCP_API_KEY",
    "MOMENT_MCP_KEY",
    "MOMENT_HUMAN_TOKEN",
    "MOMENT_API_URL",
    "MOMENT_MCP_API_URL",
    "MCP_MOMENT_API_URL",
    "MOMENT_PROJECT",
    "MOMENT_PROJECT_ID",
    "VISUAL",
    "EDITOR",
)


class Recorder:
    """A MockTransport whose handlers are keyed by ``"METHOD /path"``."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.method} {request.url.path}"
        handler = self.routes.get(key)
        if handler is None:
            raise AssertionError(f"unexpected request: {key}?{request.url.query.decode()}")
        answer = handler(request) if callable(handler) else handler
        if isinstance(answer, httpx.Response):
            return answer
        status, body = answer
        return httpx.Response(status, json=body)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.calls[index].content.decode())

    def find(self, method: str, path: str) -> httpx.Request:
        for call in self.calls:
            if call.method == method and call.url.path == path:
                return call
        raise AssertionError(f"no {method} {path} in {[c.url.path for c in self.calls]}")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway HOME plus a cwd with no real config above it."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.chdir(work_dir)
    for name in MOMENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert Path.home() == home_dir
    return home_dir


@pytest.fixture
def repo_config(tmp_path) -> Path:
    """The committed, non-secret half of the cascade."""
    path = tmp_path / "work" / ".moment.json"
    path.write_text(json.dumps({
        "apiUrl": API_URL,
        "defaultProject": "event",
        "projects": {"event": {"id": PROJECT_ID, "title": "Aurora detector"}},
    }))
    return path


def write_home(home: Path, **extra) -> Path:
    path = home / ".moment.json"
    data = {
        "humanToken": HUMAN_TOKEN,
        "humanTokenExpiresAt": "2099-01-01T00:00:00.000Z",
        "humanUser": {"id": 7, "name": "Ada Lovelace", "email": "ada@example.org"},
        "campaignContext": {f"{API_URL}|7|{PROJECT_ID}": SLUG},
    }
    data.update(extra)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def run(recorder: Recorder, monkeypatch, repo_config: Path, *args):
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", recorder.transport())
    return CliRunner().invoke(app, ["--config", str(repo_config), "--quiet", *args])


def view(**overrides) -> dict:
    """A SubmissionView as the server sends it (submissions-types.ts)."""
    base = {
        "id": 10,
        "campaign": {"slug": SLUG, "title": "First Light", "kind": "EVENT"},
        "project": {"id": PROJECT_ID, "title": "Aurora detector", "kind": "EVENT", "status": "ACTIVE"},
        "status": "DRAFT",
        "blurb": "We built a detector.",
        "socialPostUrl": None,
        "docLinks": [{"title": "Notes", "url": "https://example.org/notes"}],
        "images": [],
        "repoUrl": "https://github.com/lab/aurora",
        "videoUrl": None,
        "acceptedRulesAt": "2026-09-14T12:00:00.000Z",
        "submittedAt": None,
        "submittedBy": None,
        "snapshot": None,
        "window": {
            "opensAt": "2026-09-01T00:00:00.000Z",
            "closesAt": "2026-10-05T23:59:00.000Z",
            "state": "OPEN",
            "canEdit": True,
        },
        "review": None,
        "recognitions": [],
        "viewer": {"canEdit": True, "canSubmit": False, "canWithdraw": False, "isSteward": False},
        "missing": ["SOCIAL_POST_URL"],
        "permalink": None,
        "privateDraft": None,
        "updatedAt": "2026-09-15T09:30:00.000Z",
        "audience": "public",
    }
    base.update(overrides)
    return base


# --- The human device grant ----------------------------------------------------


def test_device_login_writes_the_home_file_after_one_pending_poll(home, repo_config, monkeypatch):
    polls = {"n": 0}

    def token(_request):
        polls["n"] += 1
        if polls["n"] == 1:
            return 400, {"error": "authorization_pending"}
        return 200, {
            "token": "mh_fresh",
            "expiresAt": "2026-12-31T00:00:00.000Z",
            "user": {"id": 7, "name": "Ada Lovelace", "email": "ada@example.org"},
        }

    recorder = Recorder({
        "POST /api/auth/device/start": (201, {
            "deviceCode": "dev-code-1",
            "userCode": "ABCD-EFGH",
            "verificationUri": f"{API_URL}/auth/device",
            "verificationUriComplete": f"{API_URL}/auth/device?code=ABCD-EFGH",
            "expiresIn": 600,
            "interval": 0,
        }),
        "POST /api/auth/device/token": token,
    })
    opened: list[str] = []
    monkeypatch.setattr(human_auth, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(human_auth, "_open_browser", lambda url: opened.append(url) or True)

    result = run(recorder, monkeypatch, repo_config, "auth", "login")
    assert result.exit_code == 0, result.output

    saved = json.loads((home / ".moment.json").read_text())
    assert saved["humanToken"] == "mh_fresh"
    assert saved["humanTokenExpiresAt"] == "2026-12-31T00:00:00.000Z"
    assert saved["humanUser"] == {"id": 7, "name": "Ada Lovelace", "email": "ada@example.org"}
    assert saved["campaignContext"] == {}
    # The secret never lands in the committed repo file.
    assert "humanToken" not in json.loads(repo_config.read_text())
    assert polls["n"] == 2
    assert opened == [f"{API_URL}/auth/device?code=ABCD-EFGH"]
    # The device code is never shown; the user code and URL are.
    assert "ABCD-EFGH" in result.output
    assert "dev-code-1" not in result.output


def test_device_login_json_reports_user_expiry_and_destination(home, repo_config, monkeypatch):
    recorder = Recorder({
        "POST /api/auth/device/start": (201, {
            "deviceCode": "dev-code-2",
            "userCode": "WXYZ-1234",
            "verificationUriComplete": f"{API_URL}/auth/device?code=WXYZ-1234",
            "expiresIn": 600,
            "interval": 0,
        }),
        "POST /api/auth/device/token": (200, {
            "token": "mh_fresh2",
            "expiresAt": "2026-12-31T00:00:00.000Z",
            "user": {"id": 9, "name": "Grace", "email": "grace@example.org"},
        }),
    })
    monkeypatch.setattr(human_auth, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(human_auth, "_open_browser", lambda _url: False)
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", recorder.transport())
    result = CliRunner().invoke(
        app, ["--config", str(repo_config), "--json", "auth", "login", "--no-browser"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["user"]["email"] == "grace@example.org"
    assert payload["expiresAt"] == "2026-12-31T00:00:00.000Z"
    assert payload["savedTo"] == str(home / ".moment.json")


def test_denied_and_expired_codes_exit_one_with_their_own_sentence(home, repo_config, monkeypatch):
    for error, sentence in (
        ("access_denied", "Denied in the browser."),
        ("expired_token", "The code expired. Run moment auth login again."),
    ):
        recorder = Recorder({
            "POST /api/auth/device/start": (201, {
                "deviceCode": "dev", "userCode": "AAAA-BBBB", "expiresIn": 600, "interval": 0,
            }),
            "POST /api/auth/device/token": (400, {"error": error}),
        })
        monkeypatch.setattr(human_auth, "_sleep", lambda _seconds: None)
        monkeypatch.setattr(human_auth, "_open_browser", lambda _url: False)
        result = run(recorder, monkeypatch, repo_config, "auth", "login", "--no-browser")
        assert result.exit_code == 1, result.output
        assert sentence in result.stderr


def test_slow_down_lengthens_the_wait_by_five_seconds(home, repo_config, monkeypatch):
    answers = iter([
        (400, {"error": "slow_down"}),
        (200, {
            "token": "mh_fresh3",
            "expiresAt": "2026-12-31T00:00:00.000Z",
            "user": {"id": 7, "name": "Ada", "email": "ada@example.org"},
        }),
    ])
    recorder = Recorder({
        "POST /api/auth/device/start": (201, {
            "deviceCode": "dev", "userCode": "AAAA-BBBB", "expiresIn": 600, "interval": 5,
        }),
        "POST /api/auth/device/token": lambda _r: next(answers),
    })
    waits: list[float] = []
    monkeypatch.setattr(human_auth, "_sleep", lambda seconds: waits.append(seconds))
    monkeypatch.setattr(human_auth, "_open_browser", lambda _url: False)
    result = run(recorder, monkeypatch, repo_config, "auth", "login", "--no-browser")
    assert result.exit_code == 0, result.output
    assert waits == [5, 10]


def test_logout_revokes_then_forgets_all_four_home_keys(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({"DELETE /api/auth/device/token": (200, {"revoked": True})})
    result = run(recorder, monkeypatch, repo_config, "auth", "logout")
    assert result.exit_code == 0, result.output
    assert "Signed out." in result.output
    saved = json.loads((home / ".moment.json").read_text())
    for key in ("humanToken", "humanTokenExpiresAt", "humanUser", "campaignContext"):
        assert key not in saved
    assert recorder.find("DELETE", "/api/auth/device/token").headers["authorization"] == (
        f"Bearer {HUMAN_TOKEN}"
    )


def test_whoami_json_carries_both_identities_with_nulls(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        "GET /api/me": (200, {"id": 7, "name": "Ada Lovelace", "email": "ada@example.org", "handle": "ada"}),
    })
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", recorder.transport())
    result = CliRunner().invoke(app, ["--config", str(repo_config), "--json", "auth", "whoami"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["human"]["email"] == "ada@example.org"
    assert payload["agent"] is None  # no agent key on this machine


# --- No token, no campaign work ------------------------------------------------


def test_whoami_reports_the_agent_when_there_is_no_person(home, repo_config, monkeypatch):
    (home / ".moment.json").write_text(json.dumps({"apiKey": "mk_agent_key_1234567890"}))
    recorder = Recorder({
        "GET /api/v1/agents/stats": (200, {
            "agent": {"id": 3, "name": "Aurora agent", "type": "RESEARCH", "owner": {"name": "Ada"}},
            "stats": {"totalCreditsEarned": 0, "successRate": 0},
        }),
    })
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", recorder.transport())
    result = CliRunner().invoke(app, ["--config", str(repo_config), "--json", "auth", "whoami"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["human"] is None
    assert payload["agent"]["name"] == "Aurora agent"
    # The agent key is reported as a fingerprint, never in full.
    assert "mk_agent_key_1234567890" not in result.stdout


def test_an_expired_saved_token_is_a_usage_error_before_any_request(home, repo_config, monkeypatch):
    write_home(home, humanTokenExpiresAt="2020-01-01T00:00:00.000Z")
    recorder = Recorder({})
    result = run(recorder, monkeypatch, repo_config, "campaign", "status")
    assert result.exit_code == 2, result.output
    assert "expired" in result.stderr
    assert "moment auth login" in result.stderr
    assert recorder.calls == []


def test_campaign_status_without_a_token_exits_two_and_names_the_fix(home, repo_config, monkeypatch):
    (home / ".moment.json").write_text(json.dumps({}))
    recorder = Recorder({})
    result = run(recorder, monkeypatch, repo_config, "campaign", "status", "--campaign", SLUG)
    assert result.exit_code == 2, result.output
    assert "Sign in first: moment auth login" in result.stderr
    assert recorder.calls == []


def test_status_without_a_selected_campaign_exits_two_with_two_fixes(home, repo_config, monkeypatch):
    write_home(home, campaignContext={})
    recorder = Recorder({})
    result = run(recorder, monkeypatch, repo_config, "campaign", "status")
    assert result.exit_code == 2, result.output
    assert "moment campaign use <slug>" in result.stderr
    assert "--campaign <slug>" in result.stderr


# --- status --------------------------------------------------------------------


def test_status_names_the_missing_public_post_and_the_next_command(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view())})
    result = run(recorder, monkeypatch, repo_config, "campaign", "status")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "First Light / Aurora detector" in out
    assert "Submission: Draft" in out
    assert "Submit by: 2026-10-05 23:59 (UTC)" in out
    assert "Needed:" in out
    assert "  - Add the link to your social media post." in out
    assert "Next: moment campaign submission edit" in out
    assert f"Web: {API_URL}/campaigns/{SLUG}/submit/{PROJECT_ID}" in out
    # No countdown, and the deadline is a date (DESIGN.md §7).
    assert "left" not in out.lower()
    request = recorder.find("GET", f"/api/projects/{PROJECT_ID}/submission")
    assert request.url.params["campaign"] == SLUG
    assert request.headers["authorization"] == f"Bearer {HUMAN_TOKEN}"


def test_status_shows_unpublished_changes_and_a_steward_grace(home, repo_config, monkeypatch):
    submitted = view(
        status="SUBMITTED",
        missing=[],
        submittedAt="2026-09-15T10:00:00.000Z",
        socialPostUrl="https://example.org/post",
        snapshot={"v": 1, "frozenAt": "2026-09-15T10:00:00.000Z", "members": [], "teams": []},
        privateDraft={
            "blurb": "A better write-up.",
            "socialPostUrl": "https://example.org/post",
            "docLinks": [],
            "images": [],
            "repoUrl": "https://github.com/lab/aurora",
            "videoUrl": None,
            "acceptRules": True,
            "savedAt": "2026-09-15T11:00:00.000Z",
            "savedBy": {"id": 7, "name": "Ada Lovelace"},
        },
        window={
            "opensAt": "2026-09-01T00:00:00.000Z",
            "closesAt": "2026-10-05T23:59:00.000Z",
            "graceUntil": "2026-10-07T12:00:00.000Z",
            "state": "CLOSED",
            "canEdit": True,
        },
    )
    write_home(home)
    recorder = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, submitted)})
    result = run(recorder, monkeypatch, repo_config, "campaign", "status")
    assert result.exit_code == 0, result.output
    assert "Submission: Submitted · unpublished changes" in result.output
    # A per-entry grace outranks the campaign close as this person's deadline.
    assert "Submit by: 2026-10-07 12:00 (UTC)" in result.output
    assert "Next: moment campaign submission update" in result.output


def test_status_turns_a_closed_window_into_a_window_line_not_a_chore(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(
            missing=["WINDOW_CLOSED"], socialPostUrl="https://example.org/post",
        )),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "status")
    assert result.exit_code == 0, result.output
    assert "Window: Submissions have closed." in result.output
    assert "Needed: nothing" in result.output


# --- use -----------------------------------------------------------------------


def test_use_scopes_the_selection_to_server_person_and_project(home, repo_config, monkeypatch):
    write_home(home, campaignContext={})
    recorder = Recorder({})
    result = run(recorder, monkeypatch, repo_config, "campaign", "use", "second-light")
    assert result.exit_code == 0, result.output
    assert f"project {PROJECT_ID} · campaign second-light" in result.output
    saved = json.loads((home / ".moment.json").read_text())
    assert saved["campaignContext"] == {f"{API_URL}|7|{PROJECT_ID}": "second-light"}

    result = run(recorder, monkeypatch, repo_config, "campaign", "use", "--clear")
    assert result.exit_code == 0, result.output
    assert json.loads((home / ".moment.json").read_text())["campaignContext"] == {}


# --- edit / save ----------------------------------------------------------------


DOCUMENT = """---
repoUrl: https://github.com/lab/aurora
socialPostUrl: https://example.org/post
videoUrl:
acceptRules: true
docLinks:
  - Notes | https://example.org/notes
  - Data | https://example.org/data
version: 2026-09-15T09:30:00.000Z
---

We built a detector and it saw first light.
"""


def test_edit_from_a_file_puts_the_whole_document_with_if_unchanged_since(home, repo_config, monkeypatch, tmp_path):
    write_home(home)
    document = tmp_path / "entry.md"
    document.write_text(DOCUMENT)
    saved_view = view(missing=[], socialPostUrl="https://example.org/post")
    recorder = Recorder({f"PUT /api/projects/{PROJECT_ID}/submission": (200, saved_view)})
    result = run(
        recorder, monkeypatch, repo_config,
        "campaign", "submission", "edit", "--file", str(document),
    )
    assert result.exit_code == 0, result.output
    assert recorder.body() == {
        "blurb": "We built a detector and it saw first light.",
        "socialPostUrl": "https://example.org/post",
        "repoUrl": "https://github.com/lab/aurora",
        "videoUrl": None,
        "docLinks": [
            {"title": "Notes", "url": "https://example.org/notes"},
            {"title": "Data", "url": "https://example.org/data"},
        ],
        "acceptRules": True,
        "ifUnchangedSince": "2026-09-15T09:30:00.000Z",
        "campaign": SLUG,
    }
    assert "Saved to Moment · Draft" in result.output
    assert "Needed: nothing" in result.output
    # A script's own file is never deleted for it.
    assert document.exists()


def test_edit_opens_the_editor_on_a_document_seeded_from_the_private_draft(home, repo_config, monkeypatch, tmp_path):
    write_home(home)
    seeded = view(
        status="SUBMITTED",
        socialPostUrl="https://example.org/old",
        privateDraft={
            "blurb": "Unpublished write-up.",
            "socialPostUrl": "https://example.org/new",
            "docLinks": [{"title": "Notes", "url": "https://example.org/notes"}],
            "images": [],
            "repoUrl": "https://github.com/lab/aurora",
            "videoUrl": None,
            "acceptRules": True,
            "savedAt": "2026-09-15T11:00:00.000Z",
            "savedBy": {"id": 7, "name": "Ada Lovelace"},
        },
        missing=[],
    )
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, seeded),
        f"PUT /api/projects/{PROJECT_ID}/submission": (200, seeded),
    })
    seen: dict = {}

    def fake_editor(cmd, *_args, **_kwargs):
        seen["path"] = cmd[-1]
        seen["text"] = Path(cmd[-1]).read_text()
        return 0

    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr("moment_cli.campaign.subprocess.call", fake_editor)
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "edit")
    assert result.exit_code == 0, result.output

    assert seen["path"].endswith(f"moment-submission-{PROJECT_ID}-{SLUG}.md")
    text = seen["text"]
    # The document shows the unpublished draft, not the record on file.
    assert "socialPostUrl: https://example.org/new" in text
    assert "Unpublished write-up." in text
    assert "version: 2026-09-15T09:30:00.000Z" in text
    assert "  - Notes | https://example.org/notes" in text
    # The temp file is cleaned up once the server has the edit.
    assert not Path(seen["path"]).exists()


def test_the_document_round_trips_through_render_and_parse():
    from moment_cli.campaign import _parse_document, _render_document

    source = view(
        blurb="# Aurora\n\nWe built a detector.\n\n- a list item\n---\nnot a fence",
        socialPostUrl="https://example.org/post",
        videoUrl="https://youtu.be/x",
        docLinks=[{"title": "Notes", "url": "https://example.org/notes"}],
    )
    parsed = _parse_document(_render_document(source))
    assert parsed["blurb"] == source["blurb"]
    assert parsed["socialPostUrl"] == "https://example.org/post"
    assert parsed["repoUrl"] == "https://github.com/lab/aurora"
    assert parsed["videoUrl"] == "https://youtu.be/x"
    assert parsed["docLinks"] == [{"title": "Notes", "url": "https://example.org/notes"}]
    assert parsed["acceptRules"] is True  # from acceptedRulesAt on the record
    assert parsed["version"] == "2026-09-15T09:30:00.000Z"

    # An emptied field clears; the guidance comments never become data.
    cleared = _parse_document(_render_document(view(repoUrl=None, docLinks=[])))
    assert cleared["repoUrl"] is None
    assert cleared["docLinks"] == []
    assert "#" not in "".join(cleared.keys())


def test_a_concurrent_edit_keeps_the_file_and_names_it(home, repo_config, monkeypatch, tmp_path):
    write_home(home)
    document = tmp_path / "entry.md"
    document.write_text(DOCUMENT)
    recorder = Recorder({
        f"PUT /api/projects/{PROJECT_ID}/submission": (409, {"error": "concurrent-update"}),
    })
    result = run(
        recorder, monkeypatch, repo_config,
        "campaign", "submission", "save", "--file", str(document),
    )
    assert result.exit_code == 1, result.output
    assert "concurrent-update" in result.stderr
    assert str(document) in result.stderr
    assert document.exists()


def test_a_document_with_no_front_matter_is_a_usage_error(home, repo_config, monkeypatch, tmp_path):
    write_home(home)
    document = tmp_path / "bare.md"
    document.write_text("Just a write-up, no fields.\n")
    recorder = Recorder({})
    result = run(
        recorder, monkeypatch, repo_config,
        "campaign", "submission", "save", "--file", str(document),
    )
    assert result.exit_code == 2, result.output
    assert "front matter" in result.stderr
    assert recorder.calls == []


# --- check / submit / update / discard -------------------------------------------


def test_check_exits_one_while_something_is_missing_and_zero_when_ready(home, repo_config, monkeypatch):
    write_home(home)
    incomplete = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view())})
    result = run(incomplete, monkeypatch, repo_config, "campaign", "submission", "check")
    assert result.exit_code == 1, result.output
    assert "Add the link to your social media post." in result.output

    complete = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(
            missing=[], socialPostUrl="https://example.org/post",
        )),
    })
    result = run(complete, monkeypatch, repo_config, "campaign", "submission", "check")
    assert result.exit_code == 0, result.output
    assert "Needed: nothing" in result.output


def test_submit_refused_with_409_exits_one_and_repeats_what_is_needed(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(contentRev="rev-1")),
        f"POST /api/projects/{PROJECT_ID}/submission/submit": (409, {
            "error": "Your entry is not complete yet",
            "missing": ["SOCIAL_POST_URL", "RULES"],
        }),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "submit")
    assert result.exit_code == 1, result.output
    assert "Your entry is not complete yet" in result.stderr
    assert "  - Add the link to your social media post." in result.output
    assert "  - Confirm the work follows the event rules." in result.output
    body = recorder.body()
    assert body["campaign"] == SLUG and body["ifContentRev"] == "rev-1"


def test_submit_prints_the_receipt_with_credit_visibility_and_the_record_link(home, repo_config, monkeypatch):
    write_home(home)
    receipt = view(
        status="SUBMITTED",
        missing=[],
        socialPostUrl="https://example.org/post",
        submittedAt="2026-09-15T10:00:00.000Z",
        snapshot={
            "v": 1,
            "frozenAt": "2026-09-15T10:00:00.000Z",
            "members": [
                {"id": 7, "name": "Ada Lovelace", "handle": "ada"},
                {"id": 8, "name": "Grace Hopper", "handle": "grace"},
            ],
            "teams": [],
        },
        permalink=f"/campaigns/{SLUG}/submissions/10",
        audience="public",
    )
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(contentRev="rev-1", missing=[])),
        f"POST /api/projects/{PROJECT_ID}/submission/submit": (200, receipt),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "submit")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Submission received" in out
    assert "Received" in out and "2026-09-15 10:00" in out
    assert "Ada Lovelace, Grace Hopper" in out
    assert "anyone with the link, and the event gallery" in out
    assert f"{API_URL}/campaigns/{SLUG}/submissions/10" in out


def test_update_publishes_and_prints_an_updated_receipt(home, repo_config, monkeypatch):
    write_home(home)
    submitted = view(
        status="SUBMITTED",
        missing=[],
        socialPostUrl="https://example.org/post",
        submittedAt="2026-09-15T10:00:00.000Z",
        snapshot={"v": 1, "frozenAt": "2026-09-15T10:00:00.000Z", "members": [], "teams": []},
        contentRev="rev-9",
        privateDraft={
            "blurb": "A better write-up.",
            "socialPostUrl": "https://example.org/post",
            "docLinks": [], "images": [],
            "repoUrl": "https://github.com/lab/aurora", "videoUrl": None,
            "acceptRules": True,
            "savedAt": "2026-09-15T11:00:00.000Z",
            "savedBy": {"id": 7, "name": "Ada Lovelace"},
        },
        permalink=f"/campaigns/{SLUG}/submissions/10",
    )
    published = view(
        status="SUBMITTED",
        missing=[],
        socialPostUrl="https://example.org/post",
        submittedAt="2026-09-15T10:00:00.000Z",
        refrozenAt="2026-09-15T12:00:00.000Z",
        snapshot={
            "v": 1,
            "frozenAt": "2026-09-15T12:00:00.000Z",
            "members": [{"id": 7, "name": "Ada Lovelace", "handle": "ada"}],
            "teams": [],
        },
        permalink=f"/campaigns/{SLUG}/submissions/10",
        privateDraft=None,
        audience="lab",
    )
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, submitted),
        f"POST /api/projects/{PROJECT_ID}/submission/publish": (200, published),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "update")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Submission updated" in out
    assert "Updated" in out and "2026-09-15 12:00" in out
    assert "Version" in out
    assert "signed-in lab members" in out
    assert recorder.find("POST", f"/api/projects/{PROJECT_ID}/submission/publish")
    assert json.loads(recorder.calls[-1].content.decode()) == {"campaign": SLUG, "ifContentRev": "rev-9"}


def test_update_on_a_draft_says_to_submit_instead(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view())})
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "update")
    assert result.exit_code == 1, result.output
    assert "Nothing is published yet — use submit." in result.stderr
    # A publish is never attempted for a draft.
    assert all(call.url.path.endswith("/submission") for call in recorder.calls)


def test_discard_deletes_the_private_draft_only(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        f"DELETE /api/projects/{PROJECT_ID}/submission/draft": (200, view(status="SUBMITTED", privateDraft=None)),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "discard")
    assert result.exit_code == 0, result.output
    assert "Unpublished changes discarded." in result.output
    assert recorder.find("DELETE", f"/api/projects/{PROJECT_ID}/submission/draft").url.params["campaign"] == SLUG


# --- list / show ------------------------------------------------------------------


def test_list_and_show_render_without_an_agent_key(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        "GET /api/campaigns": (200, {"campaigns": [
            {"slug": SLUG, "title": "First Light", "kind": "EVENT", "status": "ACTIVE"},
        ]}),
        f"GET /api/campaigns/{SLUG}": (200, {"campaign": {
            "slug": SLUG,
            "title": "First Light",
            "tagline": "Three days of open instruments.",
            "kind": "EVENT",
            "status": "ACTIVE",
            "submissionsOpenAt": "2026-09-01T00:00:00.000Z",
            "submissionsCloseAt": "2026-10-05T23:59:00.000Z",
            "submissionRequirements": "\n".join(f"line {n}" for n in range(1, 15)),
        }}),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "list")
    assert result.exit_code == 0, result.output
    assert SLUG in result.output and "EVENT" in result.output

    result = run(recorder, monkeypatch, repo_config, "campaign", "show", SLUG)
    assert result.exit_code == 0, result.output
    assert "Three days of open instruments." in result.output
    assert "2026-10-05 23:59 (UTC)" in result.output
    assert "line 10" in result.output
    assert "line 11" not in result.output  # first 10 lines only


# --- needs, fallback, stale revisions, withdraw, mine -----------------------------


NEEDS = [
    {"code": "BLURB", "sentence": "Write up what you built.", "situational": False},
    {"code": "WINDOW_CLOSED", "sentence": "Submissions have closed.", "situational": True},
]


def test_check_prints_the_servers_needs_and_exits_one_only_for_chores(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view(
        missing=["BLURB", "WINDOW_CLOSED"], needs=NEEDS,
    ))})
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "check")
    assert result.exit_code == 1, result.output
    assert "  - Write up what you built." in result.output
    assert "Window: Submissions have closed." in result.output

    only_window = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view(
        missing=["WINDOW_CLOSED"], needs=NEEDS[1:],
    ))})
    result = run(only_window, monkeypatch, repo_config, "campaign", "submission", "check")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("code", [
    "BLURB", "INLINE_IMAGE_INVALID", "SOCIAL_POST_URL", "SOCIAL_POST_URL_INVALID", "REPO_URL",
    "REPO_URL_INVALID", "VIDEO_URL_INVALID", "DOC_LINK_INVALID", "RULES", "PROJECT_PRIVATE",
])
def test_check_without_needs_still_fails_on_every_chore_code(home, repo_config, monkeypatch, code):
    write_home(home)
    recorder = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view(missing=[code]))})
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "check")
    assert result.exit_code == 1, result.output
    assert "Needed:\n  - " in result.output
    assert "nothing" not in result.output


def test_fallback_has_a_sentence_for_every_missing_code():
    from moment_cli import campaign

    chores = {"BLURB", "INLINE_IMAGE_INVALID", "SOCIAL_POST_URL", "SOCIAL_POST_URL_INVALID", "REPO_URL",
              "REPO_URL_INVALID", "VIDEO_URL_INVALID", "DOC_LINK_INVALID", "RULES", "PROJECT_PRIVATE"}
    assert set(campaign.NEED_SENTENCES) == chores | campaign.SITUATIONAL_CODES


def test_a_stale_submit_prints_the_servers_sentence_and_the_recheck(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(contentRev="rev-1", missing=[])),
        f"POST /api/projects/{PROJECT_ID}/submission/submit": (409, {
            "error": "Rin changed the write-up after you reviewed it. Review again.",
        }),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "submit")
    assert result.exit_code == 1, result.output
    assert "Rin changed the write-up after you reviewed it. Review again." in result.stderr
    assert "moment campaign submission check" in result.stderr


def test_field_revs_match_the_servers_algorithm():
    from moment_cli import campaign

    # Same inputs as submissionFieldRev in submissions-types.ts (FNV-1a, UTF-16 units, hex length).
    # Values produced by running the TypeScript algorithm itself under node.
    assert campaign._field_rev(None) == "77074ba44"
    assert campaign._field_rev([]) == "741638a52"
    assert campaign._field_rev([{"url": "https://x", "title": "Né"}]) == "92f4263022"
    assert campaign._field_rev("héllo 😀") == "8b80b396a"
    assert campaign._field_rev(True) == "4db211e54"
    assert campaign._field_rev({"b": 1, "a": "x"}) == campaign._field_rev({"a": "x", "b": 1})


def test_submit_sends_reviewed_fields_for_every_editable_field(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(contentRev="rev-1", missing=[])),
        f"POST /api/projects/{PROJECT_ID}/submission/submit": (200, view(status="SUBMITTED", missing=[])),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "submit")
    assert result.exit_code == 0, result.output
    assert set(recorder.body()["reviewedFields"]) == {
        "blurb", "socialPostUrl", "docLinks", "images", "repoUrl", "videoUrl", "acceptRules",
    }


def test_a_complete_save_on_a_submitted_entry_says_it_is_live(home, repo_config, monkeypatch, tmp_path):
    write_home(home)
    document = tmp_path / "entry.md"
    document.write_text(DOCUMENT)
    live = view(status="SUBMITTED", missing=[], privateDraft=None, needs=[])
    recorder = Recorder({f"PUT /api/projects/{PROJECT_ID}/submission": (200, live)})
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "save", "--file", str(document))
    assert result.exit_code == 0, result.output
    assert "Saved and live" in result.output


def test_an_incomplete_save_on_a_submitted_entry_says_it_is_held(home, repo_config, monkeypatch, tmp_path):
    write_home(home)
    document = tmp_path / "entry.md"
    document.write_text(DOCUMENT)
    held = view(
        status="SUBMITTED", missing=["RULES"],
        privateDraft={"blurb": "x", "docLinks": [], "images": []},
    )
    recorder = Recorder({f"PUT /api/projects/{PROJECT_ID}/submission": (200, held)})
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "save", "--file", str(document))
    assert result.exit_code == 0, result.output
    assert "not live yet" in result.output
    assert "Confirm the work follows the event rules" in result.output


def test_withdraw_posts_the_revision_and_prints_the_visibility_note(home, repo_config, monkeypatch):
    write_home(home)
    note = "Withdrawing does not make the project private again; the project menu does that."
    recorder = Recorder({
        f"GET /api/projects/{PROJECT_ID}/submission": (200, view(status="SUBMITTED", contentRev="rev-3", missing=[])),
        f"POST /api/projects/{PROJECT_ID}/submission/withdraw": (200, view(status="WITHDRAWN", visibilityNote=note)),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "withdraw")
    assert result.exit_code == 0, result.output
    assert "Entry withdrawn." in result.output
    assert note in result.output
    assert recorder.body() == {"campaign": SLUG, "ifContentRev": "rev-3"}


def test_withdraw_with_nothing_filed_exits_one_without_posting(home, repo_config, monkeypatch):
    write_home(home)
    recorder = Recorder({f"GET /api/projects/{PROJECT_ID}/submission": (200, view())})
    result = run(recorder, monkeypatch, repo_config, "campaign", "submission", "withdraw")
    assert result.exit_code == 1, result.output
    assert "Nothing is filed to withdraw." in result.stderr
    assert all(call.method == "GET" for call in recorder.calls)


MINE_PROJECTS = {
    "projects": [
        {"id": 5, "title": "Other lab work", "event": None},
        {"id": PROJECT_ID, "title": "Aurora detector", "event": {
            "campaignSlug": SLUG, "status": "DRAFT", "closesAt": "2026-10-05T23:59:00.000Z",
        }},
    ],
    "eventProject": None,
    "total": 2,
}
MINE_TEAMS = {"teams": [{"id": 3, "slug": "aurora-crew", "name": "Aurora Crew", "myRole": "OWNER", "memberCount": 3}]}


def test_mine_shows_project_and_team_and_use_saves_the_selection(home, repo_config, monkeypatch):
    write_home(home, campaignContext={})
    recorder = Recorder({
        "GET /api/me/projects": (200, MINE_PROJECTS),
        "GET /api/teams": (200, MINE_TEAMS),
    })
    result = run(recorder, monkeypatch, repo_config, "campaign", "mine", "--campaign", SLUG, "--use")
    assert result.exit_code == 0, result.output
    assert "Aurora detector (#42)" in result.output
    assert "Aurora Crew (aurora-crew) · 3 people" in result.output
    teams_call = recorder.find("GET", "/api/teams")
    assert teams_call.url.params["scope"] == "mine" and teams_call.url.params["campaign"] == SLUG
    saved = json.loads((home / ".moment.json").read_text())
    assert saved["campaignContext"][f"{API_URL}|7|{PROJECT_ID}"] == SLUG


def test_mine_falls_back_to_the_event_project_campaign_and_prints_json(home, repo_config, monkeypatch):
    write_home(home, campaignContext={})
    payload = {**MINE_PROJECTS, "eventProject": MINE_PROJECTS["projects"][1]}
    recorder = Recorder({"GET /api/me/projects": (200, payload), "GET /api/teams": (200, {"teams": []})})
    monkeypatch.setattr(api_mod, "_TRANSPORT_OVERRIDE", recorder.transport())
    result = CliRunner().invoke(app, ["--config", str(repo_config), "--json", "campaign", "mine"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["campaign"] == SLUG and out["project"]["id"] == PROJECT_ID and out["team"] is None
