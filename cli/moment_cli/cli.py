"""Typer CLI application."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel

from moment_cli import __version__, hooks as hooks_mod
from moment_cli.api import ApiError, MomentApiClient
from moment_cli.config import resolve_config
from moment_cli.display import (
    GLOW,
    ROSE,
    SPARK,
    console,
    kv,
    moment_header,
    print_error,
    print_error_json,
    print_json,
    render_activity,
    render_auth_whoami,
    render_brief,
    render_bootstrap,
    render_contribution_result,
    render_create_project,
    render_gather,
    render_graph,
    render_github_sync_summary,
    render_impact,
    render_ingest_dry_run,
    render_ingest_result,
    render_ingest_watch_dry_run,
    render_ingest_watch_result,
    render_pr_status,
    render_project_context,
    render_propose_result,
    render_stats,
    render_submission,
    render_timeline,
    render_work_context,
    render_work_list,
    render_work_start,
)

app = typer.Typer(
    name="moment",
    help="Moment research agent CLI — work discovery, claiming, submission, and project contributions.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)

auth_app = typer.Typer(help="Authentication commands.", no_args_is_help=True)
work_app = typer.Typer(help="Work discovery, claiming, and submission.", no_args_is_help=True)
project_app = typer.Typer(help="Project context and contributions.", no_args_is_help=True)
stats_app = typer.Typer(help="Agent statistics.", no_args_is_help=True)
hooks_app = typer.Typer(help="Install ambient capture hooks across agent runtimes.", no_args_is_help=True)
github_app = typer.Typer(help="GitHub sync — pull PR/issue discussion into Moment context.", no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(work_app, name="work")
app.add_typer(project_app, name="project")
app.add_typer(stats_app, name="stats")
app.add_typer(hooks_app, name="hooks")
app.add_typer(github_app, name="github")

# --- Global state ---

_state: dict = {}


def _version_callback(value: bool) -> None:
    if value:
        console.print(moment_header())
        console.print(f"v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    api_key: Optional[str] = typer.Option(None, "--api-key", help="Override API key."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override API base URL."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to JSON config file."),
    project: Optional[int] = typer.Option(None, "--project", "-p", help="Project ID (overrides MOMENT_PROJECT_ID and .moment.json)."),
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress banners and extra text."),
    version: Optional[bool] = typer.Option(None, "--version", callback=_version_callback, is_eager=True, help="Show version."),
) -> None:
    """Moment research agent CLI."""
    _state["api_key"] = api_key
    _state["api_url"] = api_url
    _state["config"] = config
    _state["project"] = project
    _state["json"] = json_mode
    _state["quiet"] = quiet


def _resolve_cli_config(
    *,
    project: Optional[int] = None,
    require_api_key: bool = True,
):
    api_key = _state.get("api_key")
    if not require_api_key and not api_key:
        api_key = "dry-run"
    cfg = resolve_config(
        api_key=api_key,
        api_url=_state.get("api_url"),
        config_path=_state.get("config"),
        project_id=project if project is not None else _state.get("project"),
    )
    _state["_config"] = cfg
    return cfg


def _get_client(project: Optional[int] = None) -> MomentApiClient:
    cfg = _resolve_cli_config(project=project)
    return MomentApiClient(cfg)


def _resolve_project_id() -> int:
    """The effective project for binding-aware commands (orient/log/sync).

    Requires _get_client() to have run first (it populates _state['_config']).
    """
    cfg = _state.get("_config")
    pid = cfg.project_id if cfg else None
    if pid is None:
        raise typer.BadParameter(
            "No project bound. Pass --project N, set MOMENT_PROJECT_ID, "
            'or add "projectId" to .moment.json (run `moment init --project N`).'
        )
    return pid


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, ApiError):
        if _state.get("json"):
            print_error_json(str(exc), exc.status, exc.details)
        else:
            print_error(str(exc), exc.status)
    elif _state.get("json"):
        print_error_json(str(exc))
    else:
        print_error(str(exc))
    raise typer.Exit(1)


def _read_file(path: str) -> str:
    return Path(path).read_text().strip()


def _default_api_url(api_url: Optional[str] = None) -> str:
    return (
        api_url
        or _state.get("api_url")
        or os.environ.get("MOMENT_API_URL")
        or os.environ.get("MOMENT_MCP_API_URL")
        or "https://www.scientific-intelligence.com"
    ).rstrip("/")


def _build_session_payload(
    note: str,
    next_step: Optional[str] = None,
    task: Optional[int] = None,
    *,
    works_now: Optional[str] = None,
    in_progress: Optional[str] = None,
    blocked: Optional[str] = None,
    watch_out: Optional[str] = None,
) -> dict:
    """Build the POST body for the session-notes / close-out endpoint.

    A handoff patch is attached only when one of the handoff fields is given —
    a plain note never silently bumps the handoff version. When refreshing the
    handoff, --next is mirrored into handoff.nextStep so the brief stays in sync.
    """
    payload: dict = {"note": note}
    if next_step:
        payload["nextStep"] = next_step
    if task is not None:
        payload["taskId"] = task
    handoff: dict = {}
    for key, val in (
        ("worksNow", works_now),
        ("inProgress", in_progress),
        ("blocked", blocked),
        ("watchOut", watch_out),
    ):
        if val:
            handoff[key] = val
    if handoff:
        if next_step:
            handoff["nextStep"] = next_step
        payload["handoff"] = handoff
    return payload


# --- Auth ---


@auth_app.command("whoami")
def auth_whoami() -> None:
    """Show the authenticated agent identity and key fingerprint."""
    try:
        client = _get_client()
        result = client.get_stats()
        if _state.get("json"):
            print_json({
                "agent": result["agent"],
                "stats": result["stats"],
                "metadata": {
                    "apiUrl": _state["_config"].api_url,
                    "fingerprint": _state["_config"].fingerprint,
                    "source": "stats",
                },
            })
            return
        render_auth_whoami(result, _state["_config"])
    except Exception as exc:
        _handle_error(exc)


@auth_app.command("login")
def auth_login(
    key: Optional[str] = typer.Option(None, "--key", "-k", help="API key to save."),
) -> None:
    """Save API key to ~/.moment.json for future use."""
    if not key:
        key = typer.prompt("Enter your Moment API key")
    config_path = Path.home() / ".moment.json"
    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing["apiKey"] = key
    config_path.write_text(json.dumps(existing, indent=2) + "\n")
    if _state.get("json"):
        print_json({"saved": str(config_path)})
    else:
        console.print(f"[{_state.get('_accent', '#8ce99a')}]API key saved to {config_path}[/]")


# --- Work (tasks) ---


@work_app.command("list")
def work_list(
    project: Optional[int] = typer.Option(None, "--project", "-p", help="Filter by project ID."),
    type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by work type (e.g. CODE_CONTRIBUTION)."),
    difficulty: Optional[str] = typer.Option(None, "--difficulty", "-d", help="Filter by difficulty (EASY, MEDIUM, HARD)."),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max results."),
) -> None:
    """List available tasks for agents."""
    try:
        client = _get_client()
        result = client.list_work(project, type, difficulty, limit)
        if _state.get("json"):
            print_json(result)
            return
        render_work_list(result)
    except Exception as exc:
        _handle_error(exc)


@work_app.command("start")
def work_start(
    work_id: int = typer.Argument(help="Task ID to claim and start."),
) -> None:
    """Claim a task and fetch full context to begin."""
    try:
        client = _get_client()
        _ensure_claim(client, work_id)
        result = client.get_work_context(work_id)
        output = {
            "work": result.get("task") or result.get("contribution"),
            "project": result.get("project"),
            "github": result.get("github"),
            "claim": result.get("claim"),
            "nextStep": result.get("nextStep"),
        }
        if _state.get("json"):
            print_json(output)
            return
        render_work_start(output)
    except Exception as exc:
        _handle_error(exc)


@work_app.command("my-claims")
def work_my_claims() -> None:
    """List your active claims — tasks you're working on or have submitted."""
    try:
        client = _get_client()
        result = client.get_my_claims()
        if _state.get("json"):
            print_json(result)
            return
        claims = result.get("claims", [])
        if not claims:
            console.print(f"[{GLOW}]No active claims. Use `moment work list` to find work.[/]")
            return
        for claim in claims:
            status_color = "#8ce99a" if claim["status"] == "ACTIVE" else "#ffd43b"
            console.print(f"  [{status_color}]{claim['status']}[/] #{claim['taskId']} — {claim['title']}")
            console.print(f"         {claim['workType']} · {claim['credits']} credits · {claim['project']['title']}")
        console.print(f"\n  [dim]{result.get('active', 0)} active, {result.get('submitted', 0)} submitted[/dim]")
    except Exception as exc:
        _handle_error(exc)


@work_app.command("context")
def work_context(
    work_id: int = typer.Argument(help="Task ID."),
) -> None:
    """Fetch full task context."""
    try:
        client = _get_client()
        result = client.get_work_context(work_id)
        if _state.get("json"):
            print_json(result)
            return
        render_work_context(result)
    except Exception as exc:
        _handle_error(exc)


@work_app.command("submit")
def work_submit(
    work_id: int = typer.Argument(help="Task ID."),
    reasoning_file: str = typer.Option(..., "--reasoning-file", help="Path to reasoning markdown file."),
    output_file: Optional[str] = typer.Option(None, "--output-file", help="Path to output JSON file."),
    pr_url: Optional[str] = typer.Option(None, "--pr-url", help="URL of the pull request."),
    confidence: float = typer.Option(..., "--confidence", help="Confidence score (0.3-1.0)."),
    tokens_used: Optional[int] = typer.Option(None, "--tokens-used", help="Token count."),
) -> None:
    """Submit task results."""
    try:
        client = _get_client()
        reasoning = _read_file(reasoning_file)
        payload: dict = {"reasoning": reasoning, "confidenceScore": confidence}
        if tokens_used is not None:
            payload["tokensUsed"] = tokens_used
        if pr_url:
            payload["prUrl"] = pr_url
        if output_file:
            payload["output"] = json.loads(Path(output_file).read_text())

        result = client.submit_work(work_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        render_submission(result)
    except Exception as exc:
        _handle_error(exc)


@work_app.command("pr")
def work_pr(
    task_id: int = typer.Argument(help="Task ID."),
    pr_url: str = typer.Option(..., "--pr-url", help="Pull request URL."),
    reasoning_file: str = typer.Option(..., "--reasoning-file", help="Path to reasoning file (min 10 chars, required)."),
    confidence: Optional[float] = typer.Option(None, "--confidence", help="Optional confidence score (0-1)."),
) -> None:
    """Submit a PR for a task."""
    try:
        client = _get_client()
        reasoning = _read_file(reasoning_file)
        result = client.submit_pr(task_id, pr_url, reasoning, confidence)
        if _state.get("json"):
            print_json(result)
            return
        console.print(f"[{GLOW}]PR submitted for task #{task_id}.[/]")
    except Exception as exc:
        _handle_error(exc)


@work_app.command("pr-status")
def work_pr_status(
    task_id: int = typer.Argument(help="Task ID."),
) -> None:
    """Check PR status for a task."""
    try:
        client = _get_client()
        result = client.get_pr_status(task_id)
        if _state.get("json"):
            print_json(result)
            return
        render_pr_status(result)
    except Exception as exc:
        _handle_error(exc)


# --- Project ---


@project_app.command("context")
def project_context(
    project_id: int = typer.Argument(help="Project ID."),
) -> None:
    """Fetch full project context."""
    try:
        client = _get_client()
        result = client.get_project_context(project_id)
        if _state.get("json"):
            print_json(result)
            return
        render_project_context(result)
    except Exception as exc:
        _handle_error(exc)


@project_app.command("resources")
def project_resources(
    project_id: int = typer.Argument(help="Project ID."),
) -> None:
    """List a project's resources (the context manifest)."""
    try:
        print_json(_get_client().list_resources(project_id))
    except Exception as exc:
        _handle_error(exc)


@project_app.command("add-resource")
def project_add_resource(
    project_id: int = typer.Argument(help="Project ID."),
    uri: str = typer.Argument(help="Resource URI (repo / paper / dataset / doc)."),
    title: Optional[str] = typer.Option(None, "--title"),
    kind: Optional[str] = typer.Option(None, "--kind", help="DATASET|CODE_REPO|PAPER|... (auto-detected if omitted)."),
    summary: Optional[str] = typer.Option(None, "--summary"),
    role: Optional[str] = typer.Option(None, "--role", help="BACKGROUND|INPUT|OUTPUT|TOOL|REFERENCE."),
) -> None:
    """Attach a resolvable artifact to a project (dedups into the context graph)."""
    payload: dict = {"uri": uri}
    for key, val in (("title", title), ("kind", kind), ("summary", summary), ("role", role)):
        if val is not None:
            payload[key] = val
    try:
        print_json(_get_client().add_resource(project_id, payload))
    except Exception as exc:
        _handle_error(exc)


@project_app.command("read-resource")
def project_read_resource(
    project_id: int = typer.Argument(help="Project ID."),
    resource_id: int = typer.Argument(help="Resource ID (from `project resources`)."),
    fidelity: Optional[str] = typer.Option(None, "--fidelity", help="metadata|preview|text|summary (default summary)."),
    section: Optional[str] = typer.Option(None, "--section", help="Return just one heading of the text."),
) -> None:
    """Pull a resource's content into context."""
    try:
        print_json(_get_client().read_resource(project_id, resource_id, fidelity, section))
    except Exception as exc:
        _handle_error(exc)


@project_app.command("page")
def project_page(
    project_id: int = typer.Argument(help="Project ID."),
    title: str = typer.Option(..., "--title"),
    body: Optional[str] = typer.Option(None, "--body", help="Inline markdown body (pass '-' to read stdin). Alternative to --body-file."),
    body_file: Optional[str] = typer.Option(None, "--body-file", help="Path to a markdown file for the page body."),
    slug: Optional[str] = typer.Option(None, "--slug", help="Stable slug to update an existing page."),
    icon: Optional[str] = typer.Option(None, "--icon"),
    pinned: bool = typer.Option(False, "--pinned"),
    agent_read: bool = typer.Option(False, "--agent-read", help="Pull the full page into every agent's context."),
) -> None:
    """Add or update a freeform markdown page."""
    if bool(body) == bool(body_file):
        raise typer.BadParameter("Provide exactly one of --body or --body-file.")
    body_text = sys.stdin.read() if body == "-" else (body if body is not None else _read_file(body_file))  # type: ignore[arg-type]
    payload: dict = {"title": title, "body": body_text, "pinned": pinned, "agentRead": agent_read}
    if slug:
        payload["slug"] = slug
    if icon:
        payload["icon"] = icon
    try:
        print_json(_get_client().upsert_page(project_id, payload))
    except Exception as exc:
        _handle_error(exc)


@project_app.command("log")
def project_log(
    project_id: int = typer.Argument(help="Project ID."),
    note: str = typer.Option(..., "--note", help="What you did; the next step; what to watch."),
    next_step: Optional[str] = typer.Option(None, "--next-step"),
    task: Optional[int] = typer.Option(None, "--task", help="Task ID this close-out followed."),
) -> None:
    """Close out: log a session note (keeps the brief fresh)."""
    payload = _build_session_payload(note, next_step, task)
    try:
        print_json(_get_client().log_session(project_id, payload))
    except Exception as exc:
        _handle_error(exc)


@project_app.command("build")
def project_build(
    project_id: int = typer.Argument(help="Project ID."),
    source: Optional[str] = typer.Option(None, "--source", help="Repo URL (defaults to the project's githubRepo)."),
) -> None:
    """Auto-populate a project's context graph from a source repo."""
    try:
        print_json(_get_client().build_context(project_id, source))
    except Exception as exc:
        _handle_error(exc)


@app.command("find")
def find(
    query: str = typer.Argument(help="Natural-language query."),
    project: Optional[int] = typer.Option(None, "--project", "-p", help="Scope to one project."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
) -> None:
    """Semantically search the context graph across projects (find_context)."""
    try:
        print_json(_get_client().find_context(query, project, limit))
    except Exception as exc:
        _handle_error(exc)


# --- Context round-trip (binding-aware: project from --project/.moment.json) ---


@app.command("orient")
def orient(
    prompt: bool = typer.Option(False, "--prompt", help="Print the agent-ready promptContext verbatim."),
) -> None:
    """Print the project brief — orient before you edit (the spin-up ritual)."""
    try:
        client = _get_client()
        pid = _resolve_project_id()
        result = client.get_project_context(pid)
        if prompt:
            sys.stdout.write((result.get("promptContext") or "") + "\n")
            return
        if _state.get("json"):
            print_json(result)
            return
        render_brief(result, quiet=bool(_state.get("quiet")))
    except Exception as exc:
        _handle_error(exc)


@app.command("gather")
def gather(
    task: Optional[str] = typer.Argument(None, help="A question/topic to compile context for."),
    page: Optional[str] = typer.Option(None, "--page", help="Seed from a page slug (instead of / with a task)."),
    depth: Optional[int] = typer.Option(None, "--depth", help="Hops to expand (1-3)."),
    budget: Optional[int] = typer.Option(None, "--budget", help="Approx token budget for the bundle."),
) -> None:
    """Compile related context for a topic — the context compiler. Hand the output to a new session."""
    try:
        if not task and not page:
            raise typer.BadParameter("Provide a task (a question/topic) or --page.")
        client = _get_client()
        pid = _resolve_project_id()
        payload: dict = {}
        if task:
            payload["task"] = task
        if page:
            payload["page"] = page
        if depth is not None:
            payload["depth"] = depth
        if budget is not None:
            payload["tokenBudget"] = budget
        result = client.gather_context(pid, payload)
        if _state.get("json"):
            print_json(result)
            return
        render_gather(result, task or f"page:{page}")
    except Exception as exc:
        _handle_error(exc)


@app.command("backlinks")
def backlinks(
    page: str = typer.Argument(help="Page slug."),
) -> None:
    """Show a page's context-graph edges — backlinks + forward links."""
    try:
        client = _get_client()
        pid = _resolve_project_id()
        result = client.get_page_graph(pid, page)
        if _state.get("json"):
            print_json(result)
            return
        render_graph(result, page)
    except Exception as exc:
        _handle_error(exc)


@app.command("log")
def log(
    note: Optional[str] = typer.Option(None, "--note", help="What you did; the next step; what to watch."),
    note_file: Optional[str] = typer.Option(None, "--note-file", help="Read the note from a file."),
    next_step: Optional[str] = typer.Option(None, "--next", help="The next step for the following session."),
    task: Optional[int] = typer.Option(None, "--task", help="Task ID this close-out followed."),
    works_now: Optional[str] = typer.Option(None, "--works-now", help="Refresh handoff: what works now."),
    in_progress: Optional[str] = typer.Option(None, "--in-progress", help="Refresh handoff: in progress."),
    blocked: Optional[str] = typer.Option(None, "--blocked", help="Refresh handoff: blocked / broken."),
    watch_out: Optional[str] = typer.Option(None, "--watch-out", help="Refresh handoff: watch out for."),
    sync: bool = typer.Option(False, "--sync", help="Regenerate .continuity/ after logging."),
) -> None:
    """Close out a session: log a note and optionally refresh the handoff."""
    try:
        if bool(note) == bool(note_file):
            raise typer.BadParameter("Provide exactly one of --note or --note-file.")
        note_text = note if note else _read_file(note_file)  # type: ignore[arg-type]
        payload = _build_session_payload(
            note_text, next_step, task,
            works_now=works_now, in_progress=in_progress, blocked=blocked, watch_out=watch_out,
        )
        client = _get_client()
        pid = _resolve_project_id()
        result = client.log_session(pid, payload)
        if _state.get("json"):
            print_json(result)
        else:
            console.print(f"[{GLOW}]{result.get('message', 'Session note logged.')}[/]")
        if sync:
            _run_sync(pid, client, check=False, out=None)
    except Exception as exc:
        _handle_error(exc)


@app.command("sync")
def sync(
    check: bool = typer.Option(False, "--check", help="Render and diff vs disk; exit nonzero on drift (no writes)."),
    out: Optional[str] = typer.Option(None, "--out", help="Target dir (default: <repo>/.continuity)."),
) -> None:
    """Regenerate the .continuity/ mirror from Moment (Moment is the source of truth)."""
    try:
        client = _get_client()
        pid = _resolve_project_id()
        _run_sync(pid, client, check=check, out=out)
    except Exception as exc:
        _handle_error(exc)


def _continuity_dir(out: Optional[str]) -> Path:
    """The mirror lives beside the repo's .moment.json; fall back to cwd/.continuity."""
    if out:
        return Path(out)
    cfg = _state.get("_config")
    cp = cfg.config_path if cfg else None
    home_cfg = Path.home() / ".moment.json"
    if cp and Path(cp).resolve() != home_cfg.resolve():
        return Path(cp).parent / ".continuity"
    return Path.cwd() / ".continuity"


# Rendering lives server-side in render.ts (the single render-truth, exposed by
# GET /continuity-export). The CLI only writes files + preserves the local log
# history below the archive marker (which the stateless server can't see).
_ARCHIVE_MARKER = "<!-- moment:archive — preserved verbatim below; not managed by Moment sync -->"
_MANIFEST_NAME = ".moment-sync.json"


def _splice_log_archive(server_log: str, existing: Optional[str]) -> str:
    """Keep Moment's managed notes (above the marker) from the server; restore the
    pre-existing on-disk history (below the marker)."""
    if _ARCHIVE_MARKER not in server_log:
        return server_log
    above = server_log.split(_ARCHIVE_MARKER, 1)[0]
    if existing and _ARCHIVE_MARKER in existing:
        below = existing.split(_ARCHIVE_MARKER, 1)[1]
    elif existing and existing.strip():
        below = "\n" + existing.strip() + "\n"
    else:
        below = "\n"
    return above + _ARCHIVE_MARKER + below


def _normalize_text(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()


def _run_sync(pid: int, client: MomentApiClient, *, check: bool, out: Optional[str]) -> None:
    target = _continuity_dir(out)
    files = dict((client.get_continuity_export(pid) or {}).get("files") or {})

    # Preserve the on-disk log history below the archive marker (server is stateless).
    if "log.md" in files:
        log_path = target / "log.md"
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else None
        files["log.md"] = _splice_log_archive(files["log.md"], existing)

    if check:
        drift = []
        for name, content in files.items():
            if name == _MANIFEST_NAME:  # timestamped — not a drift signal
                continue
            p = target / name
            if not p.exists():
                drift.append((name, "missing"))
            elif _normalize_text(p.read_text(encoding="utf-8")) != _normalize_text(content):
                drift.append((name, "changed"))
        if _state.get("json"):
            print_json({"drift": [{"file": n, "status": s} for n, s in drift]})
        elif drift:
            console.print(f"[{ROSE}]Mirror drift in {len(drift)} file(s):[/]")
            for n, s in drift:
                console.print(f"  [{ROSE}]{s:>8}[/] {n}")
        else:
            console.print(f"[{GLOW}]Mirror is in sync — no drift.[/]")
        if drift:
            raise typer.Exit(1)
        return

    target.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (target / name).write_text(content, encoding="utf-8")
    written = sorted(files)
    if _state.get("json"):
        print_json({"project": pid, "dir": str(target), "files": written})
        return
    console.print(f"[{GLOW}]Synced project #{pid} → {target}[/] [dim]({len(written)} files)[/]")
    for n in written:
        console.print(f"  [dim]·[/] {n}")


# --- Ingest front door ---


INGEST_TEXT_PREVIEW_CHARS = 1200
INGEST_WATCH_TEXT_EXTENSIONS = {".txt", ".md", ".vtt", ".srt", ".json", ".pdf"}
INGEST_DEFAULT_POLL_SECONDS = 3.0


def _ingest_target_url(api_url: str, project_id: int) -> str:
    return f"{api_url.rstrip('/')}/api/v1/agents/projects/{project_id}/ingest"


def _looks_like_pdf(path: Path, raw: bytes) -> bool:
    return path.suffix.lower() == ".pdf" or raw[:5] == b"%PDF-"


def _extract_pdf_text(path: Path, raw: bytes) -> str:
    """Extract a PDF's text layer via pypdf (lazy import → clear install hint)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise typer.BadParameter(
            "Reading PDFs needs pypdf. Install it with: pip install pypdf"
        ) from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # malformed/encrypted PDF
        raise typer.BadParameter(f"Could not read PDF ({exc}): {path}") from exc
    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        raise typer.BadParameter(
            f"PDF has no extractable text (scanned/image-only?): {path}"
        )
    return text


def _read_ingest_file(path_arg: str) -> tuple[Path, str, bytes]:
    path = Path(path_arg).expanduser()
    if not path.exists():
        raise typer.BadParameter(f"File not found: {path_arg}")
    if not path.is_file():
        raise typer.BadParameter(f"Not a file: {path}")

    raw = path.read_bytes()
    if not raw or not raw.strip():
        raise typer.BadParameter(f"File is empty: {path}")
    # PDFs are the one binary the text connector understands: extract the text
    # layer, then treat it like any other transcript/note. externalId still
    # hashes the raw bytes, so re-ingesting the same PDF is idempotent.
    if _looks_like_pdf(path, raw):
        return path, _extract_pdf_text(path, raw), raw
    if b"\x00" in raw:
        raise typer.BadParameter(f"File appears to be binary: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise typer.BadParameter(f"File is not valid UTF-8 text: {path}") from exc
    if not text.strip():
        raise typer.BadParameter(f"File is empty: {path}")
    return path, text, raw


def _bound_repo_root_from_config(config_path: Optional[str]) -> Optional[Path]:
    if not config_path:
        return None
    path = Path(config_path).expanduser().resolve()
    home_config = (Path.home() / ".moment.json").resolve()
    if path == home_config or path.name != ".moment.json":
        return None
    return path.parent


def _external_id_for_file(path: Path, raw: bytes, bound_repo_root: Optional[Path]) -> str:
    resolved = path.resolve()
    # Deterministic but non-leaky: repo-bound files use their committed
    # repo-relative POSIX path; ad hoc files outside that repo use basename only.
    if bound_repo_root is not None:
        try:
            relpath = resolved.relative_to(bound_repo_root.resolve()).as_posix()
        except ValueError:
            relpath = resolved.name
    else:
        relpath = resolved.name
    digest = hashlib.sha256(raw).hexdigest()[:12]
    return f"{relpath}@{digest}"


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _parse_meta_json(meta_json: Optional[str]) -> Optional[dict]:
    if not meta_json:
        return None
    try:
        data = json.loads(meta_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--meta-json must be a JSON object: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter("--meta-json must be a JSON object.")
    return data


def _clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise typer.BadParameter("Option values cannot be empty.")
    return cleaned


def _build_ingest_envelope(
    *,
    file: str,
    source: str,
    title: Optional[str],
    participants: Optional[list[str]],
    occurred_at: Optional[str],
    uri: Optional[str],
    meta_json: Optional[str],
    bound_repo_root: Optional[Path],
) -> dict:
    """Build one ingest envelope."""
    path, text, raw = _read_ingest_file(file)
    cleaned_source = _clean_optional(source)
    cleaned_title = _clean_optional(title) if title is not None else path.name
    cleaned_participants = [p.strip() for p in (participants or []) if p.strip()]
    cleaned_uri = _clean_optional(uri) if uri is not None else None
    cleaned_at = _clean_optional(occurred_at) if occurred_at is not None else _mtime_iso(path)
    return {
        "source": cleaned_source,
        "externalId": _external_id_for_file(path, raw, bound_repo_root),
        "text": text,
        "title": cleaned_title,
        "participants": cleaned_participants or None,
        "occurredAt": cleaned_at,
        "uri": cleaned_uri,
        "meta": _parse_meta_json(meta_json),
    }


def _validate_watch_dir(path_arg: str) -> Path:
    path = Path(path_arg).expanduser()
    if not path.exists():
        raise typer.BadParameter(f"Watch dir not found: {path_arg}")
    if not path.is_dir():
        raise typer.BadParameter(f"Not a directory: {path}")
    return path.resolve()


def _is_hidden_watch_path(path: Path, watch_dir: Path) -> bool:
    try:
        parts = path.relative_to(watch_dir).parts
    except ValueError:
        parts = path.parts
    return any(part.startswith(".") for part in parts)


def _iter_watch_files(watch_dir: Path) -> list[Path]:
    files = []
    for path in watch_dir.rglob("*"):
        if _is_hidden_watch_path(path, watch_dir):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in INGEST_WATCH_TEXT_EXTENSIONS:
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(watch_dir).as_posix())


def _build_watch_envelope(
    path: Path,
    *,
    source: str,
    title: Optional[str],
    participants: Optional[list[str]],
    occurred_at: Optional[str],
    uri: Optional[str],
    meta_json: Optional[str],
    bound_repo_root: Optional[Path],
) -> Optional[dict]:
    try:
        return _build_ingest_envelope(
            file=str(path),
            source=source,
            title=title,
            participants=participants,
            occurred_at=occurred_at,
            uri=uri,
            meta_json=meta_json,
            bound_repo_root=bound_repo_root,
        )
    except typer.BadParameter as exc:
        message = str(exc)
        if message.startswith((
            "File not found:",
            "Not a file:",
            "File is empty:",
            "File appears to be binary:",
            "File is not valid UTF-8 text:",
        )):
            return None
        raise


def _ingest_file_signature(path: Path, envelope: dict) -> tuple[int, int, str]:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size, str(envelope.get("externalId") or ""))


def _collect_watch_envelopes(
    watch_dir: Path,
    *,
    source: str,
    title: Optional[str],
    participants: Optional[list[str]],
    occurred_at: Optional[str],
    uri: Optional[str],
    meta_json: Optional[str],
    bound_repo_root: Optional[Path],
) -> list[tuple[Path, dict, tuple[int, int, str]]]:
    envelopes = []
    for path in _iter_watch_files(watch_dir):
        envelope = _build_watch_envelope(
            path,
            source=source,
            title=title,
            participants=participants,
            occurred_at=occurred_at,
            uri=uri,
            meta_json=meta_json,
            bound_repo_root=bound_repo_root,
        )
        if envelope is None:
            continue
        try:
            signature = _ingest_file_signature(path, envelope)
        except OSError:
            continue
        envelopes.append((path, envelope, signature))
    return envelopes


def _run_ingest_watch(
    *,
    watch_dir: Path,
    client: MomentApiClient,
    project_id: int,
    target_url: str,
    source: str,
    title: Optional[str],
    participants: Optional[list[str]],
    occurred_at: Optional[str],
    uri: Optional[str],
    meta_json: Optional[str],
    bound_repo_root: Optional[Path],
    poll_seconds: float,
) -> None:
    if poll_seconds <= 0:
        raise typer.BadParameter("--poll-seconds must be greater than 0.")
    seen: dict[Path, tuple[int, int, str]] = {}
    if not _state.get("json"):
        console.print(
            Panel(
                "\n".join([
                    kv("Directory", watch_dir),
                    kv("Project", project_id),
                    kv("Target", target_url),
                    kv("Poll", f"{poll_seconds:g}s"),
                ]),
                title="Watching ingest",
                border_style=GLOW,
            )
        )
    try:
        while True:
            current = {}
            for path, envelope, signature in _collect_watch_envelopes(
                watch_dir,
                source=source,
                title=title,
                participants=participants,
                occurred_at=occurred_at,
                uri=uri,
                meta_json=meta_json,
                bound_repo_root=bound_repo_root,
            ):
                current[path] = signature
                if seen.get(path) == signature:
                    continue
                result = client.ingest(project_id, envelope)
                if _state.get("json"):
                    print_json({
                        "file": str(path),
                        "externalId": envelope.get("externalId"),
                        "result": result,
                    })
                else:
                    render_ingest_watch_result(str(path), envelope.get("externalId"), result)
            seen = current
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        if _state.get("json"):
            print_json({"stopped": True, "directory": str(watch_dir)})
        else:
            console.print(f"[{GLOW}]Stopped ingest watch.[/]")


def _preview_ingest_envelope(envelope: dict) -> dict:
    preview = dict(envelope)
    text = preview.get("text")
    if isinstance(text, str) and len(text) > INGEST_TEXT_PREVIEW_CHARS:
        omitted = len(text) - INGEST_TEXT_PREVIEW_CHARS
        preview["text"] = f"{text[:INGEST_TEXT_PREVIEW_CHARS]}\n... [truncated {omitted} chars]"
    return preview


@app.command("ingest")
def ingest(
    file: Optional[str] = typer.Argument(None, help="Text or PDF file to ingest (for example, a meeting transcript or a paper PDF)."),
    project: Optional[int] = typer.Option(None, "--project", "-p", help="Project ID (overrides MOMENT_PROJECT_ID and .moment.json)."),
    source: str = typer.Option("transcript", "--source", help="Envelope source (default: transcript)."),
    title: Optional[str] = typer.Option(None, "--title", help="Envelope title (default: file name)."),
    participant: list[str] = typer.Option(None, "--participant", help="Participant name (repeatable)."),
    at: Optional[str] = typer.Option(None, "--at", help="ISO 8601 occurrence time (default: file mtime)."),
    uri: Optional[str] = typer.Option(None, "--uri", help="Source URI for the envelope."),
    meta_json: Optional[str] = typer.Option(None, "--meta-json", help="JSON object for envelope meta."),
    watch: Optional[str] = typer.Option(None, "--watch", help="Recursively poll a directory for new/changed text or PDF files and ingest them."),
    poll_seconds: float = typer.Option(INGEST_DEFAULT_POLL_SECONDS, "--poll-seconds", help="Directory watch poll interval in seconds."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the envelope and target URL without POSTing."),
) -> None:
    """Ingest a local text or PDF file as candidate context for a bound project."""
    try:
        if (file is None) == (watch is None):
            raise typer.BadParameter("Pass exactly one of FILE or --watch DIR.")
        cfg = _resolve_cli_config(project=project, require_api_key=not dry_run)
        pid = _resolve_project_id()
        target_url = _ingest_target_url(cfg.api_url, pid)
        bound_repo_root = _bound_repo_root_from_config(cfg.config_path)

        if watch is not None:
            watch_dir = _validate_watch_dir(watch)
            if dry_run:
                items = _collect_watch_envelopes(
                    watch_dir,
                    source=source,
                    title=title,
                    participants=participant,
                    occurred_at=at,
                    uri=uri,
                    meta_json=meta_json,
                    bound_repo_root=bound_repo_root,
                )
                payload = {
                    "dryRun": True,
                    "targetUrl": target_url,
                    "projectId": pid,
                    "files": [
                        {
                            "file": str(path),
                            "externalId": envelope.get("externalId"),
                            "envelope": _preview_ingest_envelope(envelope),
                        }
                        for path, envelope, _signature in items
                    ],
                }
                if _state.get("json"):
                    print_json(payload)
                    return
                render_ingest_watch_dry_run(target_url, payload["files"])
                return

            client = MomentApiClient(cfg)
            _run_ingest_watch(
                watch_dir=watch_dir,
                client=client,
                project_id=pid,
                target_url=target_url,
                source=source,
                title=title,
                participants=participant,
                occurred_at=at,
                uri=uri,
                meta_json=meta_json,
                bound_repo_root=bound_repo_root,
                poll_seconds=poll_seconds,
            )
            return

        envelope = _build_ingest_envelope(
            file=file,
            source=source,
            title=title,
            participants=participant,
            occurred_at=at,
            uri=uri,
            meta_json=meta_json,
            bound_repo_root=bound_repo_root,
        )
        if dry_run:
            payload = {
                "dryRun": True,
                "targetUrl": target_url,
                "projectId": pid,
                "envelope": _preview_ingest_envelope(envelope),
            }
            if _state.get("json"):
                print_json(payload)
                return
            render_ingest_dry_run(target_url, payload["envelope"])
            return

        client = MomentApiClient(cfg)
        result = client.ingest(pid, envelope)
        if _state.get("json"):
            print_json(result)
            return
        render_ingest_result(result)
    except Exception as exc:
        _handle_error(exc)


# --- GitHub sync ---


GITHUB_TEXT_LIMIT = 24000
GITHUB_THREAD_TRUNCATED_SUFFIX = "\n…(thread truncated)"
GITHUB_BOT_LOGINS = {
    "dependabot",
    "dependabot[bot]",
    "renovate",
    "renovate[bot]",
    "github-actions",
    "github-actions[bot]",
    "codecov",
    "codecov[bot]",
}
GITHUB_PR_DETAIL_FIELDS = (
    "number,title,body,author,comments,reviews,mergeCommit,additions,deletions,"
    "changedFiles,mergedAt,closedAt,updatedAt,state,url,labels"
)
GITHUB_ISSUE_DETAIL_FIELDS = "number,title,body,author,comments,updatedAt,closedAt,state,url,labels"


def _gh(args: list, timeout: int = 30) -> str:
    import subprocess

    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"gh {' '.join(args)} failed")
    return out.stdout.strip()


def _canonical_github_repo(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    repo = value.strip().rstrip("/")
    repo = re.sub(r"^https?://github\.com/", "", repo, flags=re.IGNORECASE)
    repo = re.sub(r"^ssh://git@github\.com/", "", repo, flags=re.IGNORECASE)
    repo = re.sub(r"^git@github\.com:", "", repo, flags=re.IGNORECASE)
    repo = re.sub(r"^github\.com/", "", repo, flags=re.IGNORECASE)
    repo = repo.removesuffix(".git").strip("/")
    parts = [part for part in repo.split("/") if part]
    if len(parts) == 2 and all(parts):
        return f"{parts[0]}/{parts[1]}"
    return None


def _resolve_github_repo(repo: Optional[str]) -> str:
    if repo:
        resolved = _canonical_github_repo(repo)
        if resolved:
            return resolved
        raise typer.BadParameter("Repo must be owner/repo or a github.com URL.")

    import subprocess

    out = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise typer.BadParameter("No repo. Pass --repo owner/repo or run inside a repo with a github origin.")
    remote = out.stdout.strip()
    if "github.com" not in remote.lower():
        raise typer.BadParameter("No repo. Pass --repo owner/repo or run inside a repo with a github origin.")
    resolved = _canonical_github_repo(remote)
    if not resolved:
        raise typer.BadParameter("No repo. Pass --repo owner/repo or run inside a repo with a github origin.")
    return resolved


def _clean_github_login(value) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("login")
    if value is None:
        return None
    login = str(value).strip().lstrip("@")
    return login or None


def _github_author_login(item: dict) -> Optional[str]:
    return _clean_github_login((item or {}).get("author"))


def _is_bot_author(login: str) -> bool:
    cleaned = (_clean_github_login(login) or "").lower()
    return cleaned.endswith("[bot]") or cleaned in GITHUB_BOT_LOGINS


def _github_merge_sha(pr: dict) -> Optional[str]:
    commit = (pr or {}).get("mergeCommit")
    if isinstance(commit, dict):
        sha = commit.get("oid") or commit.get("sha")
    else:
        sha = commit
    return str(sha or (pr or {}).get("mergeCommitSha") or "").strip() or None


def _is_merged_pr(pr: dict) -> bool:
    state = str((pr or {}).get("state") or "").upper()
    return bool((pr or {}).get("mergedAt") or state == "MERGED" or _github_merge_sha(pr))


def _external_id_for_pr(repo: str, pr: dict) -> str:
    sha = _github_merge_sha(pr)
    marker = sha if _is_merged_pr(pr) and sha else ((pr or {}).get("updatedAt") or "")
    return f"{repo}#{(pr or {}).get('number')}@{marker}"


def _external_id_for_issue(repo: str, issue: dict) -> str:
    return f"{repo}#{(issue or {}).get('number')}@{(issue or {}).get('updatedAt') or ''}"


def _github_label_names(labels) -> list[str]:
    names = []
    for label in labels or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name is not None:
            names.append(str(name))
    return names


def _truncate_github_title(title) -> str:
    return str(title or "")[:300]


def _clean_github_markdown(value) -> str:
    """Mirror github.ts cleanBody: strip HTML comments + empty checklist lines,
    collapse 3+ newlines to a blank-line separator, trim. Kept byte-identical to
    the TS mapper (app/lib/connectors/github.ts) for cross-language parity."""
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = "\n".join(
        line
        for line in text.split("\n")
        if not re.match(r"^\s*[-*]\s*\[[ xX]\]\s*$", line)
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _latest_non_bot_review(pr: dict) -> Optional[dict]:
    """The last human review in `gh` array order (mirrors github.ts, which gh
    returns chronologically). The author of this review names the decision."""
    latest = None
    for review in (pr or {}).get("reviews") or []:
        login = _github_author_login(review)
        if not login or _is_bot_author(login):
            continue
        latest = review
    return latest


def _review_decision(pr: dict) -> Optional[str]:
    latest = _latest_non_bot_review(pr)
    if not latest:
        return (pr or {}).get("reviewDecision")
    return (pr or {}).get("reviewDecision") or latest.get("state")


def _github_participants(item: dict, *, include_reviews: bool) -> Optional[list[str]]:
    """Deduped logins in array order — author, then comment authors, then review
    authors (mirrors github.ts collectParticipants). Bots stripped, no leading @."""
    seen = set()
    participants: list[str] = []

    def add(login: Optional[str]) -> None:
        if not login or _is_bot_author(login):
            return
        if login in seen:
            return
        seen.add(login)
        participants.append(login)

    add(_github_author_login(item))
    for comment in (item or {}).get("comments") or []:
        add(_github_author_login(comment))
    if include_reviews:
        for review in (item or {}).get("reviews") or []:
            add(_github_author_login(review))
    return participants or None


def _github_discussion_lines(item: dict, *, include_reviews: bool) -> list[str]:
    """Reviews first (with their state tag), then issue-comments — both in `gh`
    array order, bots stripped, bodies raw-trimmed (NOT markdown-cleaned, matching
    github.ts discussionLines) so the blob is byte-identical across both mappers."""
    lines: list[str] = []
    if include_reviews:
        for review in (item or {}).get("reviews") or []:
            login = _github_author_login(review)
            if not login or _is_bot_author(login):
                continue
            body = str(review.get("body") or "").strip()
            state = str(review.get("state") or "").strip()
            tag = f"review: {state}" if state else "review"
            lines.append(f"@{login} ({tag}): {body}".rstrip())
    for comment in (item or {}).get("comments") or []:
        login = _github_author_login(comment)
        if not login or _is_bot_author(login):
            continue
        body = str(comment.get("body") or "").strip()
        lines.append(f"@{login} (comment): {body}".rstrip())
    return lines


def _limit_github_text(sections: list[str], discussion_lines: list[str]) -> str:
    """Assemble the non-discussion sections + a '## Discussion' block (entries
    separated by a blank line), then enforce GITHUB_TEXT_LIMIT by truncating ONLY
    the discussion — title/body/decision always survive. Mirrors github.ts
    assembleWithTruncation (omits the Discussion header entirely when empty)."""
    head = "\n\n".join(sections)
    if not discussion_lines:
        return head if len(head) <= GITHUB_TEXT_LIMIT else head[:GITHUB_TEXT_LIMIT]

    fixed = f"{head}\n\n## Discussion\n"
    full = fixed + "\n\n".join(discussion_lines)
    if len(full) <= GITHUB_TEXT_LIMIT:
        return full

    budget = GITHUB_TEXT_LIMIT - len(fixed) - len(GITHUB_THREAD_TRUNCATED_SUFFIX)
    if budget <= 0:
        capped = f"{head}{GITHUB_THREAD_TRUNCATED_SUFFIX}"
        return capped[:GITHUB_TEXT_LIMIT]

    acc = ""
    for line in discussion_lines:
        nxt = f"{acc}\n\n{line}" if acc else line
        if len(nxt) > budget:
            break
        acc = nxt
    if not acc:
        acc = discussion_lines[0][:budget]
    return f"{fixed}{acc}{GITHUB_THREAD_TRUNCATED_SUFFIX}"


def _pr_state_label(pr: dict) -> str:
    # Merged label keys off mergedAt presence (mirrors github.ts), so a not-yet-
    # merged PR never shows "merged" even if a stray sha/state is present.
    if (pr or {}).get("mergedAt"):
        sha = _github_merge_sha(pr)
        return f"merged ({sha[:7]})" if sha else "merged"
    state = str((pr or {}).get("state") or "").lower()
    if state == "closed" or (pr or {}).get("closedAt"):
        return "closed"
    return "open"


def _issue_state_label(issue: dict) -> str:
    state = str((issue or {}).get("state") or "").lower()
    if state == "closed" or (issue or {}).get("closedAt"):
        return "closed"
    return "open"


def _normalize_pr_narrative(repo: str, pr: dict) -> str:
    # Blob uses the full (trimmed) title; only the envelope `title` field is capped.
    title = str((pr or {}).get("title") or "").strip()
    author = _github_author_login(pr) or ""
    header = "\n".join([
        f"# PR #{(pr or {}).get('number')}: {title}",
        f"Repo: {repo} · State: {_pr_state_label(pr)} · Author: @{author}",
        f"Link: {(pr or {}).get('url') or ''}",
    ])
    sections = [
        header,
        f"## Description\n{_clean_github_markdown((pr or {}).get('body'))}",
    ]
    decision = str(_review_decision(pr) or "").strip()
    if decision:
        latest_review = _latest_non_bot_review(pr)
        reviewer = _github_author_login(latest_review) if latest_review else ""
        line = decision
        if reviewer:
            line += f" by @{reviewer}"
        line += "."
        merged_date = str((pr or {}).get("mergedAt") or "")[:10]
        if merged_date:
            line += f" Merged {merged_date}."
        sections.append(f"## Review decision\n{line}")
    return _limit_github_text(sections, _github_discussion_lines(pr, include_reviews=True))


def _normalize_issue_narrative(repo: str, issue: dict) -> str:
    title = str((issue or {}).get("title") or "").strip()
    author = _github_author_login(issue) or ""
    header = "\n".join([
        f"# Issue #{(issue or {}).get('number')}: {title}",
        f"Repo: {repo} · State: {_issue_state_label(issue)} · Author: @{author}",
        f"Link: {(issue or {}).get('url') or ''}",
    ])
    sections = [
        header,
        f"## Description\n{_clean_github_markdown((issue or {}).get('body'))}",
    ]
    return _limit_github_text(sections, _github_discussion_lines(issue, include_reviews=False))


def _build_pr_envelope(repo: str, pr: dict) -> dict:
    sha = _github_merge_sha(pr)
    return {
        "source": "github_pr",
        "externalId": _external_id_for_pr(repo, pr),
        "text": _normalize_pr_narrative(repo, pr),
        "title": _truncate_github_title((pr or {}).get("title")),
        "participants": _github_participants(pr, include_reviews=True),
        "occurredAt": (pr or {}).get("mergedAt") or (pr or {}).get("closedAt") or (pr or {}).get("updatedAt"),
        "uri": (pr or {}).get("url"),
        "meta": {
            "repo": repo,
            "number": (pr or {}).get("number"),
            "state": (pr or {}).get("state"),
            "merged": _is_merged_pr(pr),
            "mergeCommitSha": sha,
            "author": _github_author_login(pr),
            "additions": (pr or {}).get("additions"),
            "deletions": (pr or {}).get("deletions"),
            "changedFiles": (pr or {}).get("changedFiles"),
            "labels": _github_label_names((pr or {}).get("labels")),
            "reviewDecision": _review_decision(pr),
        },
    }


def _build_issue_envelope(repo: str, issue: dict) -> dict:
    return {
        "source": "github_issue",
        "externalId": _external_id_for_issue(repo, issue),
        "text": _normalize_issue_narrative(repo, issue),
        "title": _truncate_github_title((issue or {}).get("title")),
        "participants": _github_participants(issue, include_reviews=False),
        "occurredAt": (issue or {}).get("closedAt") or (issue or {}).get("updatedAt"),
        "uri": (issue or {}).get("url"),
        "meta": {
            "repo": repo,
            "number": (issue or {}).get("number"),
            "state": (issue or {}).get("state"),
            "author": _github_author_login(issue),
            "labels": _github_label_names((issue or {}).get("labels")),
        },
    }


def _gh_json(args: list, timeout: int = 30):
    raw = _gh(args, timeout=timeout)
    return json.loads(raw) if raw else None


def _parse_github_since(value: str) -> str:
    raw = (value or "").strip()
    match = re.fullmatch(r"(\d+)d", raw.lower())
    if match:
        days = int(match.group(1))
        return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    if not raw:
        raise typer.BadParameter("--since cannot be empty.")
    return raw


def _github_item_in_window(item: dict, since_iso: str, *keys: str) -> bool:
    since_date = since_iso[:10]
    for key in keys:
        value = (item or {}).get(key)
        if value and str(value)[:10] >= since_date:
            return True
    return False


def _github_pr_detail(repo: str, number) -> dict:
    return _gh_json([
        "pr",
        "view",
        str(number),
        "-R",
        repo,
        "--json",
        GITHUB_PR_DETAIL_FIELDS,
    ]) or {}


def _github_issue_detail(repo: str, number) -> dict:
    return _gh_json([
        "issue",
        "view",
        str(number),
        "-R",
        repo,
        "--json",
        GITHUB_ISSUE_DETAIL_FIELDS,
    ]) or {}


def _collect_github_pr_envelopes(repo: str, since_iso: str, include_open: bool) -> list[dict]:
    envelopes = []
    seen: set[str] = set()

    def add_pr(detail: dict) -> None:
        if not detail:
            return
        author = _github_author_login(detail)
        if author and _is_bot_author(author):
            return
        envelope = _build_pr_envelope(repo, detail)
        external_id = envelope.get("externalId")
        if external_id in seen:
            return
        seen.add(external_id)
        envelopes.append(envelope)

    merged = _gh_json([
        "pr",
        "list",
        "-R",
        repo,
        "--state",
        "merged",
        "--search",
        f"merged:>={since_iso}",
        "--json",
        "number,title,author,mergedAt,updatedAt,url,labels,state",
        "--limit",
        "100",
    ]) or []
    for summary in merged:
        author = _github_author_login(summary)
        if author and _is_bot_author(author):
            continue
        add_pr(_github_pr_detail(repo, summary.get("number")))

    closed = _gh_json([
        "pr",
        "list",
        "-R",
        repo,
        "--state",
        "closed",
        "--json",
        "number,title,author,updatedAt,url,labels,state",
        "--limit",
        "100",
    ]) or []
    for summary in closed:
        author = _github_author_login(summary)
        if author and _is_bot_author(author):
            continue
        detail = _github_pr_detail(repo, summary.get("number"))
        if detail.get("mergedAt"):
            continue
        if not _github_item_in_window(detail, since_iso, "closedAt", "updatedAt"):
            continue
        add_pr(detail)

    if include_open:
        opened = _gh_json([
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,author,updatedAt,url,labels,state",
            "--limit",
            "100",
        ]) or []
        for summary in opened:
            author = _github_author_login(summary)
            if author and _is_bot_author(author):
                continue
            detail = _github_pr_detail(repo, summary.get("number"))
            if not _github_item_in_window(detail, since_iso, "updatedAt"):
                continue
            add_pr(detail)

    return envelopes


def _collect_github_issue_envelopes(repo: str, since_iso: str, include_open: bool) -> list[dict]:
    envelopes = []
    seen: set[str] = set()

    def add_issue(detail: dict) -> None:
        if not detail:
            return
        author = _github_author_login(detail)
        if author and _is_bot_author(author):
            return
        envelope = _build_issue_envelope(repo, detail)
        external_id = envelope.get("externalId")
        if external_id in seen:
            return
        seen.add(external_id)
        envelopes.append(envelope)

    closed = _gh_json([
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        "closed",
        "--search",
        f"closed:>={since_iso}",
        "--json",
        "number,title,author,updatedAt,url,labels,state",
        "--limit",
        "100",
    ]) or []
    for summary in closed:
        author = _github_author_login(summary)
        if author and _is_bot_author(author):
            continue
        detail = _github_issue_detail(repo, summary.get("number"))
        if not _github_item_in_window(detail, since_iso, "closedAt", "updatedAt"):
            continue
        add_issue(detail)

    if include_open:
        opened = _gh_json([
            "issue",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,author,updatedAt,url,labels,state",
            "--limit",
            "100",
        ]) or []
        for summary in opened:
            author = _github_author_login(summary)
            if author and _is_bot_author(author):
                continue
            detail = _github_issue_detail(repo, summary.get("number"))
            if not _github_item_in_window(detail, since_iso, "updatedAt"):
                continue
            add_issue(detail)

    return envelopes


@github_app.command("sync")
def github_sync(
    repo: Optional[str] = typer.Option(None, "--repo", help="owner/repo (default: the git origin)."),
    project: Optional[int] = typer.Option(None, "--project", "-p", help="Project ID."),
    since: str = typer.Option("30d", "--since", help="Window, e.g. 14d / 2026-06-01 (default 30d)."),
    prs_only: bool = typer.Option(False, "--prs-only"),
    issues_only: bool = typer.Option(False, "--issues-only"),
    include_open: bool = typer.Option(False, "--include-open"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print envelopes + target URL; POST nothing."),
) -> None:
    """Sync recently closed GitHub PRs and issues into Moment ingest."""
    try:
        if prs_only and issues_only:
            raise typer.BadParameter("Use only one of --prs-only or --issues-only.")

        resolved_repo = _resolve_github_repo(repo)
        since_iso = _parse_github_since(since)
        cfg = _resolve_cli_config(project=project, require_api_key=not dry_run)
        pid = _resolve_project_id()
        target_url = _ingest_target_url(cfg.api_url, pid)

        envelopes = []
        if not issues_only:
            envelopes.extend(_collect_github_pr_envelopes(resolved_repo, since_iso, include_open))
        if not prs_only:
            envelopes.extend(_collect_github_issue_envelopes(resolved_repo, since_iso, include_open))

        if dry_run:
            payload = {
                "dryRun": True,
                "repo": resolved_repo,
                "projectId": pid,
                "targetUrl": target_url,
                "envelopes": envelopes,
            }
            if _state.get("json"):
                print_json(payload)
                return
            console.print(Panel(
                "\n".join([
                    kv("Repo", resolved_repo),
                    kv("Target", target_url),
                    kv("Items", len(envelopes)),
                    kv("Mode", "dry run"),
                ]),
                title="GitHub sync dry run",
                border_style=GLOW,
            ))
            for envelope in envelopes:
                render_ingest_dry_run(target_url, envelope)
            return

        client = MomentApiClient(cfg)
        results = []
        for envelope in envelopes:
            external_id = envelope.get("externalId")
            try:
                result = client.ingest(pid, envelope)
                results.append({
                    "externalId": external_id,
                    "status": result.get("status") or "ok",
                })
            except Exception as item_exc:
                results.append({
                    "externalId": external_id,
                    "status": "error",
                    "error": str(item_exc),
                })

        if _state.get("json"):
            print_json({
                "repo": resolved_repo,
                "projectId": pid,
                "targetUrl": target_url,
                "results": results,
            })
            return
        if not _state.get("quiet"):
            render_github_sync_summary(resolved_repo, results)
    except Exception as exc:
        _handle_error(exc)


# --- Ambient capture (the git-drift wedge) ---


def _git(args: list, cwd: Path, timeout: int = 10) -> str:
    import subprocess

    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout.strip()


def _repo_remote_url(root: Path) -> Optional[str]:
    try:
        return _git(["remote", "get-url", "origin"], root)
    except Exception:
        return None


def _slug_from_remote_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    tail = url.rstrip("/").removesuffix(".git")
    if ":" in tail and "://" not in tail:  # git@host:owner/repo
        tail = tail.split(":", 1)[1]
    parts = [p for p in tail.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return None


def _repo_slug(root: Path) -> str:
    """`owner/repo` from the origin remote, else the directory name."""
    return _slug_from_remote_url(_repo_remote_url(root)) or root.name


def _github_web_url(remote_url: Optional[str]) -> Optional[str]:
    slug = _slug_from_remote_url(remote_url)
    if not slug or not remote_url or "github.com" not in remote_url.lower():
        return None
    return f"https://github.com/{slug}"


def _repo_info(root: Path) -> dict:
    remote_url = _repo_remote_url(root)
    slug = _repo_slug(root)
    owner = None
    name = slug
    if "/" in slug:
        owner, name = slug.rsplit("/", 1)
    return {
        "root": str(root),
        "remote": remote_url,
        "slug": slug,
        "owner": owner,
        "name": name,
        "githubUrl": _github_web_url(remote_url),
    }


def _project_title_from_repo(name: str) -> str:
    title = re.sub(r"[-_]+", " ", name).strip()
    return title[:1].upper() + title[1:] if title else "Untitled Project"


def _repo_binding_path(root: Path) -> Path:
    return root / ".moment.json"


def _read_repo_binding(root: Path) -> dict:
    path = _repo_binding_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _bound_project_id(root: Path) -> Optional[int]:
    raw = _read_repo_binding(root).get("projectId")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _write_repo_binding(root: Path, project_id: int, api_url: str) -> tuple[Path, bool]:
    moment_json = _repo_binding_path(root)
    data = _read_repo_binding(root)
    had_key = isinstance(data.get("apiKey"), str)
    data.pop("apiKey", None)  # never store a secret in the committed binding file
    data["projectId"] = project_id
    data["apiUrl"] = api_url
    moment_json.write_text(json.dumps(data, indent=2) + "\n")
    return moment_json, had_key


def _find_readme(root: Path) -> Optional[Path]:
    for name in ("README.md", "README.mdx", "README.rst", "README.txt", "README"):
        path = root / name
        if path.exists() and path.is_file():
            return path
    return None


def _read_text_lossy(path: Path, max_chars: int = 60000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text[:max_chars]


def _description_from_readme(readme_text: Optional[str], title: str) -> str:
    if readme_text:
        chunks = re.split(r"\n\s*\n", readme_text)
        for chunk in chunks:
            lines = []
            for line in chunk.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#") or stripped.startswith("![") or stripped.startswith("[!"):
                    continue
                if stripped.startswith("```"):
                    lines = []
                    break
                lines.append(stripped)
            paragraph = " ".join(lines).strip()
            if len(paragraph) >= 20:
                return paragraph[:700]
    return f"Living research context for {title}."


RESOURCE_HOST_HINTS = (
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "doi.org",
    "github.com",
    "zenodo.org",
    "figshare.com",
    "osf.io",
)


def _obvious_resource_uris(repo: dict, readme_path: Optional[Path], readme_text: Optional[str]) -> list[str]:
    seen = set()
    uris: list[str] = []

    def add(uri: Optional[str]) -> None:
        if uri and uri not in seen:
            seen.add(uri)
            uris.append(uri)

    add(repo.get("githubUrl"))
    if repo.get("githubUrl") and readme_path:
        add(f"{repo['githubUrl']}/blob/HEAD/{readme_path.name}")
    if readme_text:
        for match in re.findall(r"https?://[^\s)>\"]+", readme_text):
            uri = match.rstrip(".,;]")
            lower = uri.lower()
            if any(host in lower for host in RESOURCE_HOST_HINTS):
                add(uri)
            if len(uris) >= 8:
                break
    return uris


def _readme_page_payload(readme_path: Optional[Path], readme_text: Optional[str]) -> Optional[dict]:
    if not readme_path or not readme_text:
        return None
    return {
        "title": readme_path.name,
        "body": readme_text,
        "slug": "readme",
        "agentRead": True,
    }


def _watermark_base_sha(source_ref: Optional[str]) -> Optional[str]:
    """The newest captured sha out of 'repo@a..b' / 'repo@a' (None for @worktree)."""
    if not source_ref or "@" not in source_ref:
        return None
    ref = source_ref.rsplit("@", 1)[1]
    if ref == "worktree":
        return None
    sha = ref.split("..")[-1].strip()
    return sha or None


def _collect_commits(root: Path, base_sha: Optional[str], since: Optional[str], max_commits: int) -> list:
    """Oldest-first commits after the watermark (or the newest max_commits)."""
    fmt = "--pretty=%H%x09%an%x09%s"
    if base_sha:
        try:
            _git(["cat-file", "-e", f"{base_sha}^{{commit}}"], root)
            raw = _git(["log", "--reverse", fmt, f"{base_sha}..HEAD"], root)
        except Exception:
            base_sha = None
            raw = ""
    if not base_sha:
        args = ["log", "--reverse", fmt, "-n", str(max_commits)]
        if since:
            args += ["--since", since]
        raw = _git(args, root)
    commits = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0]:
            commits.append({"sha": parts[0], "author": parts[1], "message": parts[2][:500]})
    return commits[: 200]


@app.command("capture")
def capture(
    apply: bool = typer.Option(False, "--apply", help="Write the capture (default: dry-run preview)."),
    hook: bool = typer.Option(False, "--hook", help="Session Stop-hook mode: implies --apply, source=AGENT_HOOK, never exits nonzero."),
    note: Optional[str] = typer.Option(None, "--note", help="Optional lead-in summary for the captured note."),
    next_step: Optional[str] = typer.Option(None, "--next", help="Optional next step."),
    max_commits: int = typer.Option(50, "--max-commits", help="Cap when no prior capture watermark exists."),
) -> None:
    """Capture git drift (commits + working-tree changes) into the project's session log.

    The work writes its own context: run it by hand, from a post-commit hook, or
    let the Claude Code Stop hook (`moment onboard` / `moment init`) run it at the end of a session.
    """
    try:
        client = _get_client()
        pid = _resolve_project_id()
        root = _repo_root()
        repo = _repo_slug(root)

        try:
            branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
        except Exception:
            branch = None

        watermark = client.get_capture_watermark(pid, repo)
        base_sha = _watermark_base_sha((watermark.get("lastCapture") or {}).get("sourceRef"))
        since = watermark.get("lastSessionNoteAt") if not base_sha else None
        commits = _collect_commits(root, base_sha, since, max_commits)

        diffstat = None
        if base_sha and commits:
            try:
                diffstat = _git(["diff", "--shortstat", f"{base_sha}..HEAD"], root) or None
            except Exception:
                diffstat = None

        uncommitted = None
        try:
            dirty = [ln for ln in _git(["status", "--porcelain"], root).splitlines() if ln.strip()]
            if dirty:
                uncommitted = {"files": len(dirty)}
                ds = _git(["diff", "--shortstat"], root)
                if ds:
                    uncommitted["diffstat"] = ds[:300]
        except Exception:
            pass

        if not commits and not uncommitted:
            if _state.get("json"):
                print_json({"skipped": True, "reason": "Nothing to capture."})
            elif not _state.get("quiet"):
                console.print(f"[{GLOW}]Nothing to capture — no commits or working-tree drift since the last note.[/]")
            return

        payload: dict = {"repo": repo, "commits": commits, "source": "AGENT_HOOK" if hook else "GIT_CAPTURE"}
        if branch:
            payload["branch"] = branch
        if diffstat:
            payload["diffstat"] = diffstat[:300]
        if uncommitted:
            payload["uncommitted"] = uncommitted
        if note:
            payload["note"] = note
        if next_step:
            payload["nextStep"] = next_step

        if not (apply or hook):
            if _state.get("json"):
                print_json({"dryRun": True, "project": pid, "payload": payload})
                return
            console.print(f"[bold]Would capture to project #{pid}[/bold] [dim]({repo}{'@' + branch if branch else ''})[/dim]")
            for c in commits[:20]:
                console.print(f"  [dim]·[/] {c['sha'][:7]} {c['message']}")
            if len(commits) > 20:
                console.print(f"  [dim]· … and {len(commits) - 20} more[/dim]")
            if uncommitted:
                console.print(f"  [dim]· uncommitted: {uncommitted['files']} file(s)[/dim]")
            console.print(f"[{GLOW}]Dry run — re-run with --apply to write.[/]")
            return

        result = client.post_capture(pid, payload)
        if _state.get("json"):
            print_json(result)
        elif not _state.get("quiet"):
            if result.get("skipped"):
                console.print(f"[dim]{result.get('reason', 'Skipped.')}[/dim]")
            else:
                console.print(f"[{GLOW}]{result.get('message', 'Captured.')}[/]")
    except Exception as exc:
        if hook:
            # A Stop hook must never break the session it closes out.
            print(f"moment capture (hook): {exc}", file=sys.stderr)
            return
        _handle_error(exc)


CLAUDE_START = "<!-- moment:autostart:start -->"
CLAUDE_END = "<!-- moment:autostart:end -->"


@app.command("init")
def init(
    project: int = typer.Option(..., "--project", "-p", help="Project ID to bind this repo to."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="API URL to bind (default: resolved / localhost)."),
    no_claude: bool = typer.Option(False, "--no-claude", help="Don't touch CLAUDE.md."),
    no_hooks: bool = typer.Option(False, "--no-hooks", help="Don't install capture hooks for detected runtimes (ambient capture)."),
) -> None:
    """Bind this repo to a Moment project. For guided first-run setup, use `moment onboard`."""
    root = _repo_root()
    url = _default_api_url(api_url)
    moment_json, had_key = _write_repo_binding(root, project, url)
    console.print(f"[{GLOW}]Bound[/] {moment_json} → project #{project} @ {url}")
    if had_key:
        console.print(
            f"[{ROSE}]Removed apiKey from {moment_json.name}[/] — keep keys in "
            "~/.moment.json or $MOMENT_API_KEY (this file is committed)."
        )

    if not no_claude:
        path = root / "CLAUDE.md"
        _upsert_claude_block(path, _claude_block(project))
        console.print(f"[{GLOW}]Updated[/] {path} [dim](autostart block)[/]")

    if not no_hooks:
        _install_capture_hooks()

    console.print(
        "[dim]Next: `moment orient` to read the brief · `moment sync` to mirror it to .continuity/ · `moment onboard` for guided setup[/]"
    )


def _install_capture_hooks() -> list[dict]:
    """Install ambient capture for every detected runtime; log per-runtime in rich style.

    Returns the per-runtime result dicts (also used to surface the summary). Each entry:
    {runtime, label, detected, status, ...}. Best-effort: never raises.
    """
    results: list[dict] = []
    for rt in hooks_mod.registry():
        detected = rt.detect()
        if not detected:
            results.append({"runtime": rt.key, "label": rt.label, "detected": False, "status": "not-detected"})
            continue
        outcome = rt.install()
        entry = {"runtime": rt.key, "label": rt.label, "detected": True}
        entry.update(outcome)
        results.append(entry)
        status = outcome.get("status", "failed")
        if status in ("added", "updated"):
            detail = rt.describe()
            chained = outcome.get("chained")
            if chained:
                detail += f" (chained → {' '.join(str(c) for c in chained)})"
            console.print(f"[{GLOW}]Installed[/] {rt.label} capture — [dim]{detail}[/]")
        elif status == "present":
            console.print(f"[dim]{rt.label} capture already installed.[/dim]")
        else:
            console.print(f"[{ROSE}]Could not install {rt.label} capture — install by hand.[/]")
    return results


def _repo_root() -> Path:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except Exception:
        pass
    return Path.cwd()


def _claude_block(project_id: int) -> str:
    return (
        f"{CLAUDE_START}\n"
        f"## Moment context (project #{project_id})\n\n"
        f"This repo is **Moment project #{project_id}**. Moment is the single source of truth "
        f"for context; `.continuity/` is a generated, read-only mirror — never hand-edit it.\n\n"
        f"**Orient first** (before editing): run `moment orient` (or, via MCP, "
        f"`get_project_context({project_id})`). Read the handoff → next step → watch-out.\n\n"
        f'**Close out** (end of session): `moment log --note "…" --next "…"` — refresh the '
        f"handoff with `--works-now/--in-progress/--blocked/--watch-out` as needed, then "
        f"`moment sync` to regenerate `.continuity/` (or `moment log --sync` to do both).\n\n"
        f"**Ambient capture**: a Claude Code Stop hook runs `moment capture --hook` so git "
        f"drift is logged automatically — a deliberate `moment log` close-out is still better "
        f"when you have judgment to record.\n\n"
        f"Binding lives in `.moment.json` (`projectId`, `apiUrl`). The API key comes from the "
        f"environment / `.mcp.json` — never commit a key in `.moment.json`.\n"
        f"{CLAUDE_END}"
    )


STOP_HOOK_COMMAND = "moment capture --hook"


# The Claude-Code hook installer now lives in moment_cli.hooks (a runtime registry that
# also covers Codex). These thin shims preserve the original names used by `init` /
# `onboard` / the test suite, and delegate to the generalized installer.


def _claude_settings_path() -> Path:
    return hooks_mod.claude_settings_path()


def _stop_hook_present(data: dict) -> bool:
    return hooks_mod.claude_hooks_present(data)


def _stop_hook_status() -> str:
    """Return present|absent|failed (treats not-detected as absent for back-compat)."""
    status = hooks_mod.claude_status()
    return "absent" if status == "not-detected" else status


def _upsert_stop_hook() -> str:
    """Install Stop + SessionEnd capture hooks. Returns added|updated|present|failed.

    Idempotent + non-destructive; see moment_cli.hooks.install_claude.
    """
    return hooks_mod.install_claude()


# --- Generalized capture-hook installer (runtime registry) ---


_HOOK_STATUS_COLORS = {
    "added": GLOW,
    "updated": GLOW,
    "present": GLOW,
    "absent": SPARK,
    "failed": ROSE,
    "not-detected": "dim",
}


def _hook_status_label(status: str) -> str:
    color = _HOOK_STATUS_COLORS.get(status, "dim")
    return f"[{color}]{status}[/]"


def _parse_runtime_filter(runtime: Optional[str]) -> Optional[list[str]]:
    if not runtime:
        return None
    keys = [k.strip().lower() for k in runtime.replace(" ", ",").split(",") if k.strip()]
    return keys or None


@hooks_app.command("install")
def hooks_install(
    runtime: Optional[str] = typer.Option(
        None, "--runtime", "-r",
        help="Comma-separated subset to target (claude,codex). Default: every detected runtime.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be installed without writing."),
    all_runtimes: bool = typer.Option(
        False, "--all", help="Install for every known runtime even if not detected on this machine.",
    ),
) -> None:
    """Detect agent runtimes and install the ambient-capture hook for each, idempotently."""
    try:
        only = _parse_runtime_filter(runtime)
        try:
            runtimes = hooks_mod.select_runtimes(only)
        except ValueError as exc:
            raise typer.BadParameter(str(exc))

        results: list[dict] = []
        for rt in runtimes:
            detected = rt.detect()
            if not detected and not all_runtimes and only is None:
                results.append({"runtime": rt.key, "label": rt.label, "detected": False,
                                "action": "not-detected", "describe": rt.describe()})
                continue
            if dry_run:
                results.append({"runtime": rt.key, "label": rt.label, "detected": detected,
                                "action": "would-install" if (detected or all_runtimes or only) else "not-detected",
                                "describe": rt.describe()})
                continue
            outcome = rt.install()
            entry = {"runtime": rt.key, "label": rt.label, "detected": detected,
                     "describe": rt.describe()}
            entry.update(outcome)
            entry.setdefault("action", outcome.get("status", "failed"))
            results.append(entry)

        if _state.get("json"):
            print_json({"dryRun": dry_run, "results": results})
            return
        _render_hooks_install(results, dry_run=dry_run)
    except Exception as exc:
        _handle_error(exc)


@hooks_app.command("status")
def hooks_status() -> None:
    """Show, per runtime, whether ambient capture is installed."""
    try:
        results = []
        for rt in hooks_mod.registry():
            results.append({
                "runtime": rt.key,
                "label": rt.label,
                "detected": rt.detect(),
                "status": rt.status(),
                "describe": rt.describe(),
            })
        if _state.get("json"):
            print_json({"results": results})
            return
        lines = []
        for r in results:
            lines.append(
                kv(r["label"], f"{_hook_status_label(r['status'])} [dim]· {r['describe']}[/]")
            )
        console.print(Panel("\n".join(lines), title="Capture hooks", border_style=GLOW))
        console.print(
            "[dim]Run `moment hooks install` to (re)install · capture only fires in a repo bound "
            "with `moment init`/`moment onboard`.[/]"
        )
    except Exception as exc:
        _handle_error(exc)


def _render_hooks_install(results: list[dict], *, dry_run: bool) -> None:
    title = "Capture hooks — dry run" if dry_run else "Capture hooks installed"
    lines = []
    for r in results:
        action = r.get("action") or r.get("status", "?")
        detail = r["describe"]
        chained = r.get("chained")
        if chained:
            detail += f" [dim](chained → {' '.join(str(c) for c in chained)})[/]"
        lines.append(kv(r["label"], f"{_hook_status_label(action)} [dim]· {detail}[/]"))
    console.print(Panel("\n".join(lines), title=title, border_style=GLOW))
    if dry_run:
        console.print("[dim]Dry run — no files were written. Re-run without --dry-run to install.[/]")
    else:
        console.print(
            "[dim]Capture records git drift only (never your chat), and only in repos bound with "
            "`moment init` / `moment onboard`.[/]"
        )


def _upsert_claude_block(path: Path, block: str) -> None:
    existing = path.read_text() if path.exists() else ""
    if CLAUDE_START in existing and CLAUDE_END in existing:
        pre = existing.split(CLAUDE_START, 1)[0].rstrip()
        post = existing.split(CLAUDE_END, 1)[1].lstrip()
        merged = pre + ("\n\n" if pre else "") + block + ("\n\n" + post if post else "")
    elif existing.strip():
        merged = existing.rstrip() + "\n\n" + block
    else:
        merged = block
    path.write_text(merged.rstrip() + "\n")


def _project_id_from_bootstrap(result: dict) -> int:
    project = result.get("project") if isinstance(result, dict) else None
    raw = project.get("id") if isinstance(project, dict) else None
    if isinstance(raw, bool) or raw is None:
        raise RuntimeError("Project bootstrap response did not include project.id")
    return int(raw)


def _seed_initial_context(
    client: MomentApiClient,
    project_id: int,
    readme_page: Optional[dict],
    resources: list[str],
) -> list[dict]:
    """Idempotently attach local first-run context to an already-bound project."""
    results: list[dict] = []
    if readme_page:
        try:
            page = client.upsert_page(project_id, readme_page)
            results.append({"kind": "page", "target": "readme", "ok": True, "result": page})
        except Exception as exc:
            results.append({"kind": "page", "target": "readme", "ok": False, "error": str(exc)})

    for uri in resources:
        try:
            resource = client.add_resource(project_id, {"uri": uri})
            results.append({"kind": "resource", "target": uri, "ok": True, "result": resource})
        except Exception as exc:
            results.append({"kind": "resource", "target": uri, "ok": False, "error": str(exc)})
    return results


def _onboard_payload(
    *,
    title: str,
    description: str,
    repo: dict,
    readme_page: Optional[dict],
    resources: list[str],
    domain: Optional[str],
    visibility: Optional[str],
) -> dict:
    payload: dict = {"title": title, "description": description}
    if domain:
        payload["domain"] = domain.upper()
    if visibility:
        payload["visibility"] = visibility.upper()
    if repo.get("githubUrl"):
        payload["githubUrl"] = repo["githubUrl"]
    if readme_page:
        payload["page"] = readme_page
    if resources:
        payload["resources"] = [{"uri": uri} for uri in resources]
    payload["handoff"] = {
        "worksNow": "Repository is bound to Moment; the README has been imported as initial context.",
        "nextStep": "Run `moment orient` at the start of each session and let ambient capture record session deltas.",
    }
    payload["note"] = "Initial Moment onboarding: bound repo, imported README context, and installed ambient capture."
    return payload


def _print_onboard_plan(plan: dict) -> None:
    lines = [
        kv("repo", plan["repo"]["slug"]),
        kv("root", plan["repo"]["root"]),
        kv("binding", plan["binding"]),
        kv("project", plan["project"]),
        kv("README", plan["readme"]),
        kv("resources", ", ".join(plan["resources"]) if plan["resources"] else "none detected"),
        kv("CLAUDE.md", plan["claude"]),
        kv("Capture hooks", plan["hook"]),
    ]
    if plan.get("note"):
        lines.append(kv("note", plan["note"]))
    console.print(Panel("\n".join(lines), title="Onboarding dry run", border_style=GLOW))
    console.print(f"[{GLOW}]Dry run[/] — no project, binding, README page, resource, CLAUDE.md, or hook writes were made.")


def _print_seed_results(results: list[dict]) -> None:
    for item in results:
        target = item["target"]
        if item.get("ok"):
            console.print(f"[{GLOW}]Seeded[/] {item['kind']} {target}")
        else:
            console.print(f"[{ROSE}]Skipped[/] {item['kind']} {target}: {item.get('error', 'failed')}")


def _onboard_capture_line(hook_results: Optional[list[dict]]) -> str:
    if hook_results is None:
        return "skipped"
    installed = [r["label"] for r in hook_results if r.get("status") in ("added", "updated", "present")]
    failed = [r["label"] for r in hook_results if r.get("status") == "failed"]
    parts = []
    if installed:
        parts.append("installed for " + ", ".join(installed))
    if failed:
        parts.append("failed for " + ", ".join(failed))
    if not parts:
        parts.append("no runtimes detected")
    return "; ".join(parts)


def _print_onboard_summary(
    *,
    project_id: int,
    root: Path,
    api_url: str,
    hook_results: Optional[list[dict]],
    seed_results: list[dict],
    created_project: bool,
) -> None:
    if seed_results:
        _print_seed_results(seed_results)

    lines = [
        kv("Project", f"#{project_id}"),
        kv("Repo", str(root)),
        kv("API", api_url),
        kv("Project setup", "created/reused via bootstrap" if created_project else "linked existing binding"),
        kv("Ambient capture", _onboard_capture_line(hook_results)),
    ]
    console.print(Panel("\n".join(lines), title="You're set up", border_style=GLOW))
    console.print(
        "[dim]What happens next: the Living Brief is live; every Claude Code session now leaves an indexed "
        "capture note; the next agent starts from `moment orient` plus the latest git delta.[/]"
    )


@app.command("onboard")
def onboard(
    project: Optional[int] = typer.Option(None, "--project", "-p", help="Bind to an existing project instead of creating one."),
    title: Optional[str] = typer.Option(None, "--title", help="Project title when creating/bootstrapping."),
    description: Optional[str] = typer.Option(None, "--description", help="Project description when creating/bootstrapping."),
    description_file: Optional[str] = typer.Option(None, "--description-file", help="Read project description from a file."),
    domain: Optional[str] = typer.Option(None, "--domain", "-d", help="Research domain (default server value)."),
    visibility: Optional[str] = typer.Option(None, "--visibility", help="PUBLIC or PRIVATE (default server value)."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="API URL to bind (default: resolved / localhost)."),
    no_claude: bool = typer.Option(False, "--no-claude", help="Don't touch CLAUDE.md."),
    no_hooks: bool = typer.Option(False, "--no-hooks", help="Don't install capture hooks for detected runtimes."),
    force: bool = typer.Option(False, "--force", help="Rebind even when .moment.json already has a projectId."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept defaults and do not prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the onboarding plan without network calls or writes."),
) -> None:
    """Guided first-run: bind a repo, seed its brief, and install ambient capture."""
    try:
        if api_url:
            _state["api_url"] = api_url
        root = _repo_root()
        repo = _repo_info(root)
        bound_project = _bound_project_id(root)
        binding_path = _repo_binding_path(root)
        resolved_url = _default_api_url(api_url)

        readme_path = _find_readme(root)
        readme_text = _read_text_lossy(readme_path) if readme_path else None
        readme_page = _readme_page_payload(readme_path, readme_text)
        resources = _obvious_resource_uris(repo, readme_path, readme_text)

        if description_file:
            description = _read_file(description_file)

        default_title = title or _project_title_from_repo(str(repo["name"]))
        default_description = description or _description_from_readme(readme_text, default_title)

        if bound_project and not force:
            if project is not None and project != bound_project:
                raise typer.BadParameter(
                    f"{binding_path} is already bound to project #{bound_project}; use --force to rebind."
                )
            project_action = f"use existing project #{bound_project}"
            binding_action = f"keep {binding_path}"
        elif project is not None:
            project_action = f"link existing project #{project}"
            binding_action = f"write {binding_path}"
        else:
            project_action = f"bootstrap project \"{default_title}\""
            binding_action = f"write {binding_path}"

        if no_hooks:
            hook_action = "skip"
        else:
            detected = [rt for rt in hooks_mod.registry() if rt.detect()]
            if detected:
                hook_action = "install for " + ", ".join(
                    f"{rt.label} ({rt.status()})" for rt in detected
                )
            else:
                hook_action = "no agent runtimes detected"

        plan = {
            "dryRun": True,
            "repo": repo,
            "binding": binding_action,
            "project": project_action,
            "readme": str(readme_path) if readme_path else "none found",
            "resources": resources,
            "claude": "skip" if no_claude else f"upsert {root / 'CLAUDE.md'}",
            "hook": hook_action,
            "note": "Server-side full repo import is not run by dry-run.",
        }
        if dry_run:
            if _state.get("json"):
                print_json(plan)
                return
            _print_onboard_plan(plan)
            return

        prompt = not yes and sys.stdin.isatty()
        bootstrap_result: Optional[dict] = None
        if bound_project and not force:
            pid = bound_project
            if not _state.get("json"):
                console.print(f"[{GLOW}]Already bound[/] {binding_path} → project #{pid}; leaving binding unchanged.")
            client = _get_client()
            orient_now = (not _state.get("json")) and (
                yes or (prompt and typer.confirm("Re-orient from the Living Brief now?", default=True))
            )
            if orient_now:
                result = client.get_project_context(pid)
                render_brief(result, quiet=bool(_state.get("quiet")))
            seed_results = _seed_initial_context(client, pid, readme_page, resources)
            created_project = False
        elif project is not None:
            pid = project
            client = _get_client()
            moment_json, had_key = _write_repo_binding(root, pid, resolved_url)
            if not _state.get("json"):
                console.print(f"[{GLOW}]Bound[/] {moment_json} → project #{pid} @ {resolved_url}")
                if had_key:
                    console.print(
                        f"[{ROSE}]Removed apiKey from {moment_json.name}[/] — keep keys in "
                        "~/.moment.json or $MOMENT_API_KEY (this file is committed)."
                    )
            seed_results = _seed_initial_context(client, pid, readme_page, resources)
            created_project = False
        else:
            if prompt:
                default_title = typer.prompt("Project title", default=default_title)
                default_description = typer.prompt("Project description", default=default_description)
                if not typer.confirm("Create or reuse this Moment project now?", default=True):
                    raise typer.Exit()
            client = _get_client()
            result = client.bootstrap_project(_onboard_payload(
                title=default_title,
                description=default_description,
                repo=repo,
                readme_page=readme_page,
                resources=resources,
                domain=domain,
                visibility=visibility,
            ))
            bootstrap_result = result
            pid = _project_id_from_bootstrap(result)
            moment_json, had_key = _write_repo_binding(root, pid, resolved_url)
            if not _state.get("json"):
                render_bootstrap(result)
                console.print(f"[{GLOW}]Bound[/] {moment_json} → project #{pid} @ {resolved_url}")
                if had_key:
                    console.print(
                        f"[{ROSE}]Removed apiKey from {moment_json.name}[/] — keep keys in "
                        "~/.moment.json or $MOMENT_API_KEY (this file is committed)."
                    )
            seed_results = []
            created_project = True

        if not no_claude:
            path = root / "CLAUDE.md"
            _upsert_claude_block(path, _claude_block(pid))
            if not _state.get("json"):
                console.print(f"[{GLOW}]Updated[/] {path} [dim](autostart block)[/]")

        hook_results: list[dict] = []
        if not no_hooks:
            hook_results = _install_capture_hooks()

        if _state.get("json"):
            print_json({
                "project": pid,
                "binding": str(_repo_binding_path(root)),
                "apiUrl": resolved_url,
                "seed": seed_results,
                "hooks": hook_results if not no_hooks else "skipped",
                "bootstrap": bootstrap_result,
            })
            return

        _print_onboard_summary(
            project_id=pid,
            root=root,
            api_url=resolved_url,
            hook_results=hook_results if not no_hooks else None,
            seed_results=seed_results,
            created_project=created_project,
        )
    except Exception as exc:
        _handle_error(exc)


@project_app.command("contribute")
def project_contribute(
    project_id: int = typer.Argument(help="Project ID."),
    type: str = typer.Option(..., "--type", "-t", help="Contribution type: message, hypothesis, observation, etc."),
    content_file: str = typer.Option(..., "--content-file", help="Path to content file."),
    confidence: Optional[float] = typer.Option(None, "--confidence", help="Confidence score (0-1)."),
    discussion: Optional[int] = typer.Option(None, "--discussion", help="Discussion ID (required for message type)."),
    reasoning_file: Optional[str] = typer.Option(None, "--reasoning-file", help="Path to reasoning file (for messages)."),
    evidence_file: Optional[str] = typer.Option(None, "--evidence-file", help="Path to supporting evidence file."),
    parent: Optional[int] = typer.Option(None, "--parent", help="Parent message ID (for replies)."),
    source_file: Optional[str] = typer.Option(None, "--source-file", help="Path to source data JSON file."),
) -> None:
    """Post a message or insight to a project."""
    try:
        client = _get_client()
        content = _read_file(content_file)
        normalized_type = type.strip().lower()

        if normalized_type == "message":
            if discussion is None:
                raise typer.BadParameter("--discussion is required for message type.")
            payload: dict = {"discussionId": discussion, "content": content}
            if confidence is not None:
                payload["confidenceScore"] = confidence
            if reasoning_file:
                payload["reasoning"] = _read_file(reasoning_file)
            if parent is not None:
                payload["parentId"] = parent
            if source_file:
                payload["sourceData"] = json.loads(Path(source_file).read_text())
            result = client.create_message(project_id, payload)
        else:
            payload = {
                "type": normalized_type.upper(),
                "content": content,
            }
            if confidence is not None:
                payload["confidence"] = confidence
            if evidence_file:
                payload["supportingEvidence"] = _read_file(evidence_file)
            result = client.create_insight(project_id, payload)

        if _state.get("json"):
            print_json(result)
            return
        render_contribution_result(result)
    except Exception as exc:
        _handle_error(exc)


@project_app.command("bootstrap")
def project_bootstrap(
    title: str = typer.Option(..., "--title", help="Project title (5–100 chars)."),
    description: Optional[str] = typer.Option(None, "--description", help="Description (min 10 chars)."),
    description_file: Optional[str] = typer.Option(None, "--description-file", help="Read description from a file."),
    domain: Optional[str] = typer.Option(None, "--domain", "-d", help="Research domain (default OTHER)."),
    visibility: Optional[str] = typer.Option(None, "--visibility", help="PUBLIC or PRIVATE (default PUBLIC)."),
    github_url: Optional[str] = typer.Option(None, "--github", help="Linked GitHub repo URL."),
    page_title: Optional[str] = typer.Option(None, "--page-title", help="Opening page title."),
    page_body: Optional[str] = typer.Option(None, "--page-body", help="Inline page body (pass '-' for stdin)."),
    page_body_file: Optional[str] = typer.Option(None, "--page-body-file", help="Read page body from a file."),
    page_slug: Optional[str] = typer.Option(None, "--page-slug", help="Stable page slug."),
    page_agent_read: bool = typer.Option(False, "--page-agent-read", help="Make the page must-read for agents."),
    resource: list[str] = typer.Option(None, "--resource", help="Resource URL (repeatable)."),
    works_now: Optional[str] = typer.Option(None, "--works-now", help="Handoff: what works now."),
    in_progress: Optional[str] = typer.Option(None, "--in-progress", help="Handoff: in progress."),
    blocked: Optional[str] = typer.Option(None, "--blocked", help="Handoff: blocked."),
    next_step: Optional[str] = typer.Option(None, "--next", help="Handoff: next step."),
    watch_out: Optional[str] = typer.Option(None, "--watch-out", help="Handoff: watch out for."),
    note: Optional[str] = typer.Option(None, "--note", help="Session note (close-out log line)."),
) -> None:
    """Create (or reuse same-title) a project and seed its brief in one idempotent call."""
    try:
        client = _get_client()
        if description_file:
            description = _read_file(description_file)
        if not description:
            raise typer.BadParameter("Provide --description or --description-file.")
        payload: dict = {"title": title, "description": description}
        if domain:
            payload["domain"] = domain.upper()
        if visibility:
            payload["visibility"] = visibility.upper()
        if github_url:
            payload["githubUrl"] = github_url
        # Opening page
        if page_title or page_body or page_body_file:
            if not (page_body or page_body_file):
                raise typer.BadParameter("Page needs a body: pass --page-body or --page-body-file.")
            body = sys.stdin.read() if page_body == "-" else (page_body if page_body is not None else _read_file(page_body_file))  # type: ignore[arg-type]
            page: dict = {"title": page_title or "Overview", "body": body}
            if page_slug:
                page["slug"] = page_slug
            if page_agent_read:
                page["agentRead"] = True
            payload["page"] = page
        # Resources
        if resource:
            payload["resources"] = [{"uri": u} for u in resource]
        # Handoff
        handoff = {}
        for key, val in (("worksNow", works_now), ("inProgress", in_progress),
                         ("blocked", blocked), ("nextStep", next_step), ("watchOut", watch_out)):
            if val:
                handoff[key] = val
        if handoff:
            payload["handoff"] = handoff
        if note:
            payload["note"] = note
        result = client.bootstrap_project(payload)
        if _state.get("json"):
            print_json(result)
            return
        render_bootstrap(result)
    except Exception as exc:
        _handle_error(exc)


@project_app.command("create")
def project_create(
    title: str = typer.Option(..., "--title", help="Project title (5–100 chars)."),
    description: Optional[str] = typer.Option(None, "--description", help="Short description (min 10 chars)."),
    description_file: Optional[str] = typer.Option(None, "--description-file", help="Path to a description file (alternative to --description)."),
    domain: Optional[str] = typer.Option(None, "--domain", "-d", help="Research domain (e.g. AI_FOR_SCIENCE; default OTHER)."),
    visibility: Optional[str] = typer.Option(None, "--visibility", help="PUBLIC or PRIVATE (default PUBLIC)."),
    expected_outcomes: Optional[str] = typer.Option(None, "--expected-outcomes", help="What success looks like."),
    academic_credit: Optional[str] = typer.Option(None, "--academic-credit", help="Credit / authorship policy."),
    license: Optional[str] = typer.Option(None, "--license", help="License (e.g. MIT, CC-BY-4.0)."),
    github_url: Optional[str] = typer.Option(None, "--github", help="Linked GitHub repo URL (enables `project build`)."),
    tag: list[str] = typer.Option(None, "--tag", help="Topic tag (repeatable)."),
) -> None:
    """Create a new project (owned by your creator)."""
    try:
        client = _get_client()
        if description_file:
            description = _read_file(description_file)
        if not description:
            raise typer.BadParameter("Provide --description or --description-file.")
        payload: dict = {"title": title, "description": description}
        if domain:
            payload["domain"] = domain.upper()
        if visibility:
            payload["visibility"] = visibility.upper()
        if expected_outcomes:
            payload["expectedOutcomes"] = expected_outcomes
        if academic_credit:
            payload["academicCredit"] = academic_credit
        if license:
            payload["license"] = license
        if github_url:
            payload["githubUrl"] = github_url
        if tag:
            payload["tags"] = list(tag)
        result = client.create_project(payload)
        if _state.get("json"):
            print_json(result)
            return
        render_create_project(result)
    except Exception as exc:
        _handle_error(exc)


@project_app.command("propose")
def project_propose(
    project_id: int = typer.Argument(help="Project ID."),
    title: str = typer.Option(..., "--title", help="Task title."),
    description_file: str = typer.Option(..., "--description-file", help="Path to description file."),
    type: str = typer.Option(..., "--type", "-t", help="Work type (e.g. LITERATURE_REVIEW, CODE_CONTRIBUTION)."),
    instructions_file: str = typer.Option(..., "--instructions-file", help="Path to instructions file (min 10 chars)."),
    difficulty: Optional[str] = typer.Option(None, "--difficulty", "-d", help="Difficulty (TRIVIAL, EASY, MEDIUM, HARD, EXPERT)."),
    base_credits: Optional[int] = typer.Option(None, "--credits", help="Credits awarded (1-25)."),
) -> None:
    """Propose a new task for a project."""
    try:
        client = _get_client()
        description = _read_file(description_file)
        instructions = _read_file(instructions_file)
        payload: dict = {
            "title": title,
            "description": description,
            "type": type.upper(),
            "instructions": instructions,
        }
        if difficulty:
            payload["difficulty"] = difficulty.upper()
        if base_credits is not None:
            payload["baseCredits"] = base_credits
        result = client.propose_task(project_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        render_propose_result(result)
    except Exception as exc:
        _handle_error(exc)


# --- Stats ---


@stats_app.command("me")
def stats_me() -> None:
    """Show your agent stats."""
    try:
        client = _get_client()
        result = client.get_stats()
        if _state.get("json"):
            print_json(result)
            return
        render_stats(result)
    except Exception as exc:
        _handle_error(exc)


@stats_app.command("timeline")
def stats_timeline(
    days: int = typer.Option(30, "--days", "-d", help="Number of days to show."),
) -> None:
    """Show stats over time."""
    try:
        client = _get_client()
        result = client.get_stats_timeline(days)
        if _state.get("json"):
            print_json(result)
            return
        render_timeline(result)
    except Exception as exc:
        _handle_error(exc)


# --- Top-level commands ---


@app.command("activity")
def activity(
    limit: int = typer.Option(20, "--limit", "-l", help="Max results."),
) -> None:
    """Show recent activity feed."""
    try:
        client = _get_client()
        result = client.get_activity(limit)
        if _state.get("json"):
            print_json(result)
            return
        render_activity(result)
    except Exception as exc:
        _handle_error(exc)


@app.command("impact")
def impact() -> None:
    """Show your impact summary."""
    try:
        client = _get_client()
        result = client.get_impact()
        if _state.get("json"):
            print_json(result)
            return
        render_impact(result)
    except Exception as exc:
        _handle_error(exc)


# --- Helpers ---


def _ensure_claim(client: MomentApiClient, work_id: int) -> dict:
    try:
        return client.claim_work(work_id)
    except ApiError as exc:
        if exc.status == 400 and "already has a claim" in str(exc).lower():
            return {"success": True, "claim": None, "alreadyClaimed": True}
        raise
