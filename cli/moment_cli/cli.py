"""Typer CLI application."""

from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
import httpx
from rich.markup import escape
from rich.panel import Panel
from typer.core import TyperGroup

try:  # typer 0.2x vendors click as typer._click
    from typer._click.exceptions import NoArgsIsHelpError as _NoArgsIsHelp
    from typer._click.exceptions import UsageError as _UsageError
except ImportError:  # older typer depends on click itself
    from click.exceptions import UsageError as _UsageError

    class _NoArgsIsHelp(Exception):  # click < 8.2 shows help without raising
        pass

from moment_cli import __version__, close_out, hooks as hooks_mod
from moment_cli import doctor as doctor_mod
from moment_cli import human_auth
from moment_cli.api import ApiError, MomentApiClient, UntrustedServerError
from moment_cli.campaign import campaign_app
from moment_cli.team import team_app
from moment_cli.config import (
    DEFAULT_API_URL,
    api_origin,
    campaign_context_key,
    human_token_expired,
    parse_iso,
    read_repo_bindings,
    resolve_config,
    trust_api_url,
    update_home_config,
)
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
    render_brief,
    render_bootstrap,
    render_contribution_result,
    render_create_project,
    render_falsifier_created,
    render_falsifiers,
    render_gather,
    render_graph,
    render_github_sync_summary,
    render_impact,
    render_ingest_dry_run,
    render_ingest_result,
    render_ingest_watch_dry_run,
    render_ingest_watch_result,
    render_packet,
    render_packet_list,
    render_page,
    render_pr_status,
    render_project_context,
    render_propose_result,
    render_saved_packet,
    render_stats,
    render_submission,
    render_timeline,
    render_work_context,
    render_work_list,
    render_work_start,
    render_find,
    render_page_written,
    render_resource_added,
    render_resource_content,
    render_resources,
    stdout_is_rich,
    write_queued,
)

# Options of the root callback that may also be written after the subcommand.
# Only these three: they choose output and project, never server or credentials.
# `--api-url`, `--api-key`, and `--config` stay before the subcommand, so text that
# lands in a positional slot (a search query, a checklist item) can't redirect
# where the key is sent.
_GLOBAL_FLAGS = frozenset({"--json", "--quiet"})
_GLOBAL_VALUED = frozenset({"--project", "-p"})


def _option_names(command: object) -> tuple[set[str], set[str]]:
    """(every option spelling, the spellings that take a value) for one command."""
    names: set[str] = set()
    valued: set[str] = set()
    for param in getattr(command, "params", None) or []:
        if getattr(param, "param_type_name", "") != "option":
            continue
        spellings = list(param.opts) + list(getattr(param, "secondary_opts", None) or [])
        names.update(spellings)
        if not getattr(param, "is_flag", False) and not getattr(param, "count", False):
            valued.update(param.opts)
    return names, valued


def _hoist_global_options(root: object, args: list[str]) -> list[str]:
    """Move global options written after the subcommand to before it.

    `moment orient --json` and `moment log -p 42 …` are the natural spellings,
    but the parser only reads global options before the subcommand. A token is
    hoisted only when the command it follows does not define that option itself,
    and values of that command's own options (`--note "-p is fine"`) are skipped.
    """
    _root_names, root_valued = _option_names(root)
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] != "-":
        if args[index] == "--":
            return args
        head = args[index].split("=", 1)[0]
        index += 2 if head in root_valued and "=" not in args[index] else 1
    insert_at = index

    hoisted: list[str] = []
    passthrough: list[str] = []
    command = root
    while index < len(args) and hasattr(command, "commands"):
        token = args[index]
        subcommand = command.commands.get(token)  # type: ignore[attr-defined]
        if subcommand is not None:
            passthrough.append(token)
            command = subcommand
            index += 1
            continue
        head = token.split("=", 1)[0]
        if head in _GLOBAL_FLAGS:
            hoisted.append(token)
            index += 1
            continue
        if head in _GLOBAL_VALUED:
            take = 1 if "=" in token or index + 1 >= len(args) else 2
            hoisted.extend(args[index:index + take])
            index += take
            continue
        return args  # an unknown command or a group option: the parser reports it

    leaf_names, leaf_valued = _option_names(command)
    kept: list[str] = []
    while index < len(args):
        token = args[index]
        if token == "--":
            kept.extend(args[index:])
            break
        head = token.split("=", 1)[0] if token.startswith("--") else token
        if token.startswith("-") and token != "-" and head not in leaf_names:
            if head in _GLOBAL_FLAGS:
                hoisted.append(token)
                index += 1
                continue
            if head in _GLOBAL_VALUED:
                take = 1 if "=" in token or index + 1 >= len(args) else 2
                hoisted.extend(args[index:index + take])
                index += take
                continue
        kept.append(token)
        if head in leaf_valued and "=" not in token and index + 1 < len(args):
            kept.append(args[index + 1])
            index += 2
            continue
        index += 1

    if not hoisted:
        return args
    return args[:insert_at] + hoisted + passthrough + kept


def _report_usage_error(exc: Exception) -> "SystemExit":
    """A parser complaint (missing argument, unknown option) in the one JSON error format."""
    command = getattr(getattr(exc, "ctx", None), "command_path", None) or "moment"
    message = exc.format_message() if hasattr(exc, "format_message") else str(exc)
    _report_failure(message, hint=f"`{command} --help` shows the usage.")
    return SystemExit(2)


def _config_exit_hint(message: str) -> Optional[str]:
    if "alias" in message:
        return "`moment project list` shows this repository's aliases."
    if "moment doctor" in message:
        return None
    return "`moment doctor` checks the binding, key, and server together."


class MomentGroup(TyperGroup):
    """The root command: global options anywhere; every failure in one format."""

    def parse_args(self, ctx, args):  # type: ignore[override]
        args = _hoist_global_options(self, list(args))
        # The parser consumes the list it is given, and this runs before the root
        # callback: read --json from the argv now.
        wants_json = "--json" in _leading_options(self, args)
        try:
            return super().parse_args(ctx, args)
        except _UsageError as exc:
            if not wants_json or isinstance(exc, _NoArgsIsHelp):
                raise
            _state["json"] = True
            raise _report_usage_error(exc) from None

    def invoke(self, ctx):  # type: ignore[override]
        # An unknown subcommand is reported before the root callback sets _state.
        _state["json"] = bool(ctx.params.get("json_mode"))
        try:
            return super().invoke(ctx)
        except _UsageError as exc:
            if not _state.get("json") or isinstance(exc, _NoArgsIsHelp):
                raise
            raise _report_usage_error(exc) from None
        except SystemExit as exc:
            # Config resolution exits with a sentence (missing key, unknown alias,
            # unreadable config). That is a usage failure: exit 2, JSON-aware.
            if not isinstance(exc.code, str):
                raise
            _report_failure(exc.code, hint=_config_exit_hint(exc.code))
            raise SystemExit(2) from None


def _leading_options(root: object, args: list[str]) -> list[str]:
    """The root-level tokens before the first subcommand name."""
    _names, valued = _option_names(root)
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] != "-":
        head = args[index].split("=", 1)[0]
        index += 2 if head in valued and "=" not in args[index] else 1
    return args[:index]


app = typer.Typer(
    name="moment",
    cls=MomentGroup,
    help=(
        "Moment CLI: read and write a Moment project's shared context from a repository.\n\n"
        "Loop: `moment orient` (the brief) -> `moment page-read <slug>` or `moment find \"<question>\"` "
        "-> work -> `moment log --note \"...\" --next \"...\"` (close out).\n\n"
        "The project comes from -p <id|alias> or this repository's .moment.json. -p, --json, and "
        "--quiet work before or after the subcommand; --api-url, --api-key, and --config go before it. "
        "Output is plain text when piped; add --json to parse it. --quiet drops tips and footers. "
        "`moment commands` maps every command."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich" if stdout_is_rich() else None,
    # Piped help defaults to 80 columns, which cuts command summaries at ~45 characters.
    context_settings={} if stdout_is_rich() else {"terminal_width": 120, "max_content_width": 120},
    pretty_exceptions_enable=False,
)

auth_app = typer.Typer(help="Authentication commands.", no_args_is_help=True)
task_app = typer.Typer(help="Task discovery, claiming, and submission.", no_args_is_help=True)
work_app = typer.Typer(hidden=True, help="Legacy alias for task commands.", no_args_is_help=True)
project_app = typer.Typer(help="Project context and contributions.", no_args_is_help=True)
checklist_app = typer.Typer(help="Project coordination checklist.", no_args_is_help=True)
claim_app = typer.Typer(
    help="Claim ledger — falsifiers: what would change our mind about a claim.",
    no_args_is_help=True,
)
experiment_app = typer.Typer(help="Experiment state, results, and leaderboard.", no_args_is_help=True)
discussion_app = typer.Typer(help="Project discussions.", no_args_is_help=True)
stats_app = typer.Typer(help="Agent statistics.", no_args_is_help=True)
hooks_app = typer.Typer(help="Install ambient capture hooks across agent runtimes.", no_args_is_help=True)
github_app = typer.Typer(help="GitHub sync — pull PR/issue discussion into Moment context.", no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(task_app, name="task")
app.add_typer(work_app, name="work")
app.add_typer(project_app, name="project")
app.add_typer(checklist_app, name="checklist")
app.add_typer(claim_app, name="claim")
app.add_typer(experiment_app, name="experiment")
app.add_typer(discussion_app, name="discussion")
app.add_typer(stats_app, name="stats")
app.add_typer(hooks_app, name="hooks")
app.add_typer(github_app, name="github")
app.add_typer(campaign_app, name="campaign")
app.add_typer(team_app, name="team")


@work_app.callback()
def work_alias_notice() -> None:
    """Keep the old group usable while teaching the canonical noun."""
    sys.stderr.write("moment work is now moment task\n")

# --- Global state ---

_state: dict = {}


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"moment {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    api_key: Optional[str] = typer.Option(None, "--api-key", help="Override API key."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override API base URL."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to JSON config file."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or repository alias."),
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
    project: Optional[int | str] = None,
    hint: Optional[str] = None,
    require_api_key: bool = True,
):
    cfg = resolve_config(
        api_key=_state.get("api_key"),
        api_url=_state.get("api_url"),
        config_path=_state.get("config"),
        project_id=project if project is not None else _state.get("project"),
        project_hint=hint,
        quiet=bool(_state.get("quiet")),
        require_api_key=require_api_key,
    )
    _state["_config"] = cfg
    return cfg


HOOK_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)


def _get_client(
    project: Optional[int | str] = None,
    *,
    hint: Optional[str] = None,
    hook_mode: bool = False,
) -> MomentApiClient:
    cfg = _resolve_cli_config(project=project, hint=hint)
    return MomentApiClient(cfg, timeout=HOOK_TIMEOUT if hook_mode else None)


def _interactive() -> bool:
    """A person at a terminal, who can approve a browser sign-in."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _sign_in_if_needed() -> None:
    """First run: with no agent key and no live sign-in, start the browser sign-in.

    Only a person at a terminal can approve it, so a non-interactive caller (an
    agent) gets told to ask a person instead of waiting on a browser for minutes.
    """
    cfg = _resolve_cli_config(require_api_key=False)
    signed_in = cfg.human_token and not human_token_expired(cfg.human_token_expires_at)
    if cfg.api_key or signed_in:
        return
    if not cfg.api_url_trusted:
        raise UntrustedServerError(cfg)
    if not _interactive():
        raise typer.BadParameter(
            f"No Moment credentials on this machine for {cfg.api_url}. A person runs "
            "`moment onboard` (or `moment auth login`) in a terminal to sign in through the browser."
        )
    if not _state.get("json"):
        console.print("First, sign in to Moment.")
    human_auth.run_device_login(
        cfg, open_browser=True, json_mode=bool(_state.get("json")), quiet=bool(_state.get("quiet")),
    )


def _mint_agent_key_if_needed() -> bool:
    """Mint this person's machine key once; failures leave human auth usable."""
    def report(message: str) -> None:
        if _state.get("json"):
            sys.stderr.write(message + "\n")
        else:
            console.print(message)

    try:
        cfg = _resolve_cli_config(require_api_key=False)
    except (Exception, SystemExit) as exc:
        report(f"Could not mint an agent key: {exc}. Continuing human-only.")
        return False
    if cfg.api_key or not cfg.human_token:
        return False
    if human_token_expired(cfg.human_token_expires_at):
        report("Could not mint an agent key: the saved human token has expired. Continuing human-only.")
        return False
    name = socket.gethostname() or "this machine"
    try:
        result = MomentApiClient(cfg, human=True).post(
            "/api/agents",
            {"name": name, "type": "LLM", "version": __version__},
        )
        api_key = result.get("apiKey") if isinstance(result, dict) else None
        if not isinstance(api_key, str) or not api_key:
            report("Could not mint an agent key: the server returned no key. Continuing human-only.")
            return False
        update_home_config({"apiKey": api_key, "apiKeySource": "minted by login"})
        report(f"Agent key minted for this machine ({name}).")
        return True
    except ApiError as exc:
        if exc.status == 403:
            report(str(exc))
        elif exc.status == 429:
            report("You already have 10 agents — rotate one on /agents.")
        else:
            report(f"Could not mint an agent key: {exc}. Continuing human-only.")
    except Exception as exc:
        report(f"Could not mint an agent key: {exc}. Continuing human-only.")
    return False


def _resolve_project_id() -> int:
    """The effective project for binding-aware commands (orient/log/sync).

    Requires _get_client() to have run first (it populates _state['_config']).
    """
    cfg = _state.get("_config")
    pid = cfg.project_id if cfg else None
    if pid is None:
        raise typer.BadParameter(NO_PROJECT_MESSAGE)
    return pid


NO_PROJECT_MESSAGE = (
    "No project could be selected. Pass -p <id-or-alias>, set MOMENT_PROJECT, or "
    "set defaultProject in .moment.json. `moment project list` shows this repository's bindings."
)

PROJECT_ARG_HELP = "Project id or alias (default: -p, else this repository's binding)."


def _project_arg(value: Optional[str]) -> int:
    """A project written as an id or alias; None means -p, else the repo binding."""
    cfg = _resolve_cli_config(project=value, require_api_key=False)
    if cfg.project_id is None:
        raise typer.BadParameter(NO_PROJECT_MESSAGE)
    return cfg.project_id


def _project_and(values: list[str], what: str) -> tuple[int, str]:
    """`[PROJECT] THING` positionals: two values name the project; one leaves it to -p."""
    if len(values) == 2:
        return _project_arg(values[0]), values[1]
    if len(values) == 1:
        return _project_arg(None), values[0]
    raise typer.BadParameter(f"Expected [PROJECT] {what} (got {len(values)} values).")


def _int_value(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise typer.BadParameter(f"{what} must be a number (got {value!r}).") from None


def _explicit_project(value: Optional[str]) -> Optional[int]:
    """This command's own --project, else an explicit -p / MOMENT_PROJECT; never the
    repository default. For filters and scopes, where no project means every project."""
    selector = (
        value
        or _state.get("project")
        or os.environ.get("MOMENT_PROJECT")
        or os.environ.get("MOMENT_PROJECT_ID")
    )
    if not selector or not str(selector).strip():
        return None
    return _project_arg(str(selector))


def _server_url() -> str:
    cfg = _state.get("_config")
    return getattr(cfg, "api_url", None) or _default_api_url()


def _api_error_hint(exc: ApiError) -> Optional[str]:
    """The server's own recovery step when it sent one, else one keyed to the status."""
    details = exc.details if isinstance(exc.details, dict) else {}
    server_step = details.get("nextStep") if isinstance(details.get("nextStep"), str) else None
    server = _server_url()
    status = exc.status
    if status == 401:
        local = (
            f"{server} rejected the API key. Keys are per server; `moment doctor` checks "
            "which key and server this command used."
        )
        return f"{server_step} {local}" if server_step else local
    if server_step:
        return server_step
    if status == 403:
        return "The key is valid but lacks access for this action on this project."
    if status == 404:
        return (
            f"Not found on {server}. Check the project id or alias, slug, or resource id "
            "(`moment project list` shows this repository's bindings)."
        )
    if status == 409:
        return "Conflict with the current state: re-read it, then retry."
    if status == 429:
        return "Rate limited: wait, then retry."
    if status >= 500:
        return f"Server error at {server}: retry shortly; if it persists, run `moment doctor`."
    return None


class MomentFailure(Exception):
    """A command that ran correctly but could not finish (exit 1, not a usage error)."""


def _describe_error(exc: BaseException) -> tuple[str, Optional[int], object, Optional[str]]:
    """(message, HTTP status, server details, next-step hint) for one failure."""
    if isinstance(exc, ApiError):
        return str(exc), exc.status, exc.details, _api_error_hint(exc)
    if isinstance(exc, UntrustedServerError):
        return str(exc), None, None, exc.hint
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"Timed out waiting for {_server_url()}.", None, None,
            "Retry; if it keeps timing out, run `moment doctor`.",
        )
    if isinstance(exc, httpx.RequestError):
        reason = str(exc) or type(exc).__name__
        return (
            f"Could not reach {_server_url()} ({reason}).", None, None,
            "The server comes from --api-url, then ./.moment.json apiUrl, then MOMENT_API_URL, "
            "then ~/.moment.json. `moment doctor` checks the whole chain.",
        )
    return str(exc), None, None, None


def _report_failure(message: str, status: Optional[int] = None,
                    details: object = None, hint: Optional[str] = None) -> None:
    if _state.get("json"):
        print_error_json(message, status, details, hint=hint)
    else:
        print_error(message, status, hint=hint)


def _handle_error(exc: Exception) -> None:
    """Report a failure on stderr and exit: 2 for misuse, 1 for a refusal or failure."""
    if isinstance(exc, (typer.Exit, typer.Abort)):
        raise exc  # a deliberate exit from inside the command, not a failure
    if isinstance(exc, BrokenPipeError):
        # The reader stopped early (`| head`): not a failure. Point stdout at
        # devnull so the interpreter's final flush doesn't raise again.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        raise typer.Exit(0)
    message, status, details, hint = _describe_error(exc)
    _report_failure(message, status, details, hint)
    misuse = isinstance(exc, (typer.BadParameter, ValueError, UntrustedServerError))
    raise typer.Exit(2 if misuse else 1)


def _read_file(path: str) -> str:
    """A file's text, or stdin when the path is `-`."""
    if path == "-":
        return sys.stdin.read().strip()
    return Path(path).read_text().strip()


def _default_api_url(api_url: Optional[str] = None) -> str:
    return (
        api_url
        or _state.get("api_url")
        or os.environ.get("MOMENT_API_URL")
        or os.environ.get("MOMENT_MCP_API_URL")
        or DEFAULT_API_URL
    ).rstrip("/")


# The indexed-status sentences, mirroring app/lib/context-index.ts's
# INDEXED_STATUS_SENTENCES. A write that could not be embedded must SAY so —
# "skipped" would read as "nothing to do" and hide a dead provider.
INDEXED_STATUS_SENTENCES = {
    "done": "Searchable now.",
    "pending": "Indexing finishes after this reply.",
    "skipped": "Nothing to index (too short or not eligible for retrieval).",
    "failed": (
        "Not searchable yet — the embedding provider refused; "
        "the next write to this project retries it."
    ),
}


def _indexed_sentence(result: dict) -> Optional[str]:
    """The one-line searchability report for a write, or None if unreported."""
    if not isinstance(result, dict):
        return None
    return INDEXED_STATUS_SENTENCES.get(str(result.get("indexed") or ""))


def _version_conflict(exc: Exception) -> Optional[dict]:
    """The 409 body when this error is a lost-update collision, else None."""
    if not isinstance(exc, ApiError) or exc.status != 409:
        return None
    details = exc.details
    if isinstance(details, dict) and details.get("code") == "version_conflict":
        return details
    return None


def _log_session_with_retry(client, project_id: int, payload: dict) -> dict:
    """POST a close-out, retrying ONCE against the version that won the race.

    Six writers close out on one project minutes apart. When the handoff moved
    between our read and our write the server refuses the patch rather than
    overwriting it, and writes NOTHING — not even the session note — so a retry
    cannot double-log. We re-send the identical patch against the new version
    once; the accumulating fields put our lines beside theirs. If the second
    attempt collides too, we stop and print both versions rather than guessing.
    """
    try:
        return client.log_session(project_id, payload)
    except Exception as exc:
        conflict = _version_conflict(exc)
        if conflict is None:
            raise
        current = conflict.get("currentVersion")
        if not isinstance(current, int):
            raise
        retry = dict(payload, handoffIfVersion=current)
        try:
            return client.log_session(project_id, retry)
        except Exception as exc2:
            second = _version_conflict(exc2)
            if second is None:
                raise
            raise MomentFailure(
                "Nothing was written — the handoff moved twice while this "
                "close-out was in flight.\n"
                f"  you read version {conflict.get('expectedVersion')}\n"
                f"  it was version {current} when the retry went out\n"
                f"  it is version {second.get('currentVersion')} now\n"
                "Neither the handoff nor the session note was saved, so nothing "
                "is duplicated. Re-read the brief (moment orient), merge your "
                "lines into it by hand, and run the same command again with "
                f"--if-version {second.get('currentVersion')}."
            ) from exc2


NOTE_MAX_CHARS = 4000
NOTE_NEXT_STEP_MAX_CHARS = 2000
HANDOFF_FIELD_MAX_CHARS = 4000
READ_FIRST_MAX_ITEMS = 20


def _read_first_items(values: Optional[list[str]]) -> list[dict]:
    """`--read-first 'LABEL'` or `'LABEL :: why'` → the handoff's readFirst items."""
    items: list[dict] = []
    for raw in values or []:
        label, _sep, why = raw.partition(" :: ")
        label, why = label.strip(), why.strip()
        if not label:
            raise typer.BadParameter(f"--read-first needs a label (got {raw!r}).")
        items.append({"label": label[:200], **({"why": why[:300]} if why else {})})
    if len(items) > READ_FIRST_MAX_ITEMS:
        raise typer.BadParameter(f"--read-first takes at most {READ_FIRST_MAX_ITEMS} items.")
    return items


def _build_session_payload(
    note: str,
    next_step: Optional[str] = None,
    task: Optional[int] = None,
    *,
    works_now: Optional[str] = None,
    in_progress: Optional[str] = None,
    blocked: Optional[str] = None,
    watch_out: Optional[str] = None,
    tried_and_failed: Optional[str] = None,
    decisions: Optional[str] = None,
    open_questions: Optional[str] = None,
    read_first: Optional[list[dict]] = None,
    if_version: Optional[int] = None,
) -> dict:
    """Build the POST body for the session-notes / close-out endpoint.

    The handoff is patched only with the fields given, so a plain note never
    bumps the handoff version. `--next` is the brief's next step: it goes to the
    handoff and, when it fits the note's 2000-char field, to the note as well.
    """
    payload: dict = {"note": note}
    if next_step and len(next_step) <= NOTE_NEXT_STEP_MAX_CHARS:
        payload["nextStep"] = next_step
    if task is not None:
        payload["taskId"] = task
    handoff: dict = {}
    for key, val in (
        ("nextStep", next_step),
        ("worksNow", works_now),
        ("inProgress", in_progress),
        ("blocked", blocked),
        ("watchOut", watch_out),
        ("triedAndFailed", tried_and_failed),
        ("decisions", decisions),
        ("openQuestions", open_questions),
    ):
        if val:
            if len(val) > HANDOFF_FIELD_MAX_CHARS:
                raise typer.BadParameter(
                    f"The handoff's {key} is {len(val)} characters; the limit is "
                    f"{HANDOFF_FIELD_MAX_CHARS}. Put the detail in a page and point to it."
                )
            handoff[key] = val
    if read_first:
        handoff["readFirst"] = read_first
    if handoff:
        payload["handoff"] = handoff
        if if_version is not None:
            payload["handoffIfVersion"] = if_version
    return payload


# --- Auth ---


@auth_app.command("whoami")
def auth_whoami() -> None:
    """Show both identities: the person signed in, and the agent key, if any.

    They are different credentials with different authority, so neither is ever
    reported as the other. `--json` always carries both keys, with nulls.
    """
    cfg = _resolve_cli_config(require_api_key=False)
    human = human_auth.human_identity(cfg)
    agent = human_auth.agent_identity(cfg)
    if _state.get("json"):
        print_json({"human": human, "agent": agent})
        return
    if not human and not agent:
        sys.stderr.write(
            "No identity on this machine.\n"
            "  moment auth login             sign in as yourself\n"
            "  moment auth login --key <k>   save an agent key\n"
        )
        raise typer.Exit(2)
    human_auth.render_whoami(human, agent, cfg)


@auth_app.command("login")
def auth_login(
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Save an AGENT API key instead of signing in."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the code and URL; do not open a browser."),
    label: Optional[str] = typer.Option(None, "--label", help="How this machine appears on the approval page."),
    no_mint: bool = typer.Option(False, "--no-mint", help="Sign in without minting an agent key for this machine."),
) -> None:
    """Sign in as yourself through the browser (or save an agent key with --key).

    Without --key this runs the browser device grant: the terminal shows a short
    code, you confirm it in Moment, and the token that comes back is saved to
    ~/.moment.json. An agent key can never stand in for a person, so campaign
    commands use this token and nothing else.
    """
    if key:
        path = update_home_config({"apiKey": key, "apiKeySource": "supplied"})
        if _state.get("json"):
            print_json({"saved": str(path)})
        else:
            console.print(f"[{GLOW}]API key saved to {path}[/]")
        return
    cfg = _resolve_cli_config(require_api_key=False)
    if not cfg.api_url_trusted:
        # Signing in to a server a checkout chose would hand the person's
        # approval (and a token) to it.
        _handle_error(UntrustedServerError(cfg))
    human_auth.run_device_login(
        cfg,
        label=label,
        open_browser=not no_browser,
        json_mode=bool(_state.get("json")),
        quiet=bool(_state.get("quiet")),
    )
    if not no_mint:
        _mint_agent_key_if_needed()


@auth_app.command("logout")
def auth_logout() -> None:
    """Revoke this machine's CLI token and forget the saved identity."""
    cfg = _resolve_cli_config(require_api_key=False)
    human_auth.run_logout(cfg, json_mode=bool(_state.get("json")))


# --- Work (tasks) ---


@task_app.command("list")
@work_app.command("list")
def work_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Only this project (id or alias). Default: every project."),
    type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by work type (e.g. CODE_CONTRIBUTION)."),
    difficulty: Optional[str] = typer.Option(None, "--difficulty", "-d", help="Filter by difficulty (EASY, MEDIUM, HARD)."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", "-l", help="Max results."),
) -> None:
    """List open tasks across the lab (or one project with -p)."""
    try:
        client = _get_client()
        result = client.list_work(_explicit_project(project), type, difficulty, limit)
        if _state.get("json"):
            print_json(result)
            return
        render_work_list(result)
    except Exception as exc:
        _handle_error(exc)


@task_app.command("claim")
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


@task_app.command("mine")
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
            console.print(f"[{GLOW}]No active claims. Use `moment task list` to find tasks.[/]")
            return
        for claim in claims:
            status_color = "#8ce99a" if claim["status"] == "ACTIVE" else "#ffd43b"
            console.print(f"  [{status_color}]{claim['status']}[/] #{claim['taskId']} — {claim['title']}")
            console.print(f"         {claim['workType']} · {claim['credits']} credits · {claim['project']['title']}")
        console.print(f"\n  [dim]{result.get('active', 0)} active, {result.get('submitted', 0)} submitted[/dim]")
    except Exception as exc:
        _handle_error(exc)


@task_app.command("show")
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


@task_app.command("submit")
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


@task_app.command("pr")
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


@task_app.command("pr-status")
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
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """Fetch full project context (the raw brief payload; `orient` renders it)."""
    try:
        project_id = _project_arg(project)
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
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """List a project's resources (the context manifest): id, kind, title, URI."""
    try:
        result = _get_client().list_resources(_project_arg(project))
        if _state.get("json"):
            print_json(result)
            return
        render_resources(result, quiet=bool(_state.get("quiet")))
    except Exception as exc:
        _handle_error(exc)


@project_app.command("add-resource")
def project_add_resource(
    args: list[str] = typer.Argument(..., metavar="[PROJECT] URI", help="Optional project id or alias, then the resource URI (repo, paper, dataset, doc)."),
    title: Optional[str] = typer.Option(None, "--title"),
    kind: Optional[str] = typer.Option(None, "--kind", help="DATASET|CODE_REPO|PAPER|... (auto-detected if omitted)."),
    summary: Optional[str] = typer.Option(None, "--summary", help="One line on why this artifact matters."),
    role: Optional[str] = typer.Option(None, "--role", help="BACKGROUND|INPUT|OUTPUT|TOOL|REFERENCE."),
) -> None:
    """Attach one resolvable artifact to a project (deduplicated)."""
    try:
        project_id, uri = _project_and(args, "URI")
        payload: dict = {"uri": uri}
        for key, val in (("title", title), ("kind", kind), ("summary", summary), ("role", role)):
            if val is not None:
                payload[key] = val
        client = _get_client()
        result = client.add_resource(project_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        render_resource_added(result, client.last_status)
    except Exception as exc:
        _handle_error(exc)


@project_app.command("read-resource")
def project_read_resource(
    args: list[str] = typer.Argument(..., metavar="[PROJECT] RESOURCE_ID", help="Optional project id or alias, then the resource id (from `project resources`)."),
    fidelity: Optional[str] = typer.Option(None, "--fidelity", help="summary (default) | metadata | preview | text (the full content)."),
    section: Optional[str] = typer.Option(None, "--section", help="Return just one heading of the text."),
) -> None:
    """Pull one resource's content into context (a summary unless --fidelity text)."""
    try:
        project_id, raw_id = _project_and(args, "RESOURCE_ID")
        resource_id = _int_value(raw_id, "RESOURCE_ID")
        result = _get_client().read_resource(project_id, resource_id, fidelity, section)
        if _state.get("json"):
            print_json(result)
            return
        render_resource_content(result)
    except Exception as exc:
        _handle_error(exc)


PAGE_ROLES = ("BRIEF", "SPEC", "PLAN", "DECISION", "REPORT", "REFERENCE", "NOTE")


@app.command("page-read")
@project_app.command("page-read")
def project_page_read(
    args: list[str] = typer.Argument(..., metavar="[PROJECT] SLUG", help="Optional project id or alias, then the page slug (from orient, find, or gather)."),
) -> None:
    """Read one page's full markdown body (the read twin of `page`)."""
    try:
        project_id, slug = _project_and(args, "SLUG")
        result = _get_client().read_page(project_id, slug)
        if _state.get("json"):
            print_json(result)
            return
        render_page(result)
    except Exception as exc:
        _handle_error(exc)


@app.command("page")
@project_app.command("page")
def project_page(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    title: str = typer.Option(..., "--title"),
    body: Optional[str] = typer.Option(None, "--body", help="Inline markdown body (pass '-' to read stdin). Alternative to --body-file."),
    body_file: Optional[str] = typer.Option(None, "--body-file", help="Path to a markdown file for the page body (`-` for stdin)."),
    slug: Optional[str] = typer.Option(None, "--slug", help="Stable slug: an existing slug updates that page in place."),
    icon: Optional[str] = typer.Option(None, "--icon"),
    pinned: Optional[bool] = typer.Option(None, "--pinned/--unpinned", help="Must-read (pulled in full into every orient) or not. Omit to leave it unchanged.", show_default=False),
    role: Optional[str] = typer.Option(None, "--role", help="BRIEF|SPEC|PLAN|DECISION|REPORT|REFERENCE|NOTE (inferred from slug/title when omitted)."),
    if_version: Optional[int] = typer.Option(None, "--if-version", help="Page version you read: refuse (409) rather than overwrite a newer edit."),
) -> None:
    """Add or update a markdown page; the same slug updates it in place."""
    try:
        project_id = _project_arg(project)
    except Exception as exc:
        _handle_error(exc)
        return
    _upsert_page(
        project_id, title=title, body=body, body_file=body_file,
        slug=slug, icon=icon, pinned=pinned, role=role, if_version=if_version,
    )


def _upsert_page(
    project_id: int,
    *,
    title: str,
    body: Optional[str],
    body_file: Optional[str],
    slug: Optional[str],
    icon: Optional[str],
    pinned: Optional[bool],
    role: Optional[str] = None,
    if_version: Optional[int] = None,
) -> None:
    """The page upsert. `pinned=None` leaves an existing page's must-read state alone."""
    try:
        if bool(body) == bool(body_file):
            raise typer.BadParameter("Provide exactly one of --body or --body-file (either accepts - for stdin).")
        body_text = sys.stdin.read() if body == "-" else (body if body is not None else _read_file(body_file))  # type: ignore[arg-type]
        payload: dict = {"title": title, "body": body_text}
        if pinned is not None:
            payload["pinned"] = pinned
        if slug:
            payload["slug"] = slug
        if icon:
            payload["icon"] = icon
        if if_version is not None:
            payload["ifVersion"] = if_version
        if role:
            role_upper = role.strip().upper()
            if role_upper not in PAGE_ROLES:
                raise typer.BadParameter(f"--role must be one of {', '.join(PAGE_ROLES)} (got {role!r}).")
            payload["role"] = role_upper
        result = _get_client().upsert_page(project_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        report_index = not _state.get("quiet") or result.get("indexed") == "failed"
        render_page_written(result, _indexed_sentence(result) if report_index else None)
    except Exception as exc:
        _handle_error(exc)


@project_app.command("build")
def project_build(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    source: Optional[str] = typer.Option(None, "--source", help="Repo URL (defaults to the project's githubRepo)."),
) -> None:
    """Write: seed a thin project's context graph from its repository (README, artifacts, edges)."""
    try:
        result = _get_client().build_context(_project_arg(project), source)
        if _state.get("json") or not isinstance(result, dict) or not result.get("message"):
            print_json(result)
            return
        if not write_queued(result):
            console.print(result["message"], markup=False)
    except Exception as exc:
        _handle_error(exc)


SEARCH_TYPES = ("PROJECT", "PAGE", "RESOURCE", "SESSION_NOTE", "SIGNAL", "SUBMISSION", "DISCUSSION", "TASK")


def _search_types(value: Optional[str]) -> Optional[str]:
    """`pages,session-notes` → `PAGE,SESSION_NOTE`; unknown words pass through for the server to judge."""
    if not value:
        return value
    words = []
    for raw in value.split(","):
        word = raw.strip().upper().replace("-", "_").replace(" ", "_")
        if word not in SEARCH_TYPES and word.endswith("S") and word[:-1] in SEARCH_TYPES:
            word = word[:-1]
        if word:
            words.append(word)
    return ",".join(words)


@app.command("find")
def find(
    query: str = typer.Argument(help="Natural-language query."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Only this project (id or alias). Default: the whole lab."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="1-50 (default 8)."),
    team: Optional[str] = typer.Option(None, "--team", help="Only this team (slug)."),
    campaign: Optional[str] = typer.Option(None, "--campaign", help="Only this campaign (slug)."),
    user: Optional[str] = typer.Option(None, "--user", help="Only this person (handle)."),
    types: Optional[str] = typer.Option(None, "--types", "--type", help="Comma-separated: PROJECT,PAGE,RESOURCE,SESSION_NOTE,SIGNAL,SUBMISSION,DISCUSSION,TASK (case and plurals are forgiven)."),
    since: Optional[str] = typer.Option(None, "--since", help="Updated since: an ISO date, or 30d / 12h."),
) -> None:
    """Search the context graph by meaning, across the lab or one scope; each hit names its reader.

    Use one scope at most: -p, --team, --campaign, or --user.
    """
    try:
        other_scope = team or campaign or user
        if project and other_scope:
            raise typer.BadParameter("Use one scope at most: --project, --team, --campaign, or --user.")
        scope = None if other_scope else _explicit_project(project)
        result = _get_client().find_context(
            query, scope, limit, team=team, types=_search_types(types), since=since,
            campaign=campaign, user=user,
        )
        if _state.get("json"):
            print_json(result)
            return
        render_find(result, quiet=bool(_state.get("quiet")))
    except Exception as exc:
        _handle_error(exc)


# --- Context round-trip (binding-aware: project from --project/.moment.json) ---


@project_app.command("list")
@app.command("projects", hidden=True)
def projects(
    for_task: Optional[str] = typer.Option(None, "--for", help="Show which project would be selected for this task."),
    remote: bool = typer.Option(False, "--remote", help="List projects visible to the agent key instead of local bindings."),
    status: Optional[str] = typer.Option(None, "--status", help="Remote only: filter by project status."),
    mine: bool = typer.Option(False, "--mine", help="Remote only: show projects the key can edit."),
    after: Optional[str] = typer.Option(None, "--after", help="Remote only: continue from this cursor."),
) -> None:
    """List this repository's attached projects and explain the active choice."""
    try:
        if remote:
            payload = _get_client().list_projects_remote(
                status=status, mine=mine, after=after
            )
            if _state.get("json"):
                print_json(payload)
                return
            items = payload.get("items") or []
            if not items:
                console.print("No projects visible to this key.")
                return
            for item in items:
                counts = item.get("counts") or {}
                console.print(
                    f"[bold]{item.get('title') or 'Untitled project'}[/]  #{item.get('id')}"
                    f" · {item.get('status') or 'unknown'} · {item.get('access') or 'VIEWER'}"
                )
                console.print(
                    f"    [dim]{item.get('domain') or 'OTHER'} · "
                    f"{counts.get('openTasks', 0)} open tasks · {counts.get('members', 0)} members[/]"
                )
            if payload.get("nextCursor"):
                console.print(f"\nNext: moment project list --remote --after {payload['nextCursor']}")
            return
        if status or mine or after:
            raise typer.BadParameter("--status, --mine, and --after require --remote.")
        cfg = _resolve_cli_config(hint=for_task, require_api_key=False)
        selected = cfg.project_id
        # `alias` is a contract: whatever appears there must work as
        # `moment -p <alias>`. A legacy {"projectId": N} binding has no alias,
        # so it reports null there and carries the display name in `label`.
        payload = {
            "selected": {
                "id": selected,
                "alias": cfg.project_alias,
                "label": cfg.project_label,
                "reason": cfg.project_source,
            } if selected is not None else None,
            "projects": [
                {
                    "alias": item.alias,
                    "label": item.label,
                    "id": item.project_id,
                    "title": item.title,
                    "description": item.description,
                    "keywords": list(item.keywords),
                    "paths": list(item.paths),
                }
                for item in cfg.project_bindings
            ],
            "legacy": cfg.project_legacy,
            "config": cfg.config_path,
        }
        if _state.get("json"):
            print_json(payload)
            return
        if not cfg.project_bindings:
            console.print(
                f"[{ROSE}]No repository projects configured.[/] "
                "Run `moment init --project N --alias NAME --default`."
            )
            return
        for item in cfg.project_bindings:
            marker = "*" if item.project_id == selected else " "
            title = f" · {item.title}" if item.title else ""
            console.print(f"[{GLOW if marker == '*' else 'dim'}]{marker} {item.label}[/]  #{item.project_id}{title}")
            details = []
            if item.keywords:
                details.append("keywords: " + ", ".join(item.keywords))
            if item.paths:
                details.append("paths: " + ", ".join(item.paths))
            if details:
                console.print("    [dim]" + " · ".join(details) + "[/]")
        if cfg.project_legacy:
            console.print(
                f"[dim]Legacy `projectId` binding — no alias to address it with. "
                f"Run `moment init --project {cfg.project_bindings[0].project_id} --alias NAME` to migrate.[/]"
            )
        if selected is not None:
            console.print(f"\nSelected [bold]{cfg.project_label or ('#' + str(selected))}[/bold] — {cfg.project_source}.")
        else:
            console.print(
                f"\n[{ROSE}]Ambiguous.[/] Pass `--project <alias>` or set `defaultProject` in .moment.json."
            )
    except Exception as exc:
        _handle_error(exc)


@project_app.command("show")
def project_show(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """Show one project's metadata and current handoff headline."""
    try:
        project_id = _project_arg(project)
        payload = _get_client().get_project(project_id)
        if _state.get("json"):
            print_json(payload)
            return
        project = payload.get("project") or {}
        console.print(f"[bold]{project.get('title') or 'Untitled project'}[/] (#{project_id})")
        for label, key in (
            ("Status", "status"), ("Domain", "domain"), ("Visibility", "visibility"),
            ("Access", None), ("Tasks", "tasksEnabled"), ("Checklist", "checklistEnabled"),
        ):
            value = payload.get("access") if key is None else project.get(key)
            console.print(f"{label}: {value}")
        if project.get("description"):
            console.print(f"\n{project['description']}")
        handoff = payload.get("handoff") or {}
        if handoff.get("nextStep"):
            console.print(f"\nNext: {handoff['nextStep']}")
    except Exception as exc:
        _handle_error(exc)


@project_app.command("tasks")
def project_tasks(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    status: Optional[str] = typer.Option(None, "--status"),
    difficulty: Optional[str] = typer.Option(None, "--difficulty"),
    after: Optional[str] = typer.Option(None, "--after", help="Continue from this cursor."),
) -> None:
    """List one project's task board, including human-only and finished work."""
    try:
        project_id = _project_arg(project)
        payload = _get_client().list_project_tasks(
            project_id, status=status, difficulty=difficulty, after=after
        )
        if _state.get("json"):
            print_json(payload)
            return
        items = payload.get("items") or []
        if not items:
            console.print(f"No matching tasks on project #{project_id}.")
            return
        for item in items:
            console.print(
                f"#{item.get('id')}  [bold]{item.get('title') or 'Untitled task'}[/]"
                f" · {item.get('status')} · {item.get('difficulty')}"
            )
        if payload.get("nextCursor"):
            console.print(f"\nNext: moment project tasks {project_id} --after {payload['nextCursor']}")
    except Exception as exc:
        _handle_error(exc)


@project_app.command("set")
def project_set(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    title: Optional[str] = typer.Option(None, "--title"),
    description: Optional[str] = typer.Option(None, "--description"),
    domain: Optional[str] = typer.Option(None, "--domain"),
    tasks_enabled: Optional[bool] = typer.Option(None, "--tasks-enabled/--no-tasks-enabled"),
    checklist_enabled: Optional[bool] = typer.Option(None, "--checklist-enabled/--no-checklist-enabled"),
) -> None:
    """Change project metadata (EDITOR access required)."""
    payload = {
        key: value
        for key, value in {
            "title": title,
            "description": description,
            "domain": domain,
            "tasksEnabled": tasks_enabled,
            "checklistEnabled": checklist_enabled,
        }.items()
        if value is not None
    }
    try:
        if not payload:
            raise typer.BadParameter("Set at least one project field.")
        project_id = _project_arg(project)
        result = _get_client().update_project(project_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        console.print(
            f"Project #{project_id} updated: {', '.join(result.get('changed') or payload.keys())}."
        )
    except Exception as exc:
        _handle_error(exc)


@checklist_app.command("list")
def checklist_list(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """List a project's coordination checklist."""
    try:
        project_id = _project_arg(project)
        payload = _get_client().list_checklist(project_id)
        if _state.get("json"):
            print_json(payload)
            return
        if not payload.get("enabled"):
            console.print(f"Checklist is off for project #{project_id}.")
            return
        items = payload.get("items") or []
        if not items:
            console.print(f"No checklist items on project #{project_id}.")
            return
        for item in items:
            mark = "x" if item.get("done") else " "
            console.print(f"[{mark}] #{item.get('id')}  {item.get('text')}")
    except Exception as exc:
        _handle_error(exc)


@checklist_app.command("add")
def checklist_add(
    args: list[str] = typer.Argument(..., metavar="[PROJECT] TEXT", help="Optional project id or alias, then the item text."),
    url: Optional[str] = typer.Option(None, "--url"),
) -> None:
    """Add a coordination checklist item."""
    try:
        project_id, text = _project_and(args, "TEXT")
        result = _get_client().add_checklist_item(project_id, text, url)
        if _state.get("json"):
            print_json(result)
            return
        item = result.get("item") or {}
        console.print(f"Added checklist item #{item.get('id', '?')} to project #{project_id}.")
    except Exception as exc:
        _handle_error(exc)


@checklist_app.command("done")
def checklist_done(
    args: list[str] = typer.Argument(..., metavar="[PROJECT] ITEM_ID", help="Optional project id or alias, then the checklist item id."),
    undone: bool = typer.Option(False, "--undone", help="Mark the item not done."),
) -> None:
    """Mark a coordination checklist item done (or undo it)."""
    try:
        project_id, raw_item = _project_and(args, "ITEM_ID")
        item_id = _int_value(raw_item, "ITEM_ID")
        result = _get_client().update_checklist_item(project_id, item_id, {"done": not undone})
        if _state.get("json"):
            print_json(result)
            return
        console.print(f"Checklist item #{item_id} marked {'not done' if undone else 'done'}.")
    except Exception as exc:
        _handle_error(exc)


@checklist_app.command("remove")
def checklist_remove(
    args: list[str] = typer.Argument(..., metavar="[PROJECT] ITEM_ID", help="Optional project id or alias, then the checklist item id."),
) -> None:
    """Remove a coordination checklist item."""
    try:
        project_id, raw_item = _project_and(args, "ITEM_ID")
        item_id = _int_value(raw_item, "ITEM_ID")
        result = _get_client().remove_checklist_item(project_id, item_id)
        if _state.get("json"):
            print_json(result)
            return
        console.print(f"Removed checklist item #{item_id} from project #{project_id}.")
    except Exception as exc:
        _handle_error(exc)


@app.command("orient")
def orient(
    prompt: bool = typer.Option(False, "--prompt", help="Print the markdown brief even in a terminal."),
    for_task: Optional[str] = typer.Option(None, "--for", help="Select the most relevant attached project for this task."),
    complete: Optional[bool] = typer.Option(
        None, "--complete/--short",
        help="--short: next step, summary, what changed (~4 KB; the default when piped). "
             "--complete: everything, with the whole handoff and must-read pages (can pass 40 KB; "
             "the default in a terminal).",
        show_default=False,
    ),
    full: bool = typer.Option(False, "--full", hidden=True),
    view: Optional[str] = typer.Option(None, "--view", hidden=True, help="The server's view name: spinup, brief, or full."),
) -> None:
    """Print the project brief: the next step, summary, and what changed (--complete for everything).

    Piped (an agent), this prints the short markdown brief the MCP `orient` tool
    returns for view "spinup"; --complete matches view "brief". In a terminal it
    renders the complete brief as panels. --json returns the whole payload.
    """
    try:
        if view not in (None, "spinup", "brief", "full"):
            raise typer.BadParameter("--view must be one of spinup, brief, full; prefer --short or --complete.")
        client = _get_client(hint=for_task)
        pid = _resolve_project_id()
        piped = not stdout_is_rich() and not _state.get("json")
        # Agents read piped output through tool results that often show only
        # the first couple of KB: lead with the short view unless asked for more.
        if view:
            resolved_view: Optional[str] = view
        elif full:
            resolved_view = "full"
        elif complete is not None:
            resolved_view = "brief" if complete else "spinup"
        else:
            resolved_view = "spinup" if piped or prompt else None
        result = client.get_project_context(pid, view=resolved_view)
        brief = result.get("promptContext")
        if _state.get("json") and not prompt:
            print_json(result)
            return
        if isinstance(brief, str) and brief.strip() and (prompt or piped):
            sys.stdout.write(brief.rstrip() + "\n")
            if not _state.get("quiet"):
                sys.stdout.write(_orient_footer(result, resolved_view) + "\n")
            return
        render_brief(result, quiet=bool(_state.get("quiet")))
    except Exception as exc:
        _handle_error(exc)


def _orient_footer(result: dict, view: Optional[str]) -> str:
    handoff = result.get("handoff") or {}
    parts = []
    if view in (None, "spinup"):
        parts.append("everything: moment orient --complete")
    parts += ["read a page: moment page-read <slug>", 'search: moment find "<question>"']
    if handoff.get("version") is not None:
        parts.append(f"handoff v{handoff['version']}")
    return "— " + " · ".join(parts)


def _status_instant(value: object) -> Optional[str]:
    parsed = parse_iso(value)
    if parsed is None:
        return value.strip() if isinstance(value, str) and value.strip() else None
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _status_optional(call, section: str, skipped: list[str]):
    """A credential mismatch omits one section; other API failures still fail."""
    try:
        return call()
    except ApiError as exc:
        if exc.status in (401, 403):
            skipped.append(section)
            return None
        raise


def _active_campaign(cfg, project_id: int) -> Optional[str]:
    user_id = (cfg.human_user or {}).get("id") or "token"
    key = campaign_context_key(cfg.api_url, user_id, project_id)
    return cfg.campaign_context.get(key)


def _status_payload(cfg, project_id: int) -> dict:
    context = claims = whats_new = None
    skipped: list[str] = []
    if cfg.api_key:
        agent_client = MomentApiClient(cfg)
        context = _status_optional(
            lambda: agent_client.get_project_context(project_id), "project", skipped
        )
        claims = _status_optional(agent_client.get_my_claims, "myClaims", skipped)
        whats_new = _status_optional(agent_client.get_whats_new, "sinceYouWereHere", skipped)

    bound = next(
        (item for item in cfg.project_bindings if item.project_id == project_id),
        None,
    )
    remote_project = (context or {}).get("project") or {}
    project = {
        "id": project_id,
        "title": remote_project.get("title") or (bound.title if bound else None),
        "status": remote_project.get("status"),
    }

    handoff = (context or {}).get("handoff") or {}
    freshness = (context or {}).get("contextFreshness") or {}
    brief = None
    if context is not None:
        brief = {
            "updatedAt": handoff.get("updatedAt") or freshness.get("handoffUpdatedAt"),
            "version": handoff.get("version"),
            "tasksCompletedSince": freshness.get("tasksCompletedSince", 0),
        }

    campaign = _active_campaign(cfg, project_id)
    submission = None
    if campaign and cfg.human_token:
        human_client = MomentApiClient(cfg, human=True)
        submission = _status_optional(
            lambda: human_client.get_submission(project_id, campaign), "submission", skipped
        )

    return {
        "project": project,
        "brief": brief,
        "nextStep": handoff.get("nextStep") if context is not None else None,
        "myClaims": claims,
        "sinceYouWereHere": whats_new,
        "submission": submission,
        "activeCampaign": campaign,
        "skipped": skipped,
    }


def _render_status(payload: dict) -> None:
    project = payload["project"]
    title = project.get("title") or "Untitled project"
    state = (
        "— (not available with this key)"
        if "project" in payload.get("skipped", [])
        else project.get("status") or "status unavailable"
    )
    console.print(f"[bold]{title}[/] (#{project['id']}) · {state}")

    brief = payload.get("brief")
    if brief is not None:
        version = f"v{brief['version']}" if brief.get("version") is not None else "unversioned"
        updated = _status_instant(brief.get("updatedAt")) or "no handoff yet"
        completed = int(brief.get("tasksCompletedSince") or 0)
        console.print(
            f"Brief: {version} · {updated} · {completed} task"
            f"{'s' if completed != 1 else ''} completed since handoff"
        )
    elif "project" in payload.get("skipped", []):
        console.print("Brief: — (not available with this key)")

    if payload.get("nextStep"):
        console.print(f"Next: {payload['nextStep']}")

    claims = payload.get("myClaims")
    if claims is not None:
        console.print(
            "My claims: "
            f"{claims.get('active', 0)} active · "
            f"{claims.get('submitted', 0)} submitted · "
            f"{claims.get('recentlyDecided', 0)} recently decided"
        )
    elif "myClaims" in payload.get("skipped", []):
        console.print("My claims: — (not available with this key)")

    changed = payload.get("sinceYouWereHere")
    if changed is not None:
        feed = changed.get("feed") or []
        console.print(
            f"Since you were here, across your projects: {len(feed)} project"
            f"{'s' if len(feed) != 1 else ''} changed"
        )
        for item in feed[:3]:
            label = item.get("title") or item.get("name") or f"project {item.get('projectId', '?')}"
            total = item.get("changeCount")
            if not isinstance(total, int):
                counts = item.get("counts") or (item.get("delta") or {}).get("counts") or {}
                total = sum(value for value in counts.values() if isinstance(value, int))
            console.print(f"  - {label} (#{item.get('projectId', '?')}): {total} change{'s' if total != 1 else ''}")
    elif "sinceYouWereHere" in payload.get("skipped", []):
        console.print("Since you were here: — (not available with this key)")

    submission = payload.get("submission")
    if submission is not None:
        missing = submission.get("missing") or []
        console.print(
            f"Submission: {submission.get('status') or 'unknown'}"
            f" · {len(missing)} requirement{'s' if len(missing) != 1 else ''} missing"
        )
    elif "submission" in payload.get("skipped", []):
        console.print("Submission: — (not available with this key)")

    if payload.get("activeCampaign"):
        console.print(f"Active campaign: {payload['activeCampaign']}")


@app.command("status")
def status() -> None:
    """Show where the bound project stands in one screen."""
    try:
        cfg = _resolve_cli_config(require_api_key=False)
        if cfg.project_id is None:
            raise typer.BadParameter(NO_PROJECT_MESSAGE)
        payload = _status_payload(cfg, cfg.project_id)
        if _state.get("json"):
            print_json(payload)
            return
        _render_status(payload)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle_error(exc)


def _claim_id(value: str) -> int:
    """Accept the numeric id and the numeric tail of a Moment claim URN."""
    text = value.strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"(?:^|[/:#])([0-9]+)$", text)
    if match:
        return int(match.group(1))
    raise typer.BadParameter("Expected a numeric claim id or a Moment claim URN.")


def _record_actor(event: dict) -> str:
    actor = event.get("actor") or {}
    name = actor.get("name") or "unknown actor"
    if actor.get("isAgent"):
        operator = actor.get("operator")
        return f"{name} (agent, for {operator})" if operator else f"{name} (agent)"
    return str(name)


def _record_line(event: dict) -> tuple[str, str]:
    parsed = parse_iso(event.get("at"))
    if parsed is None:
        day, clock = "unknown date", "??:??"
    else:
        instant = parsed.astimezone(timezone.utc)
        day, clock = instant.strftime("%Y-%m-%d"), instant.strftime("%H:%M")
    snippet = str(event.get("snippet") or "").replace("\n", " ").strip()
    title = str(event.get("title") or "Untitled event")
    line = f"{clock}  {_record_actor(event)}  {title}"
    if snippet:
        line += f"  — {snippet}"
    return day, line


def _render_record_pages(pages: list[dict]) -> None:
    previous_day: Optional[str] = None
    for page in pages:
        for event in page.get("items") or page.get("events") or []:
            day, line = _record_line(event)
            if day != previous_day:
                if previous_day is not None:
                    sys.stdout.write("\n")
                sys.stdout.write(f"{day}\n")
                previous_day = day
            sys.stdout.write(f"{line}\n")


def _record_pages(
    client: MomentApiClient,
    *,
    project_id: Optional[int],
    campaign_slug: Optional[str],
    limit: Optional[int],
    all_pages: bool,
    since: Optional[str],
    kinds: Optional[list[str]],
    actor: Optional[str],
) -> list[dict]:
    pages: list[dict] = []
    after: Optional[str] = None
    while True:
        page = client.get_record(
            project_id=project_id,
            campaign_slug=campaign_slug,
            limit=limit,
            after=after,
            kinds=kinds,
            actor=actor,
            since=since,
        )
        pages.append(page)
        if not all_pages or not page.get("nextCursor"):
            return pages
        next_after = page.get("nextCursor")
        if not isinstance(next_after, str) or next_after == after:
            return pages
        after = next_after


def _record_scope(project: Optional[str], campaign_slug: Optional[str]) -> Optional[int]:
    """The project whose record to read; None when a campaign was named instead."""
    if project and campaign_slug:
        raise typer.BadParameter("Name a project or a campaign, not both.")
    if campaign_slug:
        return None
    return _project_arg(project)


@app.command("record")
def record(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id or alias (default: -p, else the repo binding)."),
    campaign_slug: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug, instead of a project."),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="Events per page."),
    all_pages: bool = typer.Option(False, "--all", help="Follow nextCursor through the full record."),
    since: Optional[str] = typer.Option(None, "--since", help="Only events at or after this ISO instant."),
    kinds: Optional[list[str]] = typer.Option(None, "--kind", help="Filter by event kind; repeatable."),
    actor: Optional[str] = typer.Option(None, "--actor", help="Filter by actor: human or agent."),
) -> None:
    """Read a project or campaign record, newest first."""
    try:
        if actor not in (None, "human", "agent"):
            raise typer.BadParameter("--actor must be human or agent.")
        project_id = _record_scope(project, campaign_slug)
        pages = _record_pages(
            _get_client(),
            project_id=project_id,
            campaign_slug=campaign_slug,
            limit=limit,
            all_pages=all_pages,
            since=since,
            kinds=kinds,
            actor=actor,
        )
        if _state.get("json"):
            print_json(pages[0] if len(pages) == 1 else {"pages": pages})
            return
        _render_record_pages(pages)
    except Exception as exc:
        _handle_error(exc)


@app.command("watch")
def watch(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id or alias (default: -p, else the repo binding)."),
    campaign_slug: Optional[str] = typer.Option(None, "--campaign", help="Campaign slug, instead of a project."),
    every: int = typer.Option(30, "--every", min=1, help="Polling interval in seconds."),
    limit: int = typer.Option(50, "--limit", min=1, help="Events per poll."),
) -> None:
    """Poll a record and print events as they arrive. Runs until interrupted; not for agent sessions."""
    try:
        project_id = _record_scope(project, campaign_slug)
    except Exception as exc:
        _handle_error(exc)
        return
    client = _get_client()
    # `latestCursor` is the forward cursor. Passing it back as `before` returns
    # only events that arrived since the previous poll; `after` is exclusively
    # the backwards/older-page cursor used by `moment record --all`.
    before: Optional[str] = None
    try:
        while True:
            page = client.get_record(
                project_id=project_id,
                campaign_slug=campaign_slug,
                limit=limit,
                after=None,
                before=before,
            )
            events = page.get("items") or page.get("events") or []
            latest = page.get("latestCursor")
            if isinstance(latest, str) and latest:
                before = latest
            if events:
                _render_record_pages([{**page, "items": list(reversed(events))}])
            time.sleep(every)
    except KeyboardInterrupt:
        return
    except Exception as exc:
        _handle_error(exc)


def _citation_parts(kind: str, identifier: str, client: MomentApiClient) -> tuple[str, str, str]:
    if kind == "project":
        project_id = int(identifier)
        result = client.get_project_context(project_id)
        title = str((result.get("project") or {}).get("title") or f"Project {project_id}")
        url = f"{client.config.api_url}/projects/{project_id}"
        key = f"moment_project_{project_id}"
    elif kind == "campaign":
        result = client.get_agent_campaign(identifier)
        campaign = result.get("campaign") or result
        title = str(campaign.get("title") or identifier)
        url = f"{client.config.api_url}/campaigns/{identifier}"
        key = f"moment_campaign_{identifier}"
    else:
        signal_id = _claim_id(identifier)
        result = client.get_claim_state(signal_id)
        claim = result.get("claim") or result.get("signal") or result
        title = str(claim.get("content") or claim.get("title") or f"Claim {identifier}")
        public_id = claim.get("publicId") or identifier
        url = f"{client.config.api_url}/claims/{public_id}"
        key = f"moment_claim_{signal_id}"
    return title, url, key


@app.command("cite")
def cite(
    kind: str = typer.Argument(help="One of project, campaign, or claim."),
    identifier: str = typer.Argument(help="Project id, campaign slug, or claim URN/id."),
    bibtex: bool = typer.Option(False, "--bibtex", help="Print the BibTeX citation."),
) -> None:
    """Print the stable Moment citation for a project, campaign, or claim."""
    kind = kind.lower()
    try:
        if kind not in {"project", "campaign", "claim"}:
            raise typer.BadParameter("Kind must be project, campaign, or claim.")
        client = _get_client()
        title, url, key = _citation_parts(kind, identifier, client)
        accessed = datetime.now(timezone.utc).date().isoformat()
        text = f"{title}. OSI — recorded on Moment. {url}"
        bib = (
            f"@misc{{{key},\n"
            f"  title = {{{title}}},\n"
            "  howpublished = {OSI — recorded on Moment},\n"
            f"  url = {{{url}}},\n"
            f"  note = {{Accessed {accessed}}}\n"
            "}"
        )
        if _state.get("json"):
            print_json({"title": title, "url": url, "key": key, "text": text, "bibtex": bib})
        else:
            sys.stdout.write((bib if bibtex else text) + "\n")
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
        client = _get_client(hint=task or page)
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


def _packet_permalink(meta: dict) -> str:
    """The stable project URL a saved snapshot lives at."""
    cfg = _state.get("_config")
    base = _default_api_url(getattr(cfg, "api_url", None))
    return f"{base}/projects/{meta.get('projectId')}/packets/{meta.get('id')}"


@app.command("packet")
def packet(
    question: str = typer.Argument(help="The question the evidence should address."),
    budget: Optional[int] = typer.Option(None, "--budget", help="Approx token budget for the packet (default 4000)."),
    max_nodes: Optional[int] = typer.Option(None, "--max-nodes", help="Hard cap on evidence items (default 40, max 100)."),
    depth: Optional[int] = typer.Option(None, "--depth", help="Graph hops to expand (1-3, default 2)."),
    save: bool = typer.Option(False, "--save", help="Freeze the compiled packet as an immutable snapshot (needs edit access)."),
) -> None:
    """Compile a Research Context Packet — inspectable evidence for one question, each item carrying authority, freshness, locator, and why it matched."""
    try:
        client = _get_client(hint=question)
        pid = _resolve_project_id()
        payload: dict = {"question": question}
        if budget is not None:
            payload["tokenBudget"] = budget
        if max_nodes is not None:
            payload["maxNodes"] = max_nodes
        if depth is not None:
            payload["depth"] = depth
        result = client.compile_packet(pid, payload)
        saved = client.save_packet(pid, result) if save else None
        if _state.get("json"):
            print_json({"packet": result, "saved": saved} if save else result)
            return
        render_packet(result, quiet=bool(_state.get("quiet")))
        if saved:
            render_saved_packet(saved, _packet_permalink(saved))
    except Exception as exc:
        _handle_error(exc)


@app.command("packet-list")
def packet_list(
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max snapshots to list (default 20, max 50)."),
) -> None:
    """List the saved packet snapshots on this project, newest first."""
    try:
        client = _get_client()
        pid = _resolve_project_id()
        result = client.list_packets(pid, limit)
        if _state.get("json"):
            print_json(result)
            return
        render_packet_list(result.get("packets") or [])
    except Exception as exc:
        _handle_error(exc)


@app.command("packet-get")
def packet_get(
    packet_id: int = typer.Argument(help="Saved packet ID (from `packet-list`)."),
) -> None:
    """Read one saved packet snapshot in full — its frozen evidence, exactly as cited."""
    try:
        client = _get_client()
        pid = _resolve_project_id()
        result = client.get_packet(pid, packet_id)
        if _state.get("json"):
            print_json(result)
            return
        render_saved_packet(result, _packet_permalink(result), title="Packet snapshot")
        render_packet(result.get("packet") or {}, quiet=bool(_state.get("quiet")))
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
@project_app.command("log")
def log(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    note: Optional[str] = typer.Option(None, "--note", help="What changed, what you verified, what to watch. `-` reads stdin."),
    note_file: Optional[str] = typer.Option(None, "--note-file", help="Read the note from a file (`-` for stdin)."),
    next_step: Optional[str] = typer.Option(None, "--next", "--next-step", help="The single next concrete step for the following session."),
    task: Optional[int] = typer.Option(None, "--task", help="Task ID this close-out followed."),
    works_now: Optional[str] = typer.Option(None, "--works-now", help="Handoff: what works now (verified)."),
    in_progress: Optional[str] = typer.Option(None, "--in-progress", help="Handoff: work in flight."),
    blocked: Optional[str] = typer.Option(None, "--blocked", help="Handoff: what is blocked, and on what."),
    watch_out: Optional[str] = typer.Option(None, "--watch-out", help="Handoff: the trap or friction the next agent should avoid."),
    tried_and_failed: Optional[list[str]] = typer.Option(None, "--tried-and-failed", help="Handoff: an approach that failed and why. Appends; repeatable."),
    decisions: Optional[list[str]] = typer.Option(None, "--decisions", help="Handoff: a decision made. Appends; repeatable."),
    open_questions: Optional[list[str]] = typer.Option(None, "--open-questions", help="Handoff: a question still open. Appends; repeatable."),
    read_first: Optional[list[str]] = typer.Option(None, "--read-first", help="Handoff: something to read first, as 'LABEL' or 'LABEL :: why'. Repeatable; replaces the whole list."),
    no_tree: bool = typer.Option(False, "--no-tree", help="Do not append the working-tree fingerprint to the note."),
    commits: Optional[str] = typer.Option(None, "--commits", help="Git range, e.g. abc123..def456 — appends a commit summary to the note."),
    if_version: Optional[int] = typer.Option(None, "--if-version", help="Handoff version you read: refuse (and retry once) rather than overwrite a newer one."),
    sync: bool = typer.Option(False, "--sync", hidden=True, help="Also write the optional .continuity/ snapshot."),
) -> None:
    """Close out a session: log a note, and patch only the handoff fields you pass.

    `moment log` and `moment project log` are the same command.
    """
    try:
        if bool(note) == bool(note_file):
            raise typer.BadParameter("Provide exactly one of --note or --note-file (either accepts - for stdin).")
        note_text = _read_file("-") if note == "-" else (note if note else _read_file(note_file))  # type: ignore[arg-type]
        if not note_text:
            raise typer.BadParameter("The note is empty.")
        if len(note_text) > NOTE_MAX_CHARS:
            raise typer.BadParameter(
                f"The note is {len(note_text)} characters; the server accepts {NOTE_MAX_CHARS}. "
                "Put the detail in a page (`moment page --title … --body -`) and summarize it here."
            )
        # Append git truth to the NOTE only (never the handoff fields): a mechanical
        # working-tree fingerprint (unless --no-tree) and, when asked, a commit summary.
        # Each is dropped rather than letting it push the note past the server's limit.
        appendices = []
        if not no_tree:
            appendices.append(close_out.tree_fingerprint())
        if commits:
            appendices.append(close_out.commits_summary(commits))
        for block in appendices:
            if block and len(note_text) + 2 + len(block) <= NOTE_MAX_CHARS:
                note_text = f"{note_text}\n\n{block}"
        payload = _build_session_payload(
            note_text, next_step, task,
            works_now=works_now, in_progress=in_progress, blocked=blocked, watch_out=watch_out,
            tried_and_failed="\n".join(tried_and_failed or []) or None,
            decisions="\n".join(decisions or []) or None,
            open_questions="\n".join(open_questions or []) or None,
            read_first=_read_first_items(read_first),
            if_version=if_version,
        )
        client = _get_client()
        pid = _project_arg(project)
        result = _log_session_with_retry(client, pid, payload)
        if _state.get("json"):
            print_json(result)
        else:
            note_id = (result.get("sessionNote") or {}).get("id")
            line = result.get("message") or "Session note logged."
            console.print(f"[{GLOW}]{escape(line)}[/]" + (f" [dim](note #{note_id}, project #{pid})[/]" if note_id else ""))
            # --quiet drops tips, never a failure: a dead embedding provider still says so.
            quiet = _state.get("quiet")
            extras = (
                None if quiet else result.get("hint"),
                _indexed_sentence(result) if not quiet or result.get("indexed") == "failed" else None,
            )
            for extra in extras:
                if extra:
                    console.print(f"[dim]{escape(str(extra))}[/]")
        if sync:
            _run_sync(pid, client, check=False, out=None)
    except Exception as exc:
        _handle_error(exc)


@app.command("sync")
def sync(
    check: bool = typer.Option(False, "--check", help="Render and diff vs disk; exit nonzero on drift (no writes)."),
    out: Optional[str] = typer.Option(None, "--out", help="Target dir (default: <repo>/.continuity)."),
) -> None:
    """Optional: write a local .continuity/ snapshot of the brief. Moment itself stays the source of truth."""
    try:
        client = _get_client()
        pid = _resolve_project_id()
        _run_sync(pid, client, check=check, out=out)
    except Exception as exc:
        _handle_error(exc)


@app.command("doctor")
def doctor() -> None:
    """Diagnose the binding in one screen — config, key, reachability, auth, project, MCP drift."""
    results = doctor_mod.run_doctor(
        config_path=_state.get("config"),
        api_url_override=_state.get("api_url"),
    )
    failed = [r for r in results if r.status == "fail"]

    if _state.get("json"):
        print_json([{"name": r.name, "status": r.status, "detail": r.detail} for r in results])
        if failed:
            raise typer.Exit(1)
        return

    tokens = {"ok": "ok", "fail": "FAIL", "skip": "skip"}
    colors = {"ok": GLOW, "fail": ROSE, "skip": "grey50"}
    name_w = max((len(r.name) for r in results), default=0)
    for r in results:
        token = tokens.get(r.status, r.status)
        color = colors.get(r.status, "white")
        console.print(f"  [{color}]{token:>4}[/]  {escape(r.name.ljust(name_w))}  [dim]{escape(r.detail)}[/]")

    if failed:
        names = ", ".join(r.name for r in failed)
        console.print(f"\n  [{ROSE}]{len(failed)} check(s) failed: {escape(names)}.[/]")
    else:
        skipped = sum(1 for r in results if r.status == "skip")
        tail = f" ({skipped} skipped)" if skipped else ""
        console.print(f"\n  [{GLOW}]Binding healthy — all checks passed{tail}.[/]")

    if failed:
        raise typer.Exit(1)


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
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id or alias (default: MOMENT_PROJECT, else .moment.json)."),
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
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id or alias (default: MOMENT_PROJECT, else .moment.json)."),
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


def _repo_binding_api_url(root: Path, override: Optional[str] = None) -> str:
    """Preserve a repository's server unless the user explicitly replaces it."""
    existing = _read_repo_binding(root).get("apiUrl")
    repo_url = existing if isinstance(existing, str) and existing.strip() else None
    return _default_api_url(override or repo_url)


def _repo_bindings(root: Path):
    """Bindings for this repo — registry or legacy — via the shared reader."""
    try:
        return read_repo_bindings(_read_repo_binding(root))
    except SystemExit:
        # A malformed value (e.g. projectId: "forty-two") is "not bound" for the
        # purposes of the callers here; resolve_config still reports it loudly.
        return read_repo_bindings({})


def _bound_project_id(root: Path) -> Optional[int]:
    """The project a bare command would use — legacy bindings included.

    `onboard` gates on this: miss the legacy shape and it treats an attached
    repo as fresh and bootstraps a duplicate remote project.
    """
    found = _repo_bindings(root)
    if not found.bindings:
        return None
    if found.default_alias:
        entry = next(
            (
                item for item in found.bindings
                if item.alias and item.alias.casefold() == found.default_alias.casefold()
            ),
            None,
        )
        if entry is not None:
            return entry.project_id
    if len(found.bindings) == 1:
        return found.bindings[0].project_id
    return None


def _binding_alias(value: Optional[str], project_id: int) -> str:
    alias = re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")
    return alias or f"project-{project_id}"


def _entry_project_id(raw: object) -> Optional[int]:
    value = raw.get("id") if isinstance(raw, dict) else raw
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _legacy_alias_migration(data: dict, projects: dict, project_id: int) -> Optional[str]:
    """Move a legacy `{"projectId": N}` binding into the projects registry.

    Called before the new binding is added: attaching a second project used to
    silently drop the legacy one (`data.pop("projectId")` with nothing written
    in its place), losing a working attachment.
    """
    try:
        legacy_id = read_repo_bindings(data).legacy_project_id
    except SystemExit:
        return None  # unusable value; the pop below is a cleanup, not a loss
    if legacy_id is None or legacy_id == project_id:
        return None
    if any(_entry_project_id(raw) == legacy_id for raw in projects.values()):
        return None
    alias = _binding_alias(None, legacy_id)
    while alias in projects:
        alias = f"{alias}-legacy"
    projects[alias] = {"id": legacy_id}
    if not isinstance(data.get("defaultProject"), str):
        # The legacy binding WAS the repository default; keep it that way unless
        # the caller explicitly promotes the new project.
        data["defaultProject"] = alias
    return alias


def _write_repo_binding(
    root: Path,
    project_id: int,
    api_url: str,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    keywords: Optional[list[str]] = None,
    paths: Optional[list[str]] = None,
    make_default: bool = False,
    rebind: bool = False,
) -> tuple[Path, bool]:
    moment_json = _repo_binding_path(root)
    data = _read_repo_binding(root)
    had_key = isinstance(data.get("apiKey"), str)
    data.pop("apiKey", None)  # never store a secret in the committed binding file
    data.pop("apiKeySource", None)  # provenance belongs beside the key in HOME

    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    binding_alias = _binding_alias(alias or title, project_id)
    _legacy_alias_migration(data, projects, project_id)
    data.pop("projectId", None)
    data["apiUrl"] = api_url

    # An alias is a name the user types; quietly repointing it at another
    # project (and moving defaultProject with it) is how you end up writing to
    # the wrong project. Make it an explicit choice.
    existing = projects.get(binding_alias)
    existing_id = _entry_project_id(existing)
    if existing_id is not None and existing_id != project_id and not rebind:
        raise SystemExit(
            f"Alias {binding_alias!r} is already attached to project #{existing_id}. "
            f"Choose a different --alias, or pass --rebind to point {binding_alias!r} "
            f"at project #{project_id} instead."
        )

    # One project id has one local alias. Re-attaching it under a new alias is a
    # rename, not a duplicate association.
    for existing_alias, raw in list(projects.items()):
        if existing_alias == binding_alias:
            continue
        if _entry_project_id(raw) == project_id:
            projects.pop(existing_alias)
            if data.get("defaultProject") == existing_alias:
                data["defaultProject"] = binding_alias

    # On a rebind the old entry describes a different project — don't inherit
    # its title/keywords/paths.
    entry = dict(existing) if isinstance(existing, dict) and existing_id == project_id else {}
    entry["id"] = project_id
    if title:
        entry["title"] = title.strip()
    if description:
        entry["description"] = description.strip()
    if keywords:
        entry["keywords"] = list(dict.fromkeys(item.strip() for item in keywords if item.strip()))
    if paths:
        entry["paths"] = list(dict.fromkeys(
            item.replace("\\", "/").strip().removeprefix("./").rstrip("/") or "."
            for item in paths if item.strip()
        ))
    projects[binding_alias] = entry
    data["projects"] = projects
    if make_default or not isinstance(data.get("defaultProject"), str):
        data["defaultProject"] = binding_alias

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
        "pinned": True,
    }


def _watermark_base_sha(source_ref: Optional[str]) -> Optional[str]:
    """The newest captured sha out of 'repo@a..b' / 'repo@a' (None for @worktree)."""
    if not source_ref or "@" not in source_ref:
        return None
    ref = source_ref.rsplit("@", 1)[1]
    if ref == "worktree":
        return None
    sha = ref.split("..")[-1].strip()
    # The watermark comes from the server and lands in a git argv: a hex sha only.
    return sha if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha) else None


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
            args.append(f"--since={since}")
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
        client = _get_client(hook_mode=hook)
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
            if hook:
                return
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
        if _state.get("json") and not hook:
            print_json(result)
        elif not _state.get("quiet") and not hook:
            if result.get("skipped"):
                console.print(f"[dim]{result.get('reason', 'Skipped.')}[/dim]")
            else:
                console.print(f"[{GLOW}]{result.get('message', 'Captured.')}[/]")
    except Exception as exc:
        if hook:
            # A Stop hook must never break the session it closes out.
            if isinstance(exc, ApiError) and exc.status not in (401, 429):
                print(f"moment capture (hook): {exc}", file=sys.stderr)
            elif not isinstance(exc, (ApiError, httpx.RequestError, httpx.TimeoutException)):
                print(f"moment capture (hook): {exc}", file=sys.stderr)
            return
        _handle_error(exc)


CLAUDE_START = "<!-- moment:autostart:start -->"
CLAUDE_END = "<!-- moment:autostart:end -->"


@app.command("init")
def init(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id (or an alias already in .moment.json) to attach. Required."),
    alias: Optional[str] = typer.Option(None, "--alias", help="Memorable local name (default: project-N)."),
    title: Optional[str] = typer.Option(None, "--title", help="Human-readable project title used for matching."),
    description: Optional[str] = typer.Option(None, "--description", help="Short note describing when this project is relevant."),
    keyword: Optional[list[str]] = typer.Option(None, "--keyword", help="Matching keyword; repeat for several."),
    path: Optional[list[str]] = typer.Option(None, "--path", help="Repo-relative path owned by this project; repeat for several."),
    default: bool = typer.Option(False, "--default", help="Make this the repository fallback."),
    rebind: bool = typer.Option(
        False,
        "--rebind",
        help="Allow --alias to be repointed when it already names a different project (otherwise that's an error).",
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="API URL to bind (default: https://scientificintelligence.org)."),
    no_claude: bool = typer.Option(False, "--no-claude", help="Don't touch CLAUDE.md."),
    no_hooks: bool = typer.Option(False, "--no-hooks", help="Don't install capture hooks for detected runtimes (ambient capture)."),
) -> None:
    """Attach this repo to a Moment project (writes .moment.json). Repeat to attach more than one.

    Local routing only: nothing on the server changes. Agents should pass
    --no-claude --no-hooks unless the user asked for startup instructions or
    ambient capture.
    """
    try:
        selector = project or _state.get("project")
        if not selector:
            raise typer.BadParameter("moment init needs the project: -p <project id>.")
        project_id = _project_arg(str(selector))
    except Exception as exc:
        _handle_error(exc)
        return
    if api_url:
        _state["api_url"] = api_url
    json_mode = bool(_state.get("json"))
    say = (lambda _message: None) if json_mode else console.print
    _mint_agent_key_if_needed()
    root = _repo_root()
    url = _repo_binding_api_url(root, api_url)
    binding_alias = _binding_alias(alias or title, project_id)
    legacy_before = _repo_bindings(root).legacy_project_id
    moment_json, had_key = _write_repo_binding(
        root,
        project_id,
        url,
        alias=binding_alias,
        title=title,
        description=description,
        keywords=keyword,
        paths=path,
        make_default=default,
        rebind=rebind,
    )
    # Naming the server on the command line is the person's choice of it.
    newly_trusted = api_origin(url) if api_url and trust_api_url(url) else None
    if newly_trusted:
        say(f"[{GLOW}]Trusted[/] {newly_trusted} for your credentials [dim](trustedApiUrls in ~/.moment.json)[/]")
    migrated = legacy_before is not None and legacy_before != project_id
    if migrated:
        say(
            f"[{GLOW}]Migrated[/] legacy projectId #{legacy_before} → alias "
            f"{_binding_alias(None, legacy_before)} [dim](still the repository default unless --default)[/]"
        )
    say(f"[{GLOW}]Attached[/] {binding_alias} → project #{project_id} in {moment_json}")
    if had_key:
        say(
            f"[{ROSE}]Removed apiKey from {moment_json.name}[/] — keep keys in "
            "~/.moment.json or $MOMENT_API_KEY (this file is committed)."
        )

    claude_path = None
    if not no_claude:
        claude_path = root / "CLAUDE.md"
        _upsert_claude_block(claude_path, _claude_block())
        say(f"[{GLOW}]Updated[/] {claude_path} [dim](autostart block)[/]")

    hook_results = None if no_hooks else _install_capture_hooks(quiet=json_mode)

    if json_mode:
        bindings = _repo_bindings(root)
        print_json({
            "alias": binding_alias,
            "projectId": project_id,
            "config": str(moment_json),
            "apiUrl": url,
            "trustedApiUrl": newly_trusted,
            "default": bindings.default_alias == binding_alias,
            "migratedLegacyProjectId": legacy_before if migrated else None,
            "removedApiKey": had_key,
            "claudeMd": str(claude_path) if claude_path else None,
            "hooks": hook_results,
        })
        return
    say(
        "[dim]Next: `moment project list --for \"your task\"` to check routing · "
        "`moment orient --for \"your task\"` to read the likely brief[/]"
    )


def _install_capture_hooks(*, quiet: bool = False) -> list[dict]:
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
        if quiet:
            continue
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


def _claude_block() -> str:
    return (
        f"{CLAUDE_START}\n"
        "## Moment context\n\n"
        "This repo is attached to one or more Moment projects (see `.moment.json`). Moment holds "
        "the shared project context: the handoff, pages, resources, and session notes.\n\n"
        "**Orient** before substantial work: `moment orient --for \"<the task>\"` prints the next step "
        "and summary (`--complete` for everything). "
        "Read a page the brief names with `moment page-read <slug>`; search with `moment find \"<question>\"`.\n\n"
        "**Close out** after substantive work: `moment log --note \"<what changed and what you verified>\" "
        "--next \"<the next concrete step>\"`, adding `--works-now`, `--in-progress`, `--blocked`, or "
        "`--watch-out` only for handoff fields that changed.\n\n"
        "A Stop hook may run `moment capture --hook` to log git drift automatically; a deliberate "
        "`moment log` is still better when you have judgment to record. `moment commands` lists every "
        "command and option. Keep API keys in `~/.moment.json` or `MOMENT_API_KEY`, never in `.moment.json`.\n"
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
    project_ref: Optional[str] = typer.Option(None, "--project", "-p", help="Bind to an existing project (id or alias) instead of creating one."),
    alias: Optional[str] = typer.Option(None, "--alias", help="Local alias when attaching an existing project."),
    title: Optional[str] = typer.Option(None, "--title", help="Project title when creating/bootstrapping."),
    description: Optional[str] = typer.Option(None, "--description", help="Project description when creating/bootstrapping."),
    description_file: Optional[str] = typer.Option(None, "--description-file", help="Read project description from a file."),
    domain: Optional[str] = typer.Option(None, "--domain", "-d", help="Research domain (default server value)."),
    visibility: Optional[str] = typer.Option(None, "--visibility", help="PUBLIC or PRIVATE (default server value)."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="API URL to bind (default: https://scientificintelligence.org)."),
    no_claude: bool = typer.Option(False, "--no-claude", help="Don't touch CLAUDE.md."),
    no_hooks: bool = typer.Option(False, "--no-hooks", help="Don't install capture hooks for detected runtimes."),
    default: bool = typer.Option(False, "--default", help="Make an attached existing project the repository fallback."),
    rebind: bool = typer.Option(
        False,
        "--rebind",
        help="Allow --alias to be repointed when it already names a different project (otherwise that's an error).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept defaults and do not prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the onboarding plan without network calls or writes."),
) -> None:
    """Guided first run: sign in if needed, bind this repo, seed its brief, and install ambient capture."""
    try:
        if api_url:
            _state["api_url"] = api_url
        selector = project_ref or _state.get("project")
        project = _project_arg(str(selector)) if selector else None
        root = _repo_root()
        repo = _repo_info(root)
        bound_project = _bound_project_id(root)
        binding_path = _repo_binding_path(root)
        resolved_url = _repo_binding_api_url(root, api_url)

        readme_path = _find_readme(root)
        readme_text = _read_text_lossy(readme_path) if readme_path else None
        readme_page = _readme_page_payload(readme_path, readme_text)
        resources = _obvious_resource_uris(repo, readme_path, readme_text)

        if description_file:
            description = _read_file(description_file)

        default_title = title or _project_title_from_repo(str(repo["name"]))
        default_description = description or _description_from_readme(readme_text, default_title)

        if bound_project and project is None:
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

        if api_url and trust_api_url(api_url) and not _state.get("json"):
            console.print(f"[{GLOW}]Trusted[/] {api_origin(api_url)} for your credentials [dim](trustedApiUrls in ~/.moment.json)[/]")
        _sign_in_if_needed()
        _mint_agent_key_if_needed()

        prompt = not yes and sys.stdin.isatty()
        bootstrap_result: Optional[dict] = None
        if bound_project and project is None:
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
            attach_alias = alias or (str(repo["name"]) if bound_project is None else f"project-{pid}")
            moment_json, had_key = _write_repo_binding(
                root,
                pid,
                resolved_url,
                alias=attach_alias,
                title=default_title,
                description=default_description,
                keywords=[str(repo["name"])],
                paths=["."],
                make_default=default or bound_project is None,
                rebind=rebind,
            )
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
            moment_json, had_key = _write_repo_binding(
                root,
                pid,
                resolved_url,
                alias=str(repo["name"]),
                title=default_title,
                description=default_description,
                keywords=[str(repo["name"])],
                paths=["."],
                make_default=True,
            )
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
            _upsert_claude_block(path, _claude_block())
            if not _state.get("json"):
                console.print(f"[{GLOW}]Updated[/] {path} [dim](autostart block)[/]")

        hook_results: list[dict] = []
        if not no_hooks:
            hook_results = _install_capture_hooks(quiet=bool(_state.get("json")))

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
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    type: str = typer.Option(..., "--type", "-t", help="message | observation | hypothesis | question | finding | suggestion."),
    content_file: str = typer.Option(..., "--content-file", help="Path to the content (`-` for stdin)."),
    confidence: Optional[float] = typer.Option(None, "--confidence", help="Confidence score (0-1)."),
    discussion: Optional[int] = typer.Option(None, "--discussion", help="Discussion ID (required for message type)."),
    reasoning_file: Optional[str] = typer.Option(None, "--reasoning-file", help="Path to reasoning file (for messages)."),
    evidence_file: Optional[str] = typer.Option(None, "--evidence-file", help="Path to supporting evidence file."),
    parent: Optional[int] = typer.Option(None, "--parent", help="Parent message ID (for replies)."),
    source_file: Optional[str] = typer.Option(None, "--source-file", help="Path to source data JSON file."),
) -> None:
    """Post a message or signal to a project."""
    try:
        project_id = _project_arg(project)
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
            result = client.create_signal(project_id, payload)

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
    page_pinned: bool = typer.Option(False, "--page-pinned", help="Make the page must-read (top of the nav, in full into every orient)."),
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
            if page_pinned:
                page["pinned"] = True
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
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    title: str = typer.Option(..., "--title", help="Task title."),
    description_file: str = typer.Option(..., "--description-file", help="Path to description file (`-` for stdin)."),
    type: str = typer.Option(..., "--type", "-t", help="Work type (e.g. LITERATURE_REVIEW, CODE_CONTRIBUTION)."),
    instructions_file: str = typer.Option(..., "--instructions-file", help="Path to instructions file, min 10 chars (`-` for stdin)."),
    difficulty: Optional[str] = typer.Option(None, "--difficulty", "-d", help="Difficulty (TRIVIAL, EASY, MEDIUM, HARD, EXPERT)."),
    base_credits: Optional[int] = typer.Option(None, "--credits", help="Credits awarded (1-25, default 10)."),
) -> None:
    """Propose a task: OPEN if the project auto-approves agent tasks, else PROPOSED for review."""
    try:
        if description_file == "-" and instructions_file == "-":
            raise typer.BadParameter("Only one of --description-file and --instructions-file can read stdin.")
        project_id = _project_arg(project)
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


# --- Claim ledger -------------------------------------------------------------


@claim_app.command("show")
def claim_show(
    claim: str = typer.Argument(help="Claim id or Moment claim URN."),
) -> None:
    """Show the current state of a claim."""
    try:
        signal_id = _claim_id(claim)
        result = _get_client().get_claim_state(signal_id)
        if _state.get("json"):
            print_json(result)
            return
        state = result.get("derivedState") or result.get("state") or result
        signal = result.get("claim") or result.get("signal") or {}
        console.print(f"Claim #{signal_id}: {signal.get('content') or signal.get('title') or 'unnamed'}")
        if state.get("epistemicStatus"):
            console.print(f"Status: {state['epistemicStatus']}")
        summary = state.get("summary") or result.get("summary") or {}
        if summary:
            console.print(" · ".join(f"{key}: {value}" for key, value in summary.items()))
    except Exception as exc:
        _handle_error(exc)


@claim_app.command("evidence")
def claim_evidence(
    claim: str = typer.Argument(help="Claim id or Moment claim URN."),
    stance: Optional[str] = typer.Option(None, "--stance", help="for, against, or all."),
) -> None:
    """List evidence linked to a claim."""
    if stance not in (None, "for", "against", "all"):
        raise typer.BadParameter("--stance must be for, against, or all.")
    try:
        signal_id = _claim_id(claim)
        result = _get_client().find_claim_evidence(signal_id, stance)
        if _state.get("json"):
            print_json(result)
            return
        items = result.get("evidence") or []
        if not items:
            console.print(f"No evidence linked to claim #{signal_id}.")
            return
        for item in items:
            other = f"{item.get('otherType', 'NODE')}:{item.get('otherId', '?')}"
            title = f" — {item['title']}" if item.get("title") else ""
            console.print(f"{item.get('kind', 'EVIDENCE')} · {other}{title}")
    except Exception as exc:
        _handle_error(exc)


@claim_app.command("falsify")
def claim_add_falsifier(
    claim: str = typer.Argument(help="Claim id or Moment claim URN."),
    title: str = typer.Option(..., "--title", help="One sentence naming the result that would change our mind (≤200 chars)."),
    body: Optional[str] = typer.Option(None, "--body", help="Optional detail: the test, the threshold, the conditions."),
    body_file: Optional[str] = typer.Option(None, "--body-file", help="Read the detail from a file instead of --body."),
) -> None:
    """Record a falsifier on a claim (needs EDITOR access on the claim's project)."""
    try:
        client = _get_client()
        signal_id = _claim_id(claim)
        detail = _read_file(body_file) if body_file else body
        result = client.add_claim_falsifier(signal_id, title, detail)
        if _state.get("json"):
            print_json(result)
            return
        render_falsifier_created(result, signal_id)
    except Exception as exc:
        _handle_error(exc)


@claim_app.command("reproduce")
def claim_reproduce(
    claim: str = typer.Argument(help="Claim id or Moment claim URN."),
    target_id: int = typer.Option(..., "--target-id", help="Result node id."),
    target_type: str = typer.Option("SUBMISSION", "--target-type", help="SUBMISSION, RESOURCE, or SIGNAL."),
    note: Optional[str] = typer.Option(None, "--note", help="Optional note for the evidence-link proposal."),
) -> None:
    """Propose that a submission, resource, or signal reproduces a claim."""
    target_type = target_type.upper()
    if target_type not in {"SUBMISSION", "RESOURCE", "SIGNAL"}:
        raise typer.BadParameter("--target-type must be SUBMISSION, RESOURCE, or SIGNAL.")
    try:
        signal_id = _claim_id(claim)
        result = _get_client().propose_evidence_link(signal_id, {
            "kind": "REPRODUCES",
            "targetType": target_type,
            "targetId": target_id,
            **({"note": note} if note else {}),
        })
        if _state.get("json"):
            print_json(result)
            return
        proposal = result.get("proposal") or {}
        console.print(
            f"REPRODUCES proposal #{proposal.get('id', '?')} queued for claim #{signal_id} "
            f"from {target_type}:{target_id}."
        )
    except Exception as exc:
        _handle_error(exc)


# --- Experiments -------------------------------------------------------------


@experiment_app.command("state")
def experiment_state(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """Show the current experiment loop for a project."""
    try:
        project_id = _project_arg(project)
        result = _get_client().get_experiment_state(project_id)
        if _state.get("json"):
            print_json(result)
            return
        experiment = result.get("experiment") or result
        console.print(f"Experiment for project #{project_id}")
        for key in ("metricName", "metricDirection", "bestMetric", "evalCommand"):
            if experiment.get(key) is not None:
                console.print(f"{key}: {experiment[key]}")
    except Exception as exc:
        _handle_error(exc)


@experiment_app.command("submit")
def experiment_submit(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    metric_value: float = typer.Option(..., "--metric-value"),
    accepted: Optional[bool] = typer.Option(None, "--accepted/--rejected"),
    commit_sha: Optional[str] = typer.Option(None, "--commit-sha"),
    parent_commit_sha: Optional[str] = typer.Option(None, "--parent-commit-sha"),
    branch_name: Optional[str] = typer.Option(None, "--branch-name"),
    pr_url: Optional[str] = typer.Option(None, "--pr-url"),
    diff: Optional[str] = typer.Option(None, "--diff"),
    description: Optional[str] = typer.Option(None, "--description"),
    hypothesis: Optional[str] = typer.Option(None, "--hypothesis"),
    eval_output: Optional[str] = typer.Option(None, "--eval-output"),
    execution_time_ms: Optional[int] = typer.Option(None, "--execution-time-ms"),
    tokens_used: Optional[int] = typer.Option(None, "--tokens-used"),
) -> None:
    """Submit one locally evaluated experiment iteration."""
    try:
        if accepted is None:
            raise typer.BadParameter("Choose exactly one of --accepted or --rejected.")
        project_id = _project_arg(project)
    except Exception as exc:
        _handle_error(exc)
        return
    payload = {
        "metricValue": metric_value,
        "accepted": accepted,
        **({"commitSha": commit_sha} if commit_sha else {}),
        **({"parentCommitSha": parent_commit_sha} if parent_commit_sha else {}),
        **({"branchName": branch_name} if branch_name else {}),
        **({"prUrl": pr_url} if pr_url else {}),
        **({"diff": diff} if diff else {}),
        **({"description": description} if description else {}),
        **({"hypothesis": hypothesis} if hypothesis else {}),
        **({"evalOutput": eval_output} if eval_output else {}),
        **({"executionTimeMs": execution_time_ms} if execution_time_ms is not None else {}),
        **({"tokensUsed": tokens_used} if tokens_used is not None else {}),
    }
    try:
        result = _get_client().submit_experiment_result(project_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        run = result.get("run") or result
        console.print(
            f"Iteration {run.get('iteration', '?')} filed for project #{project_id} "
            f"at {metric_value}."
        )
    except Exception as exc:
        _handle_error(exc)


@experiment_app.command("leaderboard")
def experiment_leaderboard(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """Show experiment runs ordered by the configured metric."""
    try:
        project_id = _project_arg(project)
        result = _get_client().get_experiment_leaderboard(project_id)
        if _state.get("json"):
            print_json(result)
            return
        entries = result.get("runs") or []
        if not entries:
            console.print(f"No experiment results for project #{project_id}.")
            return
        for entry in entries:
            console.print(
                f"Iteration {entry.get('iteration', '?')} · "
                f"{entry.get('metricValue', entry.get('metric', '?'))}"
            )
    except Exception as exc:
        _handle_error(exc)


# --- Discussions -------------------------------------------------------------


@discussion_app.command("list")
def discussion_list(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
) -> None:
    """List discussions in a project."""
    try:
        project_id = _project_arg(project)
        result = _get_client().list_discussions(project_id)
        if _state.get("json"):
            print_json(result)
            return
        discussions = result.get("recentDiscussions") or result.get("discussions") or []
        if not discussions:
            console.print("No discussions in this project yet.")
            return
        for discussion in discussions:
            console.print(
                f"#{discussion.get('id', '?')}  {discussion.get('title') or 'Untitled discussion'}"
            )
    except Exception as exc:
        _handle_error(exc)


@discussion_app.command("read")
def discussion_read(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    discussion_id: Optional[int] = typer.Option(None, "--discussion", help="Discussion ID."),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Read messages from a project discussion."""
    try:
        project_id = _project_arg(project)
        result = _get_client().get_discussion_messages(project_id, discussion_id, limit)
        if _state.get("json"):
            print_json(result)
            return
        messages = result.get("messages") or []
        for message in messages:
            console.print(f"{message.get('id', '?')}  {message.get('content', '')}")
    except Exception as exc:
        _handle_error(exc)


@discussion_app.command("post")
def discussion_post(
    project: Optional[str] = typer.Argument(None, help=PROJECT_ARG_HELP, show_default=False),
    discussion_id: int = typer.Option(..., "--discussion", help="Discussion ID."),
    content: str = typer.Option(..., "--content", help="Message text (`-` reads stdin)."),
    reasoning: Optional[str] = typer.Option(None, "--reasoning"),
    parent_id: Optional[int] = typer.Option(None, "--parent"),
) -> None:
    """Post a message to a project discussion (visible to people: post only when asked to)."""
    try:
        project_id = _project_arg(project)
        payload = {
            "discussionId": discussion_id,
            "content": _read_file("-") if content == "-" else content,
            **({"reasoning": reasoning} if reasoning else {}),
            **({"parentId": parent_id} if parent_id is not None else {}),
        }
        result = _get_client().post_message(project_id, payload)
        if _state.get("json"):
            print_json(result)
            return
        message = result.get("message") or result
        console.print(f"Message #{message.get('id', '?')} posted to discussion #{discussion_id}.")
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


_CATALOG_SKIP_OPTIONS = frozenset({"help", "version", "install_completion", "show_completion"})


def _catalog_type(param: object) -> str:
    if getattr(param, "is_flag", False):
        return "flag"
    kind = getattr(param, "type", None)
    choices = getattr(kind, "choices", None)
    if choices:
        return "|".join(str(choice) for choice in choices)
    name = str(getattr(kind, "name", "") or "text").lower()
    # typer 0.27 names click's STRING type "str"; earlier releases said "text".
    return {"integer": "int", "float": "number", "boolean": "bool", "str": "text", "string": "text"}.get(name, name)


def _catalog_option(param: object) -> dict:
    entry: dict = {
        "flags": list(param.opts) + list(getattr(param, "secondary_opts", None) or []),  # type: ignore[attr-defined]
        "type": _catalog_type(param),
    }
    if getattr(param, "required", False):
        entry["required"] = True
    if getattr(param, "multiple", False):
        entry["repeatable"] = True
    default = getattr(param, "default", None)
    if isinstance(default, (str, int, float)) and not isinstance(default, bool) and default != "":
        entry["default"] = default
    if getattr(param, "help", None):
        entry["help"] = param.help  # type: ignore[attr-defined]
    return entry


def _catalog_entry(path: list[str], command: object) -> dict:
    arguments, options = [], []
    for param in getattr(command, "params", None) or []:
        if getattr(param, "hidden", False) or getattr(param, "name", None) in _CATALOG_SKIP_OPTIONS:
            continue
        if getattr(param, "param_type_name", "") == "argument":
            name = getattr(param, "metavar", None) or param.name.upper()  # type: ignore[attr-defined]
            arguments.append({
                "name": name,
                "required": bool(getattr(param, "required", False)),
                **({"help": param.help} if getattr(param, "help", None) else {}),  # type: ignore[attr-defined]
            })
        elif getattr(param, "param_type_name", "") == "option":
            options.append(_catalog_option(param))
    help_text = (getattr(command, "help", None) or "").strip()
    return {
        "command": " ".join(path),
        "summary": help_text.split("\n")[0].strip(),
        "arguments": arguments,
        "options": options,
    }


def _catalog_walk(group: object, path: list[str]):
    for name, sub in sorted((getattr(group, "commands", None) or {}).items()):
        if getattr(sub, "hidden", False):
            continue
        if hasattr(sub, "commands"):
            yield from _catalog_walk(sub, path + [name])
        else:
            yield _catalog_entry(path + [name], sub)


def _catalog_usage(entry: dict) -> str:
    words = [f"moment {entry['command']}"]
    for argument in entry["arguments"]:
        name = argument["name"]
        words.append(name if argument["required"] or name.startswith("[") else f"[{name}]")
    return " ".join(words)


# The context loop, in the order an agent meets it; everything else follows alphabetically.
_CATALOG_START_HERE = (
    "status", "orient", "page-read", "find", "packet", "page", "project add-resource", "log",
)


def _catalog_option_line(entry: dict) -> Optional[str]:
    flags = []
    for option in entry["options"]:
        flag = "/".join(option["flags"])
        if option["type"] != "flag":
            flag += f" <{option['type']}>"
        if option.get("required"):
            flag += " (required)"
        flags.append(flag)
    return "    " + " · ".join(flags) if flags else None


@app.command("commands")
def commands_catalog(
    ctx: typer.Context,
    prefix: Optional[list[str]] = typer.Argument(None, help="A command or group, e.g. `log` or `project`: show its options."),
    all_details: bool = typer.Option(False, "--all", help="Every command with every option."),
) -> None:
    """The CLI's map: one line per command; name a command or group (or --all) for its options."""
    root = ctx.find_root().command
    entries = [
        entry for entry in _catalog_walk(root, [])
        if not prefix or entry["command"].split()[: len(prefix)] == list(prefix)
    ]
    if prefix and not entries:
        _handle_error(typer.BadParameter(f"No command matches {' '.join(prefix)!r}; `moment commands` lists them."))
    detailed = bool(prefix) or all_details
    global_options = [
        _catalog_option(param) for param in root.params
        if getattr(param, "param_type_name", "") == "option" and param.name not in _CATALOG_SKIP_OPTIONS
    ]
    if _state.get("json"):
        commands = entries if detailed else [
            {"command": entry["command"], "usage": _catalog_usage(entry), "summary": entry["summary"]}
            for entry in entries
        ]
        print_json({"version": __version__, "globalOptions": global_options, "commands": commands})
        return

    def block(entry: dict) -> list[str]:
        lines = [f"{_catalog_usage(entry)} — {entry['summary']}"]
        options = _catalog_option_line(entry) if detailed else None
        return lines + ([options] if options else [])

    lines = [
        "-p <id|alias>, --json, and --quiet work before or after the subcommand; "
        "--api-url, --api-key, and --config go before it.",
    ]
    if detailed:
        for entry in entries:
            lines += block(entry)
    else:
        by_name = {entry["command"]: entry for entry in entries}
        lines += ["", "Start here:"]
        for name in _CATALOG_START_HERE:
            if name in by_name:
                lines += block(by_name[name])
        lines += ["", "Everything else (`moment commands <command>` shows its options):"]
        for entry in entries:
            if entry["command"] not in _CATALOG_START_HERE:
                lines += block(entry)
    sys.stdout.write("\n".join(lines) + "\n")


@app.command("activity")
def activity(
    limit: int = typer.Option(20, "--limit", "-n", "-l", help="Max results."),
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
