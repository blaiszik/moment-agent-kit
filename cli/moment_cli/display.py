"""Display helpers: Rich for a person at a terminal, plain text for everything else.

Most readers of this CLI are agents reading piped stdout. Panel borders and
80-column hard wraps cost them tokens and split words mid-line, so when stdout
is not a terminal every renderer's output is flattened: panels become
``## Title`` sections, markdown prints raw, tables print as markdown tables, and
nothing is wrapped. ``MOMENT_OUTPUT=plain`` or ``MOMENT_OUTPUT=rich`` overrides
the detection (for example under a harness that allocates a pseudo-terminal).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sys
from typing import Any, Optional

from rich.console import Console, Group
from rich.errors import MarkupError
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


def _forced_mode() -> Optional[str]:
    mode = (os.environ.get("MOMENT_OUTPUT") or "").strip().lower()
    return mode if mode in ("plain", "rich") else None


def stdout_is_rich() -> bool:
    """True when a person is reading stdout in a terminal."""
    mode = _forced_mode()
    if mode:
        return mode == "rich"
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Text):
        return value.plain
    text = str(value)
    try:
        return Text.from_markup(text).plain
    except Exception:
        return text


def _table_cell(value: Any) -> str:
    return _plain_text(value).replace("|", "\\|").replace("\n", " ").strip()


def _markdown_table(table: Table) -> str:
    headers = [_table_cell(column.header) for column in table.columns]
    rows = zip(*(list(column.cells) for column in table.columns))
    lines = []
    if table.title:
        lines.append(f"## {_plain_text(table.title)}")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    lines.extend("| " + " | ".join(_table_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _flatten(renderable: Any) -> list[Any]:
    """One renderable as plain, unboxed pieces, each printed on its own line."""
    if isinstance(renderable, Panel):
        pieces: list[Any] = []
        title = _plain_text(renderable.title)
        if title:
            pieces.append(Text(f"## {title}"))
        pieces.extend(_flatten(renderable.renderable))
        subtitle = _plain_text(renderable.subtitle)
        if subtitle:
            pieces.append(Text(subtitle))
        return pieces
    if isinstance(renderable, Group):
        return [piece for child in renderable.renderables for piece in _flatten(child)]
    if isinstance(renderable, Markdown):
        return [Text(renderable.markup)]
    if isinstance(renderable, Rule):
        title = _plain_text(renderable.title)
        return [Text(f"--- {title} ---" if title else "---")]
    if isinstance(renderable, Table):
        return [Text(_markdown_table(renderable))]
    return [renderable]


# This CLI never writes link markup itself, so any `[link=…]` tag came from
# server text interpolated into a markup string, where it would become a real
# terminal hyperlink. Drop the tags and keep the words.
_LINK_MARKUP = re.compile(r"\[/?link(?:=[^\]]*)?\]")


def _defuse(renderable: Any) -> Any:
    return _LINK_MARKUP.sub("", renderable) if isinstance(renderable, str) else renderable


class AgentConsole(Console):
    """A Rich console that prints plain, unwrapped text when nobody is watching."""

    def is_plain(self) -> bool:
        mode = _forced_mode()
        if mode:
            return mode == "plain"
        return not self.is_terminal

    def _emit(self, objects: tuple, kwargs: dict) -> None:
        objects = tuple(_defuse(obj) for obj in objects)
        try:
            super().print(*objects, **kwargs)
        except MarkupError:
            # Server text such as "Use [/] to close" breaks a markup string; print
            # it literally (panels included) rather than failing the command.
            previous = self._markup
            self._markup = False
            try:
                super().print(*objects, **{**kwargs, "markup": False})
            finally:
                self._markup = previous

    def print(self, *objects: Any, **kwargs: Any) -> None:  # type: ignore[override]
        if not self.is_plain():
            self._emit(objects, kwargs)
            return
        kwargs.setdefault("soft_wrap", True)
        if all(isinstance(obj, (str, Text)) for obj in objects):
            self._emit(objects, kwargs)
            return
        for obj in objects:
            for piece in _flatten(obj):
                self._emit((piece,), kwargs)


console = AgentConsole(highlight=False)
err_console = AgentConsole(stderr=True, highlight=False)

# Color palette matching the original theme
ACCENT = "#72d6ff"
ACCENT_STRONG = "#3bb8f5"
SPARK = "#ffd166"
GLOW = "#8ce99a"
ROSE = "#ff8fab"
READ_NOW_COUNT = 3
SAME_WRITE_SLACK_MS = 5_000
_SENTINEL = object()


def _parse_timestamp(val: Any) -> Optional[datetime]:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def raw_markdown_is_stale(
    updated_at: Any,
    raw_markdown_updated_at: Any = _SENTINEL,
) -> bool:
    """Port of server rawMarkdownIsStale from app/lib/handoff-freshness.ts.

    Returns True when rawMarkdownUpdatedAt is older than updatedAt (beyond the
    same-write slack). A missing stamp is a legacy row and is NOT stale.
    Can be called with (handoff_dict) or (updated_at, raw_markdown_updated_at).
    """
    if raw_markdown_updated_at is _SENTINEL:
        if isinstance(updated_at, dict):
            handoff = updated_at
            updated_at = handoff.get("updatedAt")
            raw_markdown_updated_at = handoff.get("rawMarkdownUpdatedAt")
        else:
            raw_markdown_updated_at = None

    # No stamp = legacy row (pre-backfill) — keep today's behavior (server parity).
    if not updated_at or not raw_markdown_updated_at:
        return False

    u_dt = _parse_timestamp(updated_at)
    r_dt = _parse_timestamp(raw_markdown_updated_at)
    if u_dt is None or r_dt is None:
        return False
    return (u_dt - r_dt).total_seconds() > (SAME_WRITE_SLACK_MS / 1000.0)


def format_structured_handoff(handoff: dict) -> list[str]:
    """Format structured handoff fields into Rich markup lines."""
    hl = []
    if handoff.get("nextStep"):
        hl.append(f"[bold {GLOW}]→ Next:[/] {handoff['nextStep']}")
    for label, key in (
        ("Works now", "worksNow"),
        ("In progress", "inProgress"),
        ("Blocked", "blocked"),
        ("Watch out", "watchOut"),
    ):
        if handoff.get(key):
            hl.append(kv(label, handoff[key]))
    for r in handoff.get("readFirst") or []:
        line = f"  • {r.get('label', '')}"
        if r.get("why"):
            line += f" [dim]— {r['why']}[/]"
        hl.append(line)
    return hl


def _write_json(stream: Any, data: Any, *, pretty: bool) -> None:
    """Indented for a terminal, compact for a pipe; UTF-8 unless the stream can't take it."""
    layout: dict[str, Any] = {"indent": 2} if pretty else {"separators": (",", ":")}
    try:
        stream.write(json.dumps(data, ensure_ascii=False, default=str, **layout) + "\n")
    except UnicodeEncodeError:
        stream.write(json.dumps(data, ensure_ascii=True, default=str, **layout) + "\n")


def print_json(data: Any) -> None:
    _write_json(sys.stdout, data, pretty=stdout_is_rich())


def print_error(message: str, status: Optional[int] = None, hint: Optional[str] = None) -> None:
    prefix = f"Moment API error ({status})" if status else "Error"
    err_console.print(f"[bold red]{prefix}:[/] {escape(message)}")
    if hint:
        err_console.print(escape(hint))


def print_error_json(
    error: str,
    status: Optional[int] = None,
    details: Any = None,
    hint: Optional[str] = None,
) -> None:
    payload: dict[str, Any] = {"error": error}
    if status is not None:
        payload["status"] = status
    if isinstance(details, dict):
        # Lift the fields an agent branches on out of the server's error body.
        for key in ("code", "retryable", "retryAfter"):
            if details.get(key) is not None:
                payload[key] = details[key]
    if hint:
        payload["hint"] = hint
    if details is not None:
        payload["details"] = details
    _write_json(sys.stderr, payload, pretty=False)


def kv(label: str, value: Any, label_width: int = 16) -> str:
    return f"[dim]{str(label).ljust(label_width)}[/] {value}"


def chip(label: str, color: str = ACCENT_STRONG) -> str:
    return f"[{color}]\\[{label}][/{color}]"


def format_percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{value * 100:.1f}%"


def color_status(status: str) -> str:
    s = (status or "").upper()
    if s == "APPROVED":
        return f"[{GLOW}]{s}[/]"
    if s == "REJECTED":
        return f"[{ROSE}]{s}[/]"
    return f"[{SPARK}]{s or 'UNKNOWN'}[/]"


def moment_header() -> Text:
    """Simple styled header replacing the ASCII art banner."""
    text = Text()
    text.append("MOMENT", style=f"bold {ACCENT_STRONG}")
    text.append("  research agent cli", style="dim")
    return text


# --- Render functions ---


def render_auth_whoami(payload: dict, config: Any) -> None:
    lines = [
        kv("Name", payload["agent"]["name"]),
        kv("ID", payload["agent"]["id"]),
        kv("Type", payload["agent"]["type"]),
        kv("Owner", (payload["agent"].get("owner") or {}).get("name", "unknown")),
        kv("API", config.api_url),
        kv("Key", config.fingerprint),
        kv("Credits", payload["stats"]["totalCreditsEarned"]),
        kv("Success rate", format_percent(payload["stats"]["successRate"])),
    ]
    console.print(Panel("\n".join(lines), title="Authenticated agent", border_style=ACCENT_STRONG))


def render_stats(payload: dict) -> None:
    lines = [
        kv("Agent", payload["agent"]["name"]),
        kv("Owner", (payload["agent"].get("owner") or {}).get("name", "unknown")),
        kv("Type", payload["agent"]["type"]),
        kv("Total tasks", payload["stats"]["totalTasks"]),
        kv("Successful", payload["stats"]["successfulUnits"]),
        kv("Credits", payload["stats"]["totalCreditsEarned"]),
        kv("Tokens", payload["stats"]["totalTokensProcessed"]),
        kv("Success rate", format_percent(payload["stats"]["successRate"])),
        kv("Avg quality", payload["stats"].get("averageQuality", "n/a")),
    ]
    console.print(Panel("\n".join(lines), title="Agent stats", border_style=ACCENT_STRONG))

    claims = payload.get("recentClaims") or []
    if claims:
        claim_lines = []
        for c in claims[:5]:
            status = color_status(c["status"])
            task = c.get("task") or c.get("workUnit") or {}
            claim_lines.append(f"{status} {task.get('title', 'unknown')} [dim]({task.get('type', '')})[/]")
        console.print(Panel("\n".join(claim_lines), title="Recent claims", border_style=GLOW))


def render_work_list(payload: dict) -> None:
    recommended = payload.get("recommended")
    if recommended:
        w = recommended
        rec_lines = [
            f"[bold]#{w['id']} {w['title']}[/]",
            f"{chip(w['workType'])} {chip(w['difficulty'], SPARK)} {chip(str(w['credits']) + ' credits', GLOW)}",
            f"{w['project']['title']} [dim]| AI score {w['aiRecommendation']['score']}[/]",
            f"[dim]slots {w['slotsAvailable']}/{w['maxClaims']}[/]",
        ]
        console.print(Panel("\n".join(rec_lines), title="Top recommendation", border_style=SPARK))

    items = payload.get("tasks") or payload.get("contributions") or []
    if not items:
        console.print(Panel("[dim]No tasks available.[/]", title="Available tasks", border_style=ACCENT_STRONG))
        return

    table = Table(title="Available tasks", border_style=ACCENT_STRONG, show_lines=False)
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Title")
    table.add_column("Type", style=ACCENT)
    table.add_column("Project", style="dim")
    table.add_column("Credits", style=SPARK, justify="right")
    table.add_column("Slots", style="dim", justify="right")

    for item in items:
        table.add_row(
            f"#{item['id']}",
            item["title"],
            item["workType"],
            item["project"]["title"],
            f"{item['credits']} cr",
            f"{item['slotsAvailable']}/{item['maxClaims']}",
        )

    console.print(table)


def render_work_start(payload: dict) -> None:
    w = payload["work"]
    lines = [
        f"[bold]{w['title']}[/]",
        f"{chip(w['workType'])} {chip(w['difficulty'], SPARK)} {chip(str(w['credits']) + ' credits', GLOW)}",
        kv("Project", payload["project"]["title"]),
        kv("GitHub", payload.get("github", {}).get("repo", "none") if payload.get("github") else "none"),
        kv("Claim", payload["claim"]["id"]),
    ]
    console.print(Panel("\n".join(lines), title=f"Started task #{w['id']}", border_style=GLOW))

    if w.get("description"):
        console.print(Panel(w["description"], title="Description", border_style=ACCENT))

    if w.get("instructions"):
        console.print(Panel(w["instructions"], title="Instructions", border_style=ACCENT_STRONG))

    if payload.get("nextStep"):
        console.print(Panel(payload["nextStep"], title="Next step", border_style=SPARK))


def render_work_context(payload: dict) -> None:
    """Render full task context (used by `work context` command)."""
    task = payload.get("task") or payload.get("contribution") or payload.get("workunit") or payload.get("workUnit") or {}
    lines = []
    if task.get("title"):
        lines.append(f"[bold]{task['title']}[/]")
    if task.get("description"):
        lines.append(task["description"])
    for key in ("instructions", "context", "requirements"):
        if task.get(key):
            lines.append(f"\n[bold]{key.title()}:[/]\n{task[key]}")

    project = payload.get("project")
    if project:
        lines.append(kv("Project", project.get("title", "unknown")))

    console.print(Panel("\n".join(lines) if lines else "[dim]No context available.[/]",
                        title=f"Task #{task.get('id', '?')}", border_style=ACCENT_STRONG))


def render_submission(payload: dict) -> None:
    sub = payload["submission"]
    lines = [
        payload.get("message", "Submission received."),
        kv("Submission", sub["id"]),
        kv("Task", sub.get("taskId") or sub.get("contributionId")),
        kv("Status", sub["status"]),
        kv("PR", sub.get("prUrl", "none")),
    ]
    console.print(Panel("\n".join(lines), title="Submission received", border_style=GLOW))


def render_project_context(payload: dict) -> None:
    p = payload["project"]
    meta = payload.get("metadata", {})
    lines = [
        f"[bold]{p['title']}[/]",
        kv("Token estimate", meta.get("tokenEstimate", "n/a")),
        kv("Cache hit", "yes" if meta.get("cacheHit") else "no"),
        kv("Active tasks", len(payload.get("activeTasks") or [])),
        kv("Recent signals", len(payload.get("recentSignals") or [])),
        kv("Discussions", len(payload.get("recentDiscussions") or [])),
    ]
    console.print(Panel("\n".join(lines), title=f"Project #{p['id']}", border_style=ACCENT_STRONG))

    if p.get("description"):
        console.print(Panel(p["description"], title="Description", border_style=ACCENT))


def render_brief(payload: dict, quiet: bool = False) -> None:
    """The spin-up surface — orient before you edit. The human view of the brief,
    rendered in the same priority order as `protocol.readOrder` so it agrees with
    the agent-ready promptContext."""
    p = payload.get("project") or {}
    fresh = payload.get("contextFreshness") or {}
    handoff = payload.get("handoff")
    meta = payload.get("metadata") or {}

    # 1. Header — identity + freshness at a glance.
    domain = str(p.get("domain", "")).replace("_", " ").title()
    fresh_chip = chip("STALE", ROSE) if fresh.get("stale") else chip("FRESH", GLOW)
    header = [
        f"[bold]{p.get('title', '(untitled)')}[/]  [dim]#{p.get('id', '?')}[/]",
        f"{color_status(p.get('status', ''))} {chip(domain or 'GENERAL')} {fresh_chip}",
    ]
    if fresh.get("reason"):
        header.append(f"[dim]{fresh['reason']}[/]")
    if p.get("description"):
        header += ["", p["description"]]
    console.print(Panel("\n".join(header), title="Orient", border_style=ACCENT_STRONG))

    # 2. Spin-up — fastest path to productive.
    spin = payload.get("spinUp") or {}
    spin_lines = []
    if spin.get("tldr"):
        spin_lines.append(kv("TL;DR", spin["tldr"]))
    if spin.get("firstStep"):
        spin_lines.append(kv("First step", spin["firstStep"]))
    if spin.get("howToCollaborate"):
        spin_lines.append(kv("Collaborate", spin["howToCollaborate"]))
    if spin_lines:
        console.print(Panel("\n".join(spin_lines), title="Spin up", border_style=SPARK))

    # 3. Handoff — the heart of the brief.
    if handoff:
        footer = f"v{handoff.get('version', '?')}"
        if handoff.get("updatedAt"):
            footer += f" · {str(handoff['updatedAt'])[:10]}"
        if handoff.get("updatedBy"):
            footer += f" · {handoff['updatedBy']}"
        if handoff.get("rawMarkdownUpdatedAt"):
            footer += f" · brief stamped {str(handoff['rawMarkdownUpdatedAt'])[:10]}"
        raw = (handoff.get("rawMarkdown") or "").strip()
        if raw:
            if not raw_markdown_is_stale(handoff):
                # The full handoff is the real thing — render it as markdown.
                console.print(
                    Panel(
                        Markdown(raw),
                        title="Handoff",
                        border_style=GLOW,
                        subtitle=f"[dim]{footer}[/]",
                    )
                )
            else:
                hl = format_structured_handoff(handoff)
                items: list[Any] = []
                if hl:
                    items.append(console.render_str("\n".join(hl)))
                items.append(Rule("Earlier brief (superseded by the patches above)", style="dim"))
                items.append(Markdown(raw))
                console.print(
                    Panel(
                        Group(*items),
                        title="Handoff",
                        border_style=GLOW,
                        subtitle=f"[dim]{footer}[/]",
                    )
                )
        else:
            hl = format_structured_handoff(handoff)
            hl += ["", f"[dim]{footer}[/]"]
            console.print(Panel("\n".join(hl), title="Handoff", border_style=GLOW))
    else:
        console.print(Panel(
            "[dim]No handoff yet — run a session and `moment log` to create one.[/]",
            title="Handoff", border_style=GLOW))

    if quiet:
        return

    # Must read — pinned pages with clamped bodies (§S2).
    must_read = payload.get("mustRead") or []
    if must_read:
        mr_items: list[Any] = []
        for i, pg in enumerate(must_read):
            slug = pg.get("slug", "")
            title = pg.get("title", "")
            body = (pg.get("body") or "").strip()
            if len(body) > 3000:
                body = body[:3000] + f"\n\n… (moment page-read {slug} for the rest)"
            if i > 0:
                mr_items.append(Text(""))
            mr_items.append(Text(f"{title} ({slug})", style="bold"))
            if body:
                mr_items.append(Markdown(body))
        console.print(Panel(Group(*mr_items), title="Must read", border_style=ACCENT))

    # 4. Read order — the assembler's own prioritization.
    read_order = (payload.get("protocol") or {}).get("readOrder") or []
    if read_order:
        read_order_lines = [
            f"  {i + 1}. {step}"
            for i, step in enumerate(read_order[:READ_NOW_COUNT])
        ]
        remaining = len(read_order) - READ_NOW_COUNT
        if remaining > 0:
            read_order_lines.append(
                f"[dim]… and {remaining} reference docs — "
                "`moment gather <topic>` pulls what you need.[/]"
            )
        console.print(Panel(
            "\n".join(read_order_lines),
            title="Read order", border_style=ACCENT))

    # 5. Pages canvas.
    pages = payload.get("pages") or []
    if pages:
        table = Table(title="Pages", border_style=ACCENT, show_lines=False)
        table.add_column("Slug", style="bold")
        table.add_column("Title")
        table.add_column("Must-read", justify="center")
        for pg in pages:
            table.add_row(pg.get("slug", ""), pg.get("title", ""),
                          "✓" if pg.get("pinned") else "")
        console.print(table)

    # 6. Resources manifest (ids feed `moment read-resource`).
    resources = payload.get("resources") or []
    if resources:
        table = Table(title="Resources", border_style=ACCENT, show_lines=False)
        table.add_column("ID", style="bold", no_wrap=True)
        table.add_column("Kind", style=ACCENT)
        table.add_column("Title")
        table.add_column("URI", style="dim")
        for r in resources[:20]:
            table.add_row(f"#{r.get('id', '?')}", r.get("kind", ""),
                          r.get("title", ""), r.get("uri", ""))
        console.print(table)

    # 7. Active tasks.
    tasks = payload.get("activeTasks") or []
    if tasks:
        console.print(Panel(
            "\n".join(f"{color_status(t.get('status', ''))} {t.get('title', '')}"
                      for t in tasks[:10]),
            title=f"Active tasks ({len(tasks)})", border_style=ACCENT_STRONG))

    # Doors — OPEN, unclaimed tasks (§S2).
    door_tasks = payload.get("doorTasks") or []
    if door_tasks:
        door_lines = []
        for t in door_tasks:
            tid = t.get("id", "?")
            title = t.get("title", "")
            ttype = t.get("type", "")
            est = t.get("estimatedMinutes")
            est_str = str(est) if est is not None else "—"
            door_lines.append(f"#{tid} {title} · {ttype} · {est_str} min")
        console.print(Panel(Text("\n".join(door_lines)), title="Doors", border_style=ACCENT_STRONG))

    console.print(
        f"[dim]~{meta.get('tokenEstimate', '?')} tokens · "
        "`moment page-read <slug>` reads a page in full · "
        "`moment orient --prompt` prints the agent brief[/]")

    next_calls = payload.get("nextCalls") or []
    if next_calls:
        calls_str = " · ".join(str(c) for c in next_calls)
        console.print(f"[dim]Next: {calls_str}[/]")


def render_gather(payload: dict, label: str) -> None:
    """The compiled context bundle — what to hand a new agent session."""
    nodes = payload.get("nodes") or []
    meta = f"{len(nodes)} nodes · ~{payload.get('tokenEstimate', '?')} tokens · seeds: {payload.get('mode', '?')}"
    console.print(Panel(f"[bold]Context for:[/] {label}\n[dim]{meta}[/]", border_style=ACCENT_STRONG))
    if not nodes:
        console.print("[dim]Nothing gathered — try a different question, or check the project has a context graph.[/]")
        return
    for n in nodes:
        score = n.get("score")
        via = ", ".join(n.get("viaKinds") or [])
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
        console.print(f"[{GLOW}]{score_s}[/]  [bold]{n.get('title', '')}[/]" + (f"  [dim]· {via}[/]" if via else ""))
        body = n.get("body")
        if isinstance(body, str) and body.strip():
            # The engine spent budget on this node's FULL body — deliver it raw
            # (pipe-friendly), never re-truncated back into an excerpt.
            console.print("      [dim]— full body —[/]")
            sys.stdout.write(body.rstrip() + "\n")
        else:
            ex = (n.get("excerpt") or "").strip()
            if ex:
                console.print(f"      [dim]{ex}[/]")
    excerpt_only = [n for n in nodes if n.get("slug") and not isinstance(n.get("body"), str)]
    if excerpt_only:
        console.print("[dim]Excerpt-only pages — read any in full: moment page-read <slug>[/]")


# --- Research Context Packets ---

# Authority is how a source earned its place; freshness is honest age labeling.
AUTHORITY_STYLE = {"REVIEWED": GLOW, "CURATED": ACCENT, "UNREVIEWED": SPARK}
FRESHNESS_STYLE = {"CURRENT": GLOW, "STALE": ROSE, "UNKNOWN": "dim"}


def _clip(text: Any, limit: int = 240) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _packet_author(meta: dict) -> str:
    user = meta.get("createdBy") or {}
    agent = meta.get("createdByAgent") or {}
    return user.get("name") or agent.get("name") or "unknown"


def _packet_reader(item: dict) -> Optional[str]:
    """The CLI call that reads an evidence item's source in full, when there is one."""
    entity = item.get("entity") or {}
    kind = str(entity.get("type") or "").upper()
    project = entity.get("projectId")
    scope = f"-p {project} " if project else ""
    if kind == "PAGE":
        href = str((item.get("locator") or {}).get("href") or "")
        slug = entity.get("slug") or (href.split("page=", 1)[1].split("&", 1)[0] if "page=" in href else None)
        return f"moment {scope}page-read {slug}" if slug else None
    if kind == "RESOURCE" and entity.get("id"):
        return f"moment {scope}project read-resource {entity['id']}"
    return None


def render_packet(payload: dict, quiet: bool = False) -> None:
    """A compiled Research Context Packet — inspectable evidence, not just ranked context."""
    evidence = payload.get("evidence") or []
    caveats = payload.get("caveats") or []
    meta = (
        f"{len(evidence)} evidence · "
        f"~{payload.get('tokenEstimate', '?')}/{payload.get('tokenBudget', '?')} tokens · "
        f"{payload.get('excludedCount', 0)} withheld · {payload.get('compilerVersion', '?')}"
    )
    console.print(Panel(
        f"[bold]Question:[/] {payload.get('question', '')}\n[dim]{meta}[/]",
        title="Research Context Packet", border_style=ACCENT_STRONG))

    # Caveats are the honest limits — budget clips, staleness, empty results.
    if caveats:
        console.print(Panel("\n".join(f"• {c}" for c in caveats), title="Caveats", border_style=SPARK))

    if not evidence:
        console.print("[dim]No evidence matched — try a different question, or check the project has a context graph.[/]")
        return

    for item in evidence:
        authority = str(item.get("authority") or "UNREVIEWED")
        freshness = str(item.get("freshness") or "UNKNOWN")
        entity = item.get("entity") or {}
        evidence_id = item.get("evidenceId") or f"{entity.get('type', '?')}:{entity.get('id', '?')}"
        date = str(item.get("updatedAt") or "")[:10]
        author = (item.get("provenance") or {}).get("author")
        reader = _packet_reader(item)
        line = (
            f"[bold]{escape(str(evidence_id))}[/] {escape(str(item.get('title') or '(untitled)'))}"
            + f" {chip(authority, AUTHORITY_STYLE.get(authority, ACCENT_STRONG))}"
            + f" {chip(freshness, FRESHNESS_STYLE.get(freshness, 'dim'))}"
            + (f" [dim]· {date}[/]" if date else "")
            + (f" [dim]· {escape(str(author))}[/]" if author else "")
            + (f" [dim]→ {escape(reader)}[/]" if reader else "")
        )
        console.print(line)
        reason = (item.get("match") or {}).get("reason")
        if reason:
            console.print(f"      [dim]why: {reason}[/]")
        if not quiet:
            excerpt = _clip(item.get("excerpt"), 400)
            if excerpt:
                console.print(f"      [dim]{excerpt}[/]")


def render_saved_packet(meta: dict, permalink: Optional[str] = None,
                        title: str = "Packet snapshot saved") -> None:
    """The stable handle for a frozen packet snapshot."""
    lines = [
        kv("Packet", f"#{meta.get('id')}"),
        kv("Question", _clip(meta.get("question"), 160)),
        kv("Evidence", meta.get("evidenceCount", 0)),
        kv("Saved by", _packet_author(meta)),
        kv("Permalink", permalink or f"/projects/{meta.get('projectId')}/packets/{meta.get('id')}"),
    ]
    console.print(Panel("\n".join(lines), title=title, border_style=GLOW))


def render_packet_list(packets: list) -> None:
    """Saved snapshots for one project, newest first."""
    if not packets:
        console.print(Panel("[dim]No saved packets yet — `moment -p N packet \"<question>\" --save` freezes one.[/]",
                            title="Saved packets", border_style=ACCENT_STRONG))
        return

    table = Table(title="Saved packets", border_style=ACCENT_STRONG, show_lines=False)
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Question")
    table.add_column("Evidence", style=SPARK, justify="right")
    table.add_column("Saved", style="dim", no_wrap=True)
    table.add_column("By", style="dim")
    for p in packets:
        table.add_row(
            f"#{p.get('id', '?')}",
            _clip(p.get("question"), 80),
            str(p.get("evidenceCount", 0)),
            str(p.get("createdAt", ""))[:10],
            _packet_author(p),
        )
    console.print(table)


def render_page(payload: dict) -> None:
    """A single page in full: one header line (slug, role, version for --if-version,
    date), then the raw markdown body on stdout."""
    page = payload.get("page") or {}
    title = page.get("title") or page.get("slug") or "(untitled)"
    bits = [
        page.get("slug"),
        page.get("role"),
        "pinned" if page.get("pinned") else None,
        f"v{page['version']}" if page.get("version") is not None else None,
        str(page.get("updatedAt") or "")[:10] or None,
    ]
    meta = " · ".join(str(bit) for bit in bits if bit)
    if console.is_plain():
        console.print(Text(f"# {title}" + (f" ({meta})" if meta else "")))
    else:
        console.print(Panel(f"[bold]{escape(title)}[/]" + (f"\n[dim]{escape(meta)}[/]" if meta else ""), border_style=ACCENT_STRONG))
    body = page.get("body") or ""
    if body.strip():
        sys.stdout.write(body.rstrip() + "\n")
    else:
        console.print("[dim](this page is empty)[/]")


def _clip_line(text: Any, limit: int = 240) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def write_queued(result: dict) -> bool:
    """Print the HTTP 202 outcome of a write on a project the key can't edit."""
    if not (isinstance(result, dict) and result.get("queued")):
        return False
    proposal = result.get("proposal") or {}
    console.print(Text(
        f"Queued for an editor (proposal #{proposal.get('id', '?')}): "
        f"{proposal.get('summary') or 'change'}. Not applied yet."
    ))
    return True


def render_page_written(result: dict, searchable: Optional[str] = None) -> None:
    if write_queued(result):
        return
    page = result.get("page") or {}
    verb = "Created" if result.get("created") else "Updated"
    bits = [page.get("slug"), f"v{page['version']}" if page.get("version") is not None else None,
            "pinned" if page.get("pinned") else None]
    line = f"{verb} page \"{page.get('title') or page.get('slug')}\" ({' · '.join(str(b) for b in bits if b)})."
    console.print(Text(line + (f" {searchable}" if searchable else "")))


def render_resource_added(result: dict, status: Optional[int]) -> None:
    if write_queued(result):
        return
    resource = result.get("resource") or {}
    state = "already on file" if status == 200 else "new"
    console.print(Text(
        f"Attached #{resource.get('id')} {resource.get('kind') or 'RESOURCE'} "
        f"\"{resource.get('title') or resource.get('uri')}\" ({state})."
        + (" Content is ingesting in the background." if result.get("ingesting") else "")
    ))


def render_resources(payload: dict, quiet: bool = False) -> None:
    resources = payload.get("resources") or []
    if not resources:
        console.print(Text("No resources on this project."))
        return
    for resource in resources:
        role = f" · {resource['role']}" if resource.get("role") else ""
        console.print(Text(
            f"#{resource.get('id')} {resource.get('kind') or 'OTHER'}{role} "
            f"\"{resource.get('title') or 'untitled'}\" — {resource.get('uri') or ''}"
        ))
        if resource.get("summary") and not quiet:
            console.print(Text(f"    {_clip_line(resource['summary'], 200)}"))
    if payload.get("truncated"):
        console.print(Text("(first 200 shown)"))
    if not quiet:
        console.print(Text("— read one: moment project read-resource <id> [--fidelity text]"))


def render_resource_content(payload: dict) -> None:
    resource = payload.get("resource") or {}
    bits = [f"#{resource.get('id')}", payload.get("fidelity"),
            f"~{payload['tokenEstimate']} tokens" if payload.get("tokenEstimate") else None]
    console.print(Text(f"# {resource.get('title') or resource.get('uri')} ({' · '.join(str(b) for b in bits if b)})"))
    if resource.get("uri"):
        console.print(Text(str(resource["uri"])))
    if payload.get("accessNote"):
        console.print(Text(f"Access: {payload['accessNote']}"))
    content = payload.get("content")
    if isinstance(content, str):
        sys.stdout.write(content.rstrip() + "\n")
    elif content is not None:
        print_json(content)
    if payload.get("moreAvailable"):
        console.print(Text("— more available: --fidelity text, or --section <heading>"))


def _find_reader(hit: dict) -> Optional[str]:
    """The CLI call that reads a search hit in full, when there is one."""
    kind = str(hit.get("entityType") or "").upper()
    project = hit.get("projectId")
    scope = f"-p {project} " if project else ""
    if kind == "PAGE" and hit.get("ref"):
        return f"moment {scope}page-read {hit['ref']}"
    if kind == "RESOURCE" and hit.get("entityId"):
        return f"moment {scope}project read-resource {hit['entityId']}"
    if kind == "TASK" and hit.get("entityId"):
        return f"moment task show {hit['entityId']}"
    if kind == "PROJECT" and (hit.get("entityId") or project):
        return f"moment -p {hit.get('entityId') or project} orient"
    return None


def render_find(payload: dict, quiet: bool = False) -> None:
    hits = payload.get("results") or []
    if not hits:
        console.print(Text("No matches. Try other words, or widen the scope (drop -p, --types, --since)."))
        return
    for index, hit in enumerate(hits, 1):
        kind = hit.get("entityType") or "?"
        where = f"project {hit['projectId']}" if hit.get("projectId") else ""
        reader = _find_reader(hit)
        ident = f" #{hit['entityId']}" if hit.get("entityId") and not reader else ""
        line = f"{index}. {kind}{ident} \"{hit.get('title') or 'untitled'}\"" + (f" · {where}" if where else "")
        console.print(Text(line + (f" → {reader}" if reader else "")))
        if hit.get("snippet") and not quiet:
            console.print(Text(f"   {_clip_line(hit['snippet'])}"))


def render_graph(payload: dict, page: str) -> None:
    """A page's context-graph edges (backlinks + forward links)."""
    console.print(Panel(f"[bold]{page}[/]", title="Context graph", border_style=ACCENT_STRONG))
    bl = payload.get("backlinks") or []
    fw = payload.get("forward") or []
    if bl:
        console.print("[dim]Linked from:[/]")
        for e in bl:
            src = e.get("source") or {}
            console.print(f"  [{ACCENT}]←[/] {src.get('title', '?')} [dim]· {e.get('kind', '')}[/]")
    if fw:
        console.print("[dim]Links to:[/]")
        for e in fw:
            tgt = e.get("target") or {}
            console.print(f"  [{ACCENT}]→[/] {tgt.get('title', '?')} [dim]· {e.get('kind', '')}[/]")
    if not bl and not fw:
        console.print("[dim]No graph edges yet for this page.[/]")


def _falsifier_line(ref: dict) -> str:
    """One falsifier row. An OPEN falsifier is the loud one — it is still owed a test."""
    title = ref.get("title") or "(untitled)"
    signal_id = ref.get("signalId")
    if ref.get("open"):
        return f"  [{SPARK}]OPEN[/] [dim]#{signal_id}[/] {title}"
    return f"  [dim]settled #{signal_id}[/] [dim]{title}[/]"


def render_falsifiers(payload: dict, signal_id: int) -> None:
    """A claim's falsifiers — what would change our mind — plus its structural stake."""
    refs = payload.get("falsifiers") or []
    total = payload.get("total", len(refs))
    open_count = payload.get("open", sum(1 for r in refs if r.get("open")))
    stake = payload.get("structuralStake")
    header = [
        kv("Claim", f"#{payload.get('signalId', signal_id)}"),
        kv("Falsifiers", f"{open_count} open of {total}"),
    ]
    if stake is not None:
        header.append(kv("Structural stake", stake))
    console.print(Panel("\n".join(header), title="Falsifiers", border_style=ACCENT_STRONG))
    if not refs:
        console.print("[dim]Nothing has been named that would change our mind about this claim.[/]")
        return
    for ref in refs:
        console.print(_falsifier_line(ref))
    if len(refs) < total:
        console.print(f"[dim]… and {total - len(refs)} more (this list is capped).[/]")


def render_falsifier_created(payload: dict, signal_id: int) -> None:
    """The confirmation for `claim add-falsifier`, followed by the refreshed ledger."""
    falsifier = payload.get("falsifier") or {}
    lines = [
        kv("Falsifier", f"#{falsifier.get('id')}"),
        kv("Tests claim", f"#{signal_id}"),
        kv("Title", falsifier.get("title") or ""),
    ]
    console.print(Panel("\n".join(lines), title="Falsifier recorded", border_style=GLOW))
    state = payload.get("claimState") or {}
    summary = state.get("falsifiers") or {}
    if summary:
        render_falsifiers(
            {
                "signalId": signal_id,
                "falsifiers": summary.get("refs") or [],
                "total": summary.get("total", 0),
                "open": summary.get("open", 0),
                "structuralStake": state.get("structuralStake"),
            },
            signal_id,
        )


def render_contribution_result(payload: dict) -> None:
    if payload.get("message"):
        msg = payload["message"]
        lines = [
            kv("Message", msg["id"]),
            kv("Discussion", msg["discussionId"]),
            kv("Created", msg["createdAt"]),
        ]
        console.print(Panel("\n".join(lines), title="Message posted", border_style=GLOW))
        return

    if payload.get("signal"):
        sig = payload["signal"]
        lines = [
            kv("Signal", sig["id"]),
            kv("Type", sig["type"]),
            kv("Project", sig["project"]["title"]),
            kv("Created", sig["createdAt"]),
        ]
        console.print(Panel("\n".join(lines), title="Signal created", border_style=GLOW))


def render_activity(payload: dict) -> None:
    items = payload.get("activities") or payload.get("activity") or []
    if not items:
        console.print(Panel("[dim]No recent activity.[/]", title="Activity", border_style=ACCENT_STRONG))
        return

    table = Table(title="Recent activity", border_style=ACCENT_STRONG)
    table.add_column("Time", style="dim")
    table.add_column("Type", style=ACCENT)
    table.add_column("Description")

    for item in items[:20]:
        table.add_row(
            str(item.get("createdAt", "")),
            str(item.get("type", "")),
            str(item.get("description", item.get("title", ""))),
        )

    console.print(table)


def render_impact(payload: dict) -> None:
    console.print(Panel(
        "\n".join(f"{k}: {v}" for k, v in payload.items() if k != "success"),
        title="Impact",
        border_style=GLOW,
    ))


def render_timeline(payload: dict) -> None:
    entries = payload.get("timeline") or payload.get("entries") or []
    if not entries:
        console.print("[dim]No timeline data.[/]")
        return

    table = Table(title="Stats timeline", border_style=ACCENT_STRONG)
    table.add_column("Date", style="dim")
    table.add_column("Tasks", justify="right")
    table.add_column("Credits", justify="right", style=SPARK)

    for e in entries:
        table.add_row(
            str(e.get("date", "")),
            str(e.get("tasks", e.get("workUnits", 0))),
            str(e.get("credits", 0)),
        )

    console.print(table)


def render_pr_status(payload: dict) -> None:
    lines = [kv(k, v) for k, v in payload.items() if k != "success"]
    console.print(Panel("\n".join(lines) if lines else "[dim]No PR status.[/]",
                        title="PR status", border_style=GLOW))


def render_propose_result(payload: dict) -> None:
    task = payload.get("task") or payload.get("workunit") or payload.get("workUnit") or payload
    lines = [kv(k, v) for k, v in task.items() if k not in ("success",)]
    console.print(Panel("\n".join(lines) if lines else "Task proposed.",
                        title="Task proposed", border_style=GLOW))


def render_ingest_dry_run(target_url: str, envelope_preview: dict) -> None:
    lines = [
        kv("Target", target_url),
        kv("Source", envelope_preview.get("source")),
        kv("External ID", envelope_preview.get("externalId")),
        kv("Title", envelope_preview.get("title")),
        kv("Occurred at", envelope_preview.get("occurredAt")),
    ]
    console.print(Panel("\n".join(lines), title="Ingest dry run", border_style=GLOW))
    console.print_json(data={"targetUrl": target_url, "envelope": envelope_preview})


def _ingest_result_lines(payload: dict) -> list[str]:
    proposed = payload.get("proposed") or {}
    pages = proposed.get("pages") or []
    signals = proposed.get("signals") or []
    resources = proposed.get("resources") or []
    edges = proposed.get("edges") or 0
    return [
        kv("Record", payload.get("ingestRecordId")),
        kv("Status", payload.get("status")),
        kv("Candidates", f"{len(pages)} pages, {len(signals)} signals, {len(resources)} resources, {edges} edges"),
        kv("Session note", proposed.get("sessionNoteId")),
        f"[{SPARK}]Candidates pending review - not published.[/]",
    ]


def render_ingest_result(payload: dict) -> None:
    lines = _ingest_result_lines(payload)
    console.print(Panel("\n".join(lines), title="Ingest candidates", border_style=GLOW))
    if payload.get("summary"):
        console.print(Panel(payload["summary"], title="Summary", border_style=ACCENT))


def render_github_sync_summary(repo: str, results: list) -> None:
    n = len(results)
    ok = sum(1 for item in results if not item.get("error"))
    lines = [
        kv("Repo", repo),
        kv("Ingested", f"{ok}/{n} items"),
    ]
    for item in results:
        external_id = item.get("externalId") or "unknown"
        if item.get("error"):
            lines.append(f"[{ROSE}]✗ {external_id}: {item.get('error')}[/]")
        else:
            lines.append(f"[{GLOW}]✓[/] {external_id}")
    console.print(Panel("\n".join(lines), title="GitHub sync", border_style=ACCENT))


def render_ingest_watch_dry_run(target_url: str, files: list[dict]) -> None:
    lines = [
        kv("Target", target_url),
        kv("Files", len(files)),
        kv("Mode", "dry run"),
    ]
    console.print(Panel("\n".join(lines), title="Ingest watch dry run", border_style=GLOW))
    if not files:
        console.print(Panel("[dim]No matching text files found.[/]", title="Files", border_style=ACCENT))
        return

    table = Table(title="Files that would be ingested", border_style=ACCENT_STRONG, show_lines=False)
    table.add_column("File")
    table.add_column("External ID", style=ACCENT)
    table.add_column("Title", style="dim")
    for item in files:
        envelope = item.get("envelope") or {}
        table.add_row(
            str(item.get("file") or ""),
            str(item.get("externalId") or envelope.get("externalId") or ""),
            str(envelope.get("title") or ""),
        )
    console.print(table)


def render_ingest_watch_result(file: str, external_id: Any, payload: dict) -> None:
    lines = [
        kv("File", file),
        kv("External ID", external_id),
        *_ingest_result_lines(payload),
    ]
    console.print(Panel("\n".join(lines), title="Ingest watch", border_style=GLOW))
    if payload.get("summary"):
        console.print(Panel(payload["summary"], title="Summary", border_style=ACCENT))


def render_bootstrap(payload: dict) -> None:
    project = payload.get("project") or {}
    reused = payload.get("reused")
    lines = [kv("project", f"#{project.get('id')} {project.get('title','')}".strip())]
    lines.append(kv("status", "reused existing" if reused else "created new"))
    for k in ("domain", "visibility", "url"):
        if project.get(k) is not None:
            lines.append(kv(k, project.get(k)))
    page = payload.get("page")
    if page:
        lines.append(kv("page", f"{page.get('slug')} ({'created' if page.get('created') else 'updated'})"))
    resources = payload.get("resources") or []
    if resources:
        ok = sum(1 for r in resources if r.get("ok"))
        lines.append(kv("resources", f"{ok}/{len(resources)} attached"))
        for r in resources:
            if not r.get("ok"):
                lines.append(f"  [rose]✗ {r.get('uri')}: {r.get('error','failed')}[/]")
    if payload.get("handoffRefreshed"):
        lines.append(kv("handoff", "refreshed"))
    if payload.get("sessionNoteId"):
        lines.append(kv("note", f"#{payload.get('sessionNoteId')} logged"))
    console.print(Panel("\n".join(lines), title="Project bootstrapped", border_style=GLOW))


def render_create_project(payload: dict) -> None:
    project = payload.get("project") or payload
    fields = ("id", "title", "domain", "status", "visibility", "ownerId", "githubRepo", "url")
    lines = [kv(k, project.get(k)) for k in fields if project.get(k) is not None]
    next_steps = payload.get("nextSteps") or []
    if next_steps:
        lines.append("")
        lines.append("[bold]Next steps[/]")
        lines.extend(f"  • {step}" for step in next_steps)
    console.print(Panel("\n".join(lines) if lines else "Project created.",
                        title="Project created", border_style=GLOW))
