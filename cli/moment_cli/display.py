"""Rich-based display helpers."""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

# Color palette matching the original theme
ACCENT = "#72d6ff"
ACCENT_STRONG = "#3bb8f5"
SPARK = "#ffd166"
GLOW = "#8ce99a"
ROSE = "#ff8fab"
READ_NOW_COUNT = 3


def print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def print_error(message: str, status: Optional[int] = None) -> None:
    prefix = f"Moment API error ({status})" if status else "Error"
    err_console.print(f"[bold red]{prefix}:[/] {message}")


def print_error_json(error: str, status: Optional[int] = None, details: Any = None) -> None:
    payload = {"error": error}
    if status is not None:
        payload["status"] = status
    if details is not None:
        payload["details"] = details
    sys.stderr.write(json.dumps(payload, indent=2, default=str) + "\n")


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
        kv("Recent insights", len(payload.get("recentInsights") or [])),
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
        raw = (handoff.get("rawMarkdown") or "").strip()
        if raw:
            # The full handoff is the real thing — render it as markdown.
            console.print(Panel(Markdown(raw), title="Handoff", border_style=GLOW,
                                subtitle=f"[dim]{footer}[/]"))
        else:
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
            hl += ["", f"[dim]{footer}[/]"]
            console.print(Panel("\n".join(hl), title="Handoff", border_style=GLOW))
    else:
        console.print(Panel(
            "[dim]No handoff yet — run a session and `moment log` to create one.[/]",
            title="Handoff", border_style=GLOW))

    if quiet:
        return

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
                          "✓" if pg.get("agentRead") else "")
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

    console.print(
        f"[dim]~{meta.get('tokenEstimate', '?')} tokens · "
        "`moment orient --prompt` for the agent-ready brief · "
        "`moment sync` to mirror to .continuity/[/]")


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
        source = str((item.get("provenance") or {}).get("sourceType") or entity.get("type") or "")
        locator = item.get("locator") or {}
        where = locator.get("label") or locator.get("href") or item.get("uri") or ""
        line = (
            f"[bold]{item.get('title', '(untitled)')}[/]"
            + (f" [dim]· {source.lower()}[/]" if source else "")
            + f" {chip(authority, AUTHORITY_STYLE.get(authority, ACCENT_STRONG))}"
            + f" {chip(freshness, FRESHNESS_STYLE.get(freshness, 'dim'))}"
            + (f" [dim]· {where}[/]" if where else "")
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
    """A single page in full — title header, then the raw markdown body on stdout."""
    page = payload.get("page") or {}
    title = page.get("title") or page.get("slug") or "(untitled)"
    bits = [b for b in (page.get("role"), f"/{page.get('slug')}" if page.get("slug") else None) if b]
    console.print(Panel(f"[bold]{title}[/]" + (f"\n[dim]{' · '.join(str(b) for b in bits)}[/]" if bits else ""), border_style=ACCENT_STRONG))
    body = page.get("body") or ""
    if body.strip():
        sys.stdout.write(body.rstrip() + "\n")
    else:
        console.print("[dim](this page is empty)[/]")


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

    if payload.get("insight"):
        ins = payload["insight"]
        lines = [
            kv("Insight", ins["id"]),
            kv("Type", ins["type"]),
            kv("Project", ins["project"]["title"]),
            kv("Created", ins["createdAt"]),
        ]
        console.print(Panel("\n".join(lines), title="Insight created", border_style=GLOW))


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
