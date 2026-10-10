"""`moment campaign …` — one person's part in a campaign, from the terminal.

The same identity, the same rules, and the same record as the web: every
command here reads or writes through the campaign HTTP surface with the
person's CLI token, so what the terminal says and what the browser shows can
never disagree. The CLI adds no rules of its own — `missing[]` and the window
are computed server-side and rendered here.

Exit codes are a contract for scripts: 0 done, 1 the server refused (a 409 or
any other API error), 2 this command was used wrongly (not signed in, no
project, no campaign selected, no editor). Diagnostics go to stderr; `--json`
prints the raw server view or receipt on stdout.

Doctrine (DESIGN.md §6–7): a deadline is a DATE, never a countdown; an empty
state is one honest line plus one action.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from urllib.parse import quote
from rich.panel import Panel
from rich.table import Table

from moment_cli.api import ApiError, MomentApiClient
from moment_cli.config import Config, campaign_context_key, parse_iso, update_home_config
from moment_cli.display import ACCENT_STRONG, GLOW, console, kv, print_json

campaign_app = typer.Typer(
    help=(
        "Your part in a campaign — the event's record of your project. "
        "`mine` shows your project and team; `use` selects a campaign locally; "
        "`submission` edits, checks, and files the entry."
    ),
    no_args_is_help=True,
)
submission_app = typer.Typer(
    help="The entry itself: edit, check, submit, update, discard, withdraw.",
    no_args_is_help=True,
)
campaign_app.add_typer(submission_app, name="submission")

# The server owns the sentence for every reason an entry is blocked: each view
# carries `needs[]` ({code, sentence, situational}). This table is only the
# fallback for a server that predates `needs`, one line per `missing[]` code
# (mirrors MISSING_COPY and MISSING_SITUATIONAL in the web app).
NEED_SENTENCES: dict[str, str] = {
    "BLURB": "Write up what you built.",
    "INLINE_IMAGE_INVALID": "A picture in the write-up is not uploaded. Upload the file instead.",
    "SOCIAL_POST_URL": "Add the link to your social media post.",
    "SOCIAL_POST_URL_INVALID": "The post link has to be a public https:// address.",
    "REPO_URL": "Add the repository where the work lives.",
    "REPO_URL_INVALID": "The repository link has to be a public https:// address.",
    "VIDEO_URL_INVALID": "The demonstration video link has to be a public https:// address.",
    "DOC_LINK_INVALID": "Each document link needs a title and public https:// address.",
    "RULES": "Confirm the work follows the event rules.",
    "PROJECT_PRIVATE": "This project is private and cannot file a public entry.",
    "CAMPAIGN_CLOSED": "The event has closed; entries are final.",
    "RECOGNIZED": "This entry won an award and stays on record.",
    "WINDOW_CLOSED": "Submissions have closed.",
    "WINDOW_NOT_OPEN": "Submissions have not opened yet.",
    "NOT_ENLISTED": "This project is not in the event. Ask the organizers.",
}

#: Facts about the event, not chores for the team: they print on a "Window:"
#: line and never block `check` (mirrors MISSING_SITUATIONAL).
SITUATIONAL_CODES = frozenset(
    {"NOT_ENLISTED", "CAMPAIGN_CLOSED", "RECOGNIZED", "WINDOW_CLOSED", "WINDOW_NOT_OPEN"}
)

AUDIENCE_WORDS = {
    "public": "anyone with the link, and the event gallery",
    "lab": "signed-in lab members",
    "private": "this project's editors and the event stewards",
}

STATUS_WORDS = {"DRAFT": "Draft", "SUBMITTED": "Submitted", "WITHDRAWN": "Withdrawn"}


# --- Shared plumbing ---------------------------------------------------------


def _cli():
    """cli.py imports this module, so the dependency is resolved at call time."""
    from moment_cli import cli as cli_mod

    return cli_mod


def _state() -> dict:
    return _cli()._state


def _usage(message: str) -> "typer.Exit":
    """Wrong use (not signed in, nothing selected): exit 2, in the CLI's one error format."""
    _cli()._report_failure(message.rstrip("\n"))
    return typer.Exit(2)


def _refused(exc: Exception) -> "typer.Exit":
    """Any API refusal — 409 included — is exit 1, with the server's sentence and hint."""
    message, status, details, hint = _cli()._describe_error(exc)
    _cli()._report_failure(message, status, details, hint)
    return typer.Exit(1)


def _scope(*, hint: Optional[str] = None) -> tuple[Config, MomentApiClient]:
    """Config plus a client that acts as the PERSON, not as an agent key."""
    cfg = _cli()._resolve_cli_config(hint=hint, require_api_key=False)
    return cfg, MomentApiClient(cfg, human=True)


def _agent_scope(*, hint: Optional[str] = None) -> tuple[Config, MomentApiClient]:
    """Campaign-wide agent operations use the agent key, not the human token."""
    cfg = _cli()._resolve_cli_config(hint=hint)
    return cfg, MomentApiClient(cfg)


def _project_id(cfg: Config) -> int:
    if cfg.project_id is None:
        raise _usage(
            "No project selected. Pass -p <id-or-alias>, or run `moment project list` to "
            "see what this repository is attached to."
        )
    return cfg.project_id


def _resolve_campaign(cfg: Config, override: Optional[str]) -> str:
    """The campaign in play: an explicit flag, else this scope's selection."""
    if override and override.strip():
        return override.strip()
    key = campaign_context_key(cfg.api_url, _user_id(cfg), cfg.project_id)
    selected = cfg.campaign_context.get(key)
    if selected:
        return selected
    raise _usage(
        "No campaign selected for this project. Two ways forward:\n"
        "  moment campaign use <slug>            select it for this project\n"
        "  moment campaign status --campaign <slug>   name it once"
    )


def _user_id(cfg: Config) -> Any:
    """The person half of a scope key. A headless MOMENT_HUMAN_TOKEN run has no
    saved identity, so it shares one stable bucket rather than a wrong one."""
    return (cfg.human_user or {}).get("id") or "token"


def _fmt_instant(value: Any) -> Optional[str]:
    """An instant as a date and time, in UTC, as the server gave it."""
    parsed = parse_iso(value)
    if parsed is None:
        return value if isinstance(value, str) and value.strip() else None
    return parsed.strftime("%Y-%m-%d %H:%M")


def _deadline(view: dict) -> Optional[str]:
    """The date this person may still edit until: a steward grace outranks the close."""
    window = view.get("window") or {}
    return window.get("graceUntil") or window.get("closesAt")


def _status_phrase(view: dict) -> str:
    phrase = STATUS_WORDS.get(view.get("status") or "", view.get("status") or "unknown")
    if view.get("privateDraft"):
        phrase += " · unpublished changes"
    return phrase


def _needs(view: dict) -> list[dict]:
    """What still blocks the entry, as {code, sentence, situational}. The server's
    `needs[]` when it sent one; else the local sentence for each `missing[]` code."""
    server = view.get("needs")
    if isinstance(server, list):
        return [
            {
                "code": item.get("code"),
                "sentence": item["sentence"],
                "situational": bool(item.get("situational")),
            }
            for item in server
            if isinstance(item, dict) and isinstance(item.get("sentence"), str)
        ]
    return [
        {
            "code": code,
            "sentence": NEED_SENTENCES.get(code, code),
            "situational": code in SITUATIONAL_CODES,
        }
        for code in (view.get("missing") or [])
        if isinstance(code, str)
    ]


def _chores(view: dict) -> list[str]:
    return [item["sentence"] for item in _needs(view) if not item["situational"]]


def _window_sentences(view: dict) -> list[str]:
    return [item["sentence"].rstrip(".") for item in _needs(view) if item["situational"]]


def _print_needs(view: dict, *, window_label: str = "Window: ") -> None:
    chores = _chores(view)
    if chores:
        console.print("Needed:")
        for sentence in chores:
            console.print(f"  - {sentence}")
    else:
        console.print("Needed: nothing")
    sentences = _window_sentences(view)
    if sentences:
        console.print(f"{window_label}{'; '.join(sentences)}.")


def _print_refusal_detail(exc: ApiError) -> None:
    """A 409's own `missing[]`, when it sent one. Never invent a readiness line
    from a refusal that said nothing about content."""
    missing = exc.details.get("missing") if isinstance(exc.details, dict) else None
    if not isinstance(missing, list) or not missing:
        return
    _print_needs({"missing": missing})


def _next_command(view: dict) -> str:
    needed = bool(_chores(view))
    if view.get("status") == "SUBMITTED":
        if view.get("privateDraft") and not needed:
            return "moment campaign submission update"
        return "moment campaign submission edit"
    return "moment campaign submission edit" if needed else "moment campaign submission submit"


def _web_editor_url(cfg: Config, slug: str, project_id: int) -> str:
    return f"{cfg.api_url}/campaigns/{slug}/submit/{project_id}"


def _effective(view: dict) -> dict:
    """What the person is editing: their unpublished draft, else the record.

    The server computes `missing` over exactly this content, so the editor and
    the terminal answer "what still blocks publishing" the same way.
    """
    draft = view.get("privateDraft")
    if isinstance(draft, dict):
        return draft
    return view


# --- Reviewing before writing -------------------------------------------------
#
# submit / update / withdraw act on what the person last READ. The view's
# `contentRev` goes back as `ifContentRev`, so a file a teammate changed in
# between is refused with the server's own sentence instead of filed unseen.

STALE_SUFFIX = " after you reviewed it. Review again."
FIELD_NAMES = ("blurb", "socialPostUrl", "docLinks", "images", "repoUrl", "videoUrl", "acceptRules")


def _stable(value: Any) -> str:
    """The server's stableStringify (submissions-types.ts): sorted keys, JSON scalars."""
    if isinstance(value, list):
        return "[" + ",".join(_stable(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_stable(value[key])}" for key in sorted(value)
        ) + "}"
    return json.dumps(value, ensure_ascii=False)


def _field_rev(value: Any) -> str:
    """The server's submissionFieldRev: FNV-1a over UTF-16 code units, plus the length."""
    units = _stable(value).encode("utf-16-le")
    digest = 0x811C9DC5
    for index in range(0, len(units), 2):
        digest = ((digest ^ (units[index] | (units[index + 1] << 8))) * 0x01000193) & 0xFFFFFFFF
    return f"{digest:08x}{len(units) // 2:x}"


def _reviewed_fields(view: dict) -> dict[str, str]:
    """One revision per editable field of what was just read, so a refusal can say WHICH field moved."""
    content = _effective(view)
    values = {name: content.get(name) for name in FIELD_NAMES}
    values["docLinks"] = values["docLinks"] or []
    values["images"] = values["images"] or []
    values["acceptRules"] = (
        bool(content.get("acceptRules")) if "acceptRules" in content else bool(view.get("acceptedRulesAt"))
    )
    return {name: _field_rev(value) for name, value in values.items()}


def _read_view(client: MomentApiClient, project_id: int, slug: str) -> dict:
    try:
        return client.get_submission(project_id, slug)
    except ApiError as exc:
        raise _refused(exc)


def _rev_body(slug: str, view: dict) -> dict:
    rev = view.get("contentRev")
    return {"campaign": slug, **({"ifContentRev": rev} if isinstance(rev, str) and rev else {})}


def _write_refusal(exc: ApiError) -> "typer.Exit":
    """A refused submit/update/withdraw: the server's sentence, what it still
    needs, and — when a teammate changed the file since the read — the re-check."""
    if _state().get("json"):
        return _refused(exc)
    if exc.status == 409:
        sys.stderr.write(f"{exc}\n")
        if str(exc).endswith(STALE_SUFFIX):
            sys.stderr.write("Run `moment campaign submission check` to see the current entry, then try again.\n")
        _print_refusal_detail(exc)
        return typer.Exit(1)
    return _refused(exc)


# --- The submission document -------------------------------------------------

DOC_GUIDANCE = (
    "# Your entry. Fields above the second --- , the write-up below it.",
    "# docLinks: one `- title | url` per line. Lines starting with # are ignored.",
    "# version is the copy you started from — leave it alone so a teammate's",
    "# newer save is reported instead of being overwritten.",
)


def _render_document(view: dict) -> str:
    content = _effective(view)
    lines = ["---", *DOC_GUIDANCE]
    for key in ("repoUrl", "socialPostUrl", "videoUrl"):
        lines.append(f"{key}: {content.get(key) or ''}")
    accepted = (
        bool(content.get("acceptRules"))
        if "acceptRules" in content
        else bool(view.get("acceptedRulesAt"))
    )
    lines.append(f"acceptRules: {'true' if accepted else 'false'}")
    lines.append("docLinks:")
    for link in content.get("docLinks") or []:
        if isinstance(link, dict):
            lines.append(f"  - {link.get('title', '')} | {link.get('url', '')}")
    lines.append(f"version: {view.get('updatedAt') or ''}")
    lines.append("---")
    lines.append("")
    lines.append((content.get("blurb") or "").rstrip("\n"))
    return "\n".join(lines) + "\n"


def _parse_document(text: str) -> dict:
    """Front matter + write-up → the fields a PUT carries.

    Deliberately strict about the fence and forgiving inside it: a hand-edited
    file should fail loudly if it lost its structure, and quietly tolerate
    blank lines, comments, and stray quotes if it did not.
    """
    raw_lines = text.split("\n")
    start = 0
    while start < len(raw_lines) and not raw_lines[start].strip():
        start += 1
    if start >= len(raw_lines) or raw_lines[start].strip() != "---":
        raise _usage(
            "This file has no front matter. It must begin with a `---` line, the "
            "fields, another `---`, then the write-up."
        )

    fields: dict[str, Any] = {}
    doc_links: list[dict[str, str]] = []
    in_links = False
    index = start + 1
    closed = False
    while index < len(raw_lines):
        line = raw_lines[index]
        index += 1
        if line.strip() == "---":
            closed = True
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_links and stripped.startswith("-"):
            item = stripped[1:].strip()
            title, _, url = item.partition("|")
            title, url = title.strip(), url.strip()
            if url:
                doc_links.append({"title": title or url, "url": url})
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        in_links = key == "docLinks"
        if not in_links:
            fields[key] = value
    if not closed:
        raise _usage("This file's front matter is not closed by a second `---` line.")

    body = "\n".join(raw_lines[index:]).strip("\n")
    accept = fields.get("acceptRules", "").strip().lower()
    return {
        "blurb": body,
        "socialPostUrl": fields.get("socialPostUrl") or None,
        "repoUrl": fields.get("repoUrl") or None,
        "videoUrl": fields.get("videoUrl") or None,
        "docLinks": doc_links,
        "acceptRules": accept in ("true", "yes", "1", "on"),
        "version": fields.get("version") or None,
    }


def _document_path(project_id: int, slug: str) -> Path:
    """A stable per-person path (so a refused save can be retried), never in a
    shared temp directory where another user could plant a symlink."""
    folder = Path.home() / ".cache" / "moment"
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        folder.chmod(0o700)
    safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip(".-") or "campaign"
    return folder / f"moment-submission-{project_id}-{safe_slug}.md"


def _open_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise _usage(
            f"No editor configured. Set $VISUAL or $EDITOR, or edit the file by hand:\n"
            f"  {path}\n"
            f"then run: moment campaign submission save --file {path}"
        )
    try:
        code = subprocess.call([*shlex.split(editor), str(path)])
    except OSError as exc:
        raise _usage(f"Could not run {editor!r}: {exc}\nThe file is at {path}")
    if code != 0:
        raise _usage(f"{editor} exited with status {code}. The file is at {path}")


def _put_document(
    cfg: Config,
    client: MomentApiClient,
    project_id: int,
    slug: str,
    parsed: dict,
    *,
    keep_path: Optional[Path],
) -> dict:
    """PUT the parsed document. On a refusal the file survives, named."""
    body = {
        "blurb": parsed["blurb"],
        "socialPostUrl": parsed["socialPostUrl"],
        "repoUrl": parsed["repoUrl"],
        "videoUrl": parsed["videoUrl"],
        "docLinks": parsed["docLinks"],
        "acceptRules": parsed["acceptRules"],
        "ifUnchangedSince": parsed["version"],
    }
    try:
        return client.put_submission(project_id, slug, body)
    except ApiError as exc:
        if keep_path is not None and keep_path.exists():
            sys.stderr.write(f"Your edit is kept at {keep_path}\n")
            if exc.status == 409:
                sys.stderr.write(
                    "A teammate saved a newer version. Re-run "
                    "`moment campaign submission edit` to start from theirs.\n"
                )
        raise _refused(exc)


# --- Rendering ---------------------------------------------------------------


def _print_status(cfg: Config, view: dict, slug: str, project_id: int) -> None:
    campaign = view.get("campaign") or {}
    project = view.get("project") or {}
    console.print(f"[bold]{campaign.get('title') or slug} / {project.get('title') or f'project {project_id}'}[/]")
    console.print(f"Submission: {_status_phrase(view)}")
    deadline = _fmt_instant(_deadline(view))
    if deadline:
        console.print(f"Submit by: {deadline} (UTC)")
    console.print("")
    _print_needs(view)
    console.print("")
    console.print(f"Next: {_next_command(view)}")
    console.print(f"Web: {_web_editor_url(cfg, slug, project_id)}")


def _saved_line(saved: dict) -> str:
    """Say what a save did to the record, not just that it was kept."""
    if saved.get("status") != "SUBMITTED":
        return f"Saved to Moment · {_status_phrase(saved)}"
    if saved.get("privateDraft"):
        chores = _chores(saved)
        why = chores[0].rstrip(".") if chores else "run `moment campaign submission update` to put it live"
        return f"Saved, but not live yet: the entry on file did not change ({why})."
    return "Saved and live: the entry on file now has your changes."


def _print_receipt(cfg: Config, view: dict, *, verb: str) -> None:
    """The record, as the person will cite it. Honor-free: receiving an entry
    says nothing about whether it won."""
    campaign = view.get("campaign") or {}
    project = view.get("project") or {}
    snapshot = view.get("snapshot") or {}
    when = (
        view.get("submittedAt")
        if verb == "Received"
        else (view.get("refrozenAt") or snapshot.get("frozenAt") or view.get("submittedAt"))
    )
    members = [
        person.get("name")
        for person in (snapshot.get("members") or [])
        if isinstance(person, dict) and person.get("name")
    ]
    audience = AUDIENCE_WORDS.get(view.get("audience") or "", "this project's editors and the event stewards")
    lines = [
        kv("Project", project.get("title") or project.get("id")),
        kv("Campaign", campaign.get("title") or campaign.get("slug")),
        kv(verb, f"{_fmt_instant(when) or 'unknown'} (UTC)"),
        kv("Version", f"{_fmt_instant(snapshot.get('frozenAt')) or 'none'} (UTC)"),
        kv("Credited", ", ".join(members) if members else "nobody yet"),
        kv("Visible to", audience),
    ]
    deadline = _fmt_instant(_deadline(view))
    if deadline:
        lines.append(kv("Changes until", f"{deadline} (UTC)"))
    permalink = view.get("permalink")
    if permalink:
        lines.append(kv("Web", f"{cfg.api_url}{permalink}"))
    title = "Submission received" if verb == "Received" else "Submission updated"
    if _state().get("quiet"):
        console.print(title)
        for line in lines:
            console.print(line)
        return
    console.print(Panel("\n".join(lines), title=title, border_style=GLOW))


# --- Commands: the campaign ---------------------------------------------------


@campaign_app.command("list")
def campaign_list(
    remote: bool = typer.Option(False, "--remote", help="Use the agent-key campaign API."),
    after: Optional[str] = typer.Option(None, "--after", help="Remote only: continue from this cursor."),
) -> None:
    """The campaigns you can see on this server."""
    if after and not remote:
        raise _usage("--after requires --remote.")
    cfg, client = _agent_scope() if remote else _scope()
    try:
        payload = client.list_agent_campaigns(after=after) if remote else client.list_campaigns()
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(payload)
        return
    campaigns = payload.get("items") or payload.get("campaigns") or []
    if not campaigns:
        console.print("No campaigns you can see yet.")
        console.print(f"Web: {cfg.api_url}/campaigns")
        return
    table = Table(border_style=ACCENT_STRONG, box=None)
    table.add_column("Slug", style="bold", no_wrap=True)
    table.add_column("Title")
    table.add_column("Kind")
    table.add_column("Status", style="dim")
    for item in campaigns:
        table.add_row(
            str(item.get("slug") or ""),
            str(item.get("title") or ""),
            str(item.get("kind") or ""),
            str(item.get("status") or ""),
        )
    if remote and payload.get("nextCursor"):
        console.print(f"\nNext: moment campaign list --remote --after {payload['nextCursor']}")
    console.print(table)


@campaign_app.command("show")
def campaign_show(slug: str = typer.Argument(..., help="Campaign slug.")) -> None:
    """One campaign: what it is, when it takes entries, what an entry needs."""
    cfg = _cli()._resolve_cli_config(require_api_key=False)
    client = MomentApiClient(cfg) if cfg.api_key else MomentApiClient(cfg, human=True)
    try:
        payload = client.get_agent_campaign(slug) if cfg.api_key else client.get_campaign(slug)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(payload)
        return
    campaign = payload.get("campaign") or {}
    console.print(f"[bold]{campaign.get('title') or slug}[/]")
    if campaign.get("tagline"):
        console.print(campaign["tagline"])
    console.print("")
    console.print(kv("Kind", campaign.get("kind") or "unknown"))
    console.print(kv("Status", campaign.get("status") or "unknown"))
    opens = _fmt_instant(campaign.get("submissionsOpenAt"))
    closes = _fmt_instant(campaign.get("submissionsCloseAt"))
    console.print(kv("Entries open", f"{opens} (UTC)" if opens else "no date set"))
    console.print(kv("Entries close", f"{closes} (UTC)" if closes else "no date set"))
    requirements = (campaign.get("submissionRequirements") or "").strip()
    if requirements:
        console.print("")
        console.print("[bold]An entry needs[/]")
        shown = requirements.split("\n")[:10]
        for line in shown:
            console.print(line)
        if len(requirements.split("\n")) > len(shown):
            console.print("[dim]…[/]")


@campaign_app.command("use")
def campaign_use(
    slug: Optional[str] = typer.Argument(None, help="Campaign slug to select."),
    clear: bool = typer.Option(False, "--clear", help="Forget the selection for this project."),
) -> None:
    """Select a campaign for this project, on this machine.

    Local context only: it does not enroll the project. The selection is scoped
    to the server, the person, and the project, so two checkouts and two
    accounts never inherit each other's choice.
    """
    cfg, _client = _scope()
    project_id = _project_id(cfg)
    if not cfg.human_token:
        raise _usage("Sign in first: moment auth login")
    key = campaign_context_key(cfg.api_url, _user_id(cfg), project_id)
    context = dict(cfg.campaign_context)
    if clear:
        context.pop(key, None)
    else:
        if not slug or not slug.strip():
            raise _usage("Name a campaign: moment campaign use <slug> (or --clear).")
        context[key] = slug.strip()
    path = update_home_config({"campaignContext": context})
    user = (cfg.human_user or {}).get("name") or _user_id(cfg)
    if _state().get("json"):
        print_json({
            "apiUrl": cfg.api_url,
            "user": user,
            "projectId": project_id,
            "campaign": context.get(key),
            "savedTo": str(path),
        })
        return
    if clear:
        console.print(f"Scope: {cfg.api_url} · {user} · project {project_id} · no campaign")
        return
    console.print(
        f"Scope: {cfg.api_url} · {user} · project {project_id} · campaign {context[key]}"
    )


@campaign_app.command("status")
def campaign_status(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """Where your entry stands, and the one next thing to do."""
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    try:
        view = client.get_submission(project_id, slug)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(view)
        return
    _print_status(cfg, view, slug, project_id)


@campaign_app.command("mine")
def campaign_mine(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
    use: bool = typer.Option(False, "--use", help="Select this campaign and project for later commands."),
) -> None:
    """Your project and team in a campaign, in one place."""
    cfg, client = _scope()
    try:
        mine = client.get("/api/me/projects")
    except ApiError as exc:
        raise _refused(exc)
    projects = [item for item in (mine.get("projects") or []) if isinstance(item, dict)]

    slug = (campaign or "").strip() or None
    if not slug and cfg.project_id is not None:
        slug = cfg.campaign_context.get(campaign_context_key(cfg.api_url, _user_id(cfg), cfg.project_id))
    if not slug:
        lead = mine.get("eventProject")
        slug = ((lead or {}).get("event") or {}).get("campaignSlug") if isinstance(lead, dict) else None
    if not slug:
        raise _usage(
            "No campaign to look in. Name one: moment campaign mine --campaign <slug>\n"
            "  (moment campaign list shows what is open)"
        )

    project = next(
        (
            item for item in projects
            if isinstance(item.get("event"), dict) and item["event"].get("campaignSlug") == slug
        ),
        None,
    )
    try:
        teams_payload = client.get(f"/api/teams?scope=mine&campaign={quote(slug, safe='')}")
    except ApiError as exc:
        raise _refused(exc)
    teams = [item for item in (teams_payload.get("teams") or []) if isinstance(item, dict)]
    team = next((item for item in teams if item.get("myRole")), teams[0] if teams else None)

    saved_to = None
    if use and project:
        key = campaign_context_key(cfg.api_url, _user_id(cfg), project.get("id"))
        context = dict(cfg.campaign_context)
        context[key] = slug
        saved_to = str(update_home_config({"campaignContext": context}))

    if _state().get("json"):
        print_json({
            "campaign": slug,
            "project": project,
            "team": team,
            "teams": teams,
            **({"savedTo": saved_to} if saved_to else {}),
        })
        return
    console.print(f"[bold]{slug}[/]")
    if project:
        event = project.get("event") or {}
        console.print(kv("Project", f"{project.get('title')} (#{project.get('id')})"))
        console.print(kv("Entry", STATUS_WORDS.get(event.get("status") or "", "Not started")
                         + (" · unpublished changes" if event.get("hasUnpublishedChanges") else "")))
        closes = _fmt_instant(event.get("closesAt"))
        if closes:
            console.print(kv("Entries close", f"{closes} (UTC)"))
    else:
        console.print("You have no project in this campaign yet.")
        console.print(f"Web: {cfg.api_url}/campaigns/{slug}")
    if team:
        size = team.get("memberCount")
        console.print(kv("Team", f"{team.get('name')} ({team.get('slug')})" + (f" · {size} people" if size else "")))
    elif project:
        console.print("No team yet. Start or join one: moment team list --campaign " + slug)
    if use:
        if project:
            console.print(f"Selected campaign {slug} for project {project.get('id')}.")
            if cfg.project_id != project.get("id"):
                console.print(f"Use it with: moment -p {project.get('id')} campaign status")
        else:
            console.print("Nothing to select: no project of yours is in this campaign.")


@campaign_app.command("report")
def campaign_report(
    target_id: int = typer.Option(..., "--target-id", help="Campaign target ID."),
    value: float = typer.Option(..., "--value", help="Reported measurement value."),
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
    project: Optional[str] = typer.Option(None, "--project", help="Producing project (id or alias)."),
    note: Optional[str] = typer.Option(None, "--note", help="Measurement note."),
    evidence_url: Optional[str] = typer.Option(None, "--evidence-url", help="Evidence URL."),
) -> None:
    """Report evidence against a campaign target."""
    cfg, client = _agent_scope()
    slug = _resolve_campaign(cfg, campaign)
    project_id = _cli()._project_arg(project) if project else None
    payload = {
        "targetId": target_id,
        "value": value,
        **({"projectId": project_id} if project_id is not None else {}),
        **({"note": note} if note else {}),
        **({"evidenceUrl": evidence_url} if evidence_url else {}),
    }
    try:
        result = client.report_campaign_progress(slug, payload)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(result)
        return
    measurement = result.get("measurement") or result
    console.print(
        f"Measurement {value} filed against target #{target_id} of campaign "
        f"{slug} as {measurement.get('status', 'REPORTED')}."
    )


@campaign_app.command("targets")
def campaign_targets(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """List the targets in a campaign."""
    cfg, client = _agent_scope()
    slug = _resolve_campaign(cfg, campaign)
    try:
        result = client.get_campaign_context(slug)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(result)
        return
    structured = result.get("structured") or result
    targets = structured.get("targets") or structured.get("campaign", {}).get("targets") or []
    if not targets:
        console.print(f"No targets in campaign {slug}.")
        return
    for target in targets:
        console.print(
            f"#{target.get('id', '?')}  {target.get('name') or target.get('title') or 'Untitled target'}"
            f" · {target.get('metricKey') or target.get('metricName') or ''}"
        )


@campaign_app.command("enlist")
def campaign_enlist(
    commitment: str = typer.Option(..., "--commitment", help="What this project will advance."),
    target_ids: list[int] = typer.Option(None, "--target", help="Campaign target ID; repeatable."),
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
    project: Optional[str] = typer.Option(None, "--project", help="Project id or alias; defaults to the bound project."),
    role: Optional[str] = typer.Option(None, "--role", help="Project contribution role."),
    local_metrics: list[str] = typer.Option(None, "--local-metric", help="Local metric name; repeatable."),
) -> None:
    """Enlist a project in a campaign."""
    cfg, client = _agent_scope()
    slug = _resolve_campaign(cfg, campaign)
    selected_project = _cli()._project_arg(project) if project else _project_id(cfg)
    if not target_ids:
        raise _usage("Provide at least one --target.")
    if local_metrics and len(local_metrics) not in (0, len(target_ids)):
        raise _usage("Provide one --local-metric for each --target, or none.")
    payload = {
        "projectId": selected_project,
        "commitment": commitment,
        "targets": [
            {"targetId": target, **({"localMetricName": local_metrics[index]} if local_metrics else {})}
            for index, target in enumerate(target_ids)
        ],
        **({"role": role} if role else {}),
    }
    try:
        result = client.enlist_campaign_project(slug, payload)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(result)
        return
    console.print(f"Project #{selected_project} enlisted in campaign {slug}.")


@campaign_app.command("tasks")
def campaign_tasks(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
    status: Optional[str] = typer.Option(None, "--status"),
    difficulty: Optional[str] = typer.Option(None, "--difficulty"),
    good_first: Optional[bool] = typer.Option(None, "--good-first/--all-tasks"),
) -> None:
    """List tasks decomposed under a campaign."""
    cfg, client = _agent_scope()
    slug = _resolve_campaign(cfg, campaign)
    try:
        result = client.list_campaign_tasks(
            slug, status=status, difficulty=difficulty, good_first=good_first,
        )
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(result)
        return
    tasks = result.get("tasks") or []
    if not tasks:
        console.print(f"No tasks under campaign {slug}.")
        return
    for task in tasks:
        console.print(
            f"#{task.get('id', '?')}  {task.get('title') or 'Untitled task'}"
            f" · {task.get('status') or ''} · {task.get('difficulty') or ''}"
        )


# --- Commands: the entry ------------------------------------------------------


@submission_app.command("edit")
def submission_edit(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
    file: Optional[str] = typer.Option(None, "--file", help="Read this file instead of opening an editor."),
) -> None:
    """Edit the entry as a Markdown document, then save it to Moment."""
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)

    if file:
        path = Path(file).expanduser()
        if not path.exists():
            raise _usage(f"No such file: {path}")
        parsed = _parse_document(path.read_text())
        remove_on_success = False
    else:
        try:
            view = client.get_submission(project_id, slug)
        except ApiError as exc:
            raise _refused(exc)
        path = _document_path(project_id, slug)
        path.write_text(_render_document(view))
        _open_editor(path)
        parsed = _parse_document(path.read_text())
        remove_on_success = True

    saved = _put_document(cfg, client, project_id, slug, parsed, keep_path=path)
    if remove_on_success:
        try:
            path.unlink()
        except OSError:
            pass
    if _state().get("json"):
        print_json(saved)
        return
    console.print(_saved_line(saved))
    _print_needs(saved)
    if not _state().get("quiet"):
        console.print(f"Next: {_next_command(saved)}")


@submission_app.command("save")
def submission_save(
    file: str = typer.Option(..., "--file", help="The submission document to save."),
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """Save a submission document without opening an editor (for scripts).

    On a draft, a save only keeps your words; `submit` files the entry. On an
    entry that is already submitted, a save that leaves nothing missing goes
    live at once, and the record changes. A save that is still incomplete is
    held as unpublished changes: the record on file does not move until a
    complete save (or `update`). The same goes for a save by someone who may not
    change a filed entry, and for any save after the entry is locked.
    """
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    path = Path(file).expanduser()
    if not path.exists():
        raise _usage(f"No such file: {path}")
    parsed = _parse_document(path.read_text())
    saved = _put_document(cfg, client, project_id, slug, parsed, keep_path=path)
    if _state().get("json"):
        print_json(saved)
        return
    console.print(_saved_line(saved))
    _print_needs(saved)


@submission_app.command("check")
def submission_check(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """Say what is ready and what is still needed. Changes nothing.

    Exits 0 when nothing you can fix is missing, 1 while something is. A closed
    window or an unenlisted project prints on the Window line and does not
    change the exit code.
    """
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    try:
        view = client.get_submission(project_id, slug)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(view)
    else:
        _print_needs(view)
    if _chores(view):
        raise typer.Exit(1)


@submission_app.command("status")
def submission_status(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id or alias (default: the bound project)."),
    campaign: Optional[str] = typer.Option(None, "--campaign", "-c", help="Campaign slug (default: the campaign in use)."),
    json_out: bool = typer.Option(False, "--json", help="Print the raw status JSON."),
) -> None:
    """Read-only: is this entry ready to file, and what does it still need?

    A person's token reads the session route; an agent key reads the agent
    status route (H-51 B-L6). Same words as `check`; never writes.
    """
    cfg = _cli()._resolve_cli_config(project=project, require_api_key=False)
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    try:
        if cfg.human_token:
            view = MomentApiClient(cfg, human=True).get_submission(project_id, slug)
        elif cfg.api_key:
            view = MomentApiClient(cfg).get(
                f"/api/v1/agents/campaigns/{quote(slug, safe='')}/submissions/{project_id}/status"
            )
        else:
            raise _usage("Sign in first: `moment auth login` (or configure an agent key).")
    except ApiError as exc:
        raise _refused(exc)
    if json_out or _state().get("json"):
        print_json(view)
        return
    campaign_row = view.get("campaign") or {}
    project_row = view.get("project") or {}
    console.print(
        f"[bold]Entry · {project_row.get('title') or f'project {project_id}'} → {campaign_row.get('title') or slug}[/]"
    )
    status = view.get("status") or "NONE"
    if status == "NONE":
        console.print("Status:   Not started")
    elif status == "SUBMITTED":
        when = _fmt_instant(view.get("submittedAt"))
        console.print(f"Status:   {_status_phrase(view)}{f' {when}' if when else ''}")
    else:
        console.print(f"Status:   {_status_phrase(view)}")
    needed = _chores(view)
    ready_flag = view.get("ready") if isinstance(view.get("ready"), bool) else not needed
    if status == "SUBMITTED":
        console.print("Ready:    yes")
    elif ready_flag and not needed:
        console.print("Ready:    yes — nothing is missing; `moment campaign submission submit` files it.")
    else:
        console.print("Ready:    no")
    for sentence in needed:
        console.print(f"Needed:   {sentence}")
    sentences = _window_sentences(view)
    if sentences:
        console.print(f"Window:   {'; '.join(sentences)}")
    audience = AUDIENCE_WORDS.get(view.get("audience") or "", "this project's editors and the event stewards")
    console.print(f"Audience: {audience}")


@submission_app.command("submit")
def submission_submit(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """File the entry and print its receipt."""
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    current = _read_view(client, project_id, slug)
    body = {**_rev_body(slug, current), "reviewedFields": _reviewed_fields(current)}
    try:
        view = client.post(f"/api/projects/{project_id}/submission/submit", body)
    except ApiError as exc:
        raise _write_refusal(exc)
    if _state().get("json"):
        print_json(view)
        return
    _print_receipt(cfg, view, verb="Received")


@submission_app.command("update")
def submission_update(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """Put your held changes live on the entry on file.

    Only needed for changes that did not go live when saved (a save that was
    still incomplete). A complete save on a submitted entry is already live.
    """
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    # Read before publishing, so a DRAFT gets the sentence that actually helps
    # instead of the publish route's generic refusal.
    current = _read_view(client, project_id, slug)
    if current.get("status") == "DRAFT":
        _cli()._report_failure("Nothing is published yet — use submit.")
        raise typer.Exit(1)
    try:
        view = client.post(f"/api/projects/{project_id}/submission/publish", _rev_body(slug, current))
    except ApiError as exc:
        raise _write_refusal(exc)
    if _state().get("json"):
        print_json(view)
        return
    _print_receipt(cfg, view, verb="Updated")


@submission_app.command("discard")
def submission_discard(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """Throw away your unpublished changes; the entry on file is untouched."""
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    try:
        view = client.discard_submission_draft(project_id, slug)
    except ApiError as exc:
        raise _refused(exc)
    if _state().get("json"):
        print_json(view)
        return
    console.print("Unpublished changes discarded.")


@submission_app.command("withdraw")
def submission_withdraw(
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug (overrides the selection)."),
) -> None:
    """Withdraw the filed entry from the event.

    Withdrawing does not make the project private again. You can submit again
    while the window is open.
    """
    cfg, client = _scope()
    project_id = _project_id(cfg)
    slug = _resolve_campaign(cfg, campaign)
    current = _read_view(client, project_id, slug)
    if current.get("status") != "SUBMITTED":
        _cli()._report_failure("Nothing is filed to withdraw.")
        raise typer.Exit(1)
    try:
        view = client.post(f"/api/projects/{project_id}/submission/withdraw", _rev_body(slug, current))
    except ApiError as exc:
        raise _write_refusal(exc)
    if _state().get("json"):
        print_json(view)
        return
    console.print("Entry withdrawn.")
    note = view.get("visibilityNote")
    if note:
        console.print(note)
    console.print("Next: moment campaign submission submit")
