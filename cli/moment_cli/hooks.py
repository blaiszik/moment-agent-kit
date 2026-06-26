"""Generalized capture-hook installer — make ambient capture automatic across agent runtimes.

One opt-in (`moment hooks install`, also called by `moment onboard`) detects the agent
runtimes present on the machine and installs the right capture hook for each, idempotently.

Design memo: plans/context-engine-and-signals/auto-capture-design.md

A "runtime" is a tiny adapter that knows how to:
  - detect()   -> is this runtime present on the machine?
  - status()   -> "present" | "absent" | "failed" | "not-detected"
  - install()  -> "added" | "updated" | "present" | "failed" | "not-detected"
  - describe() -> a one-line human description of where/what it writes

Two runtimes ship today:
  - Claude Code: a `Stop` + `SessionEnd` hook in ~/.claude/settings.json running
    `moment capture --hook` (preserves all other hooks/keys).
  - Codex: a `notify` dispatcher shim at ~/.local/bin/moment-codex-notify that runs
    `moment capture --hook` in the turn's cwd, then forwards to any previously-configured
    notify program (so we never clobber e.g. the Computer Use client).

Everything here is read-merge-write and re-runnable; capture itself only ever records git
drift (never the transcript), so a no-op turn writes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Callable, Optional

# The capture command every runtime ultimately runs. Resolved to an absolute path
# at install time (hook environments often have a stripped PATH).
CAPTURE_ARGS = ["capture", "--hook"]


# --------------------------------------------------------------------------- #
# Resolving the `moment` invocation (hook PATH is often stripped)
# --------------------------------------------------------------------------- #

def resolve_moment_command() -> list[str]:
    """Best-effort absolute invocation for `moment`, as an argv list.

    Hooks run under a stripped environment, so a bare `moment` can silently fail.
    Prefer an absolute `moment` on PATH; fall back to `python -m moment_cli` using
    the current interpreter; last resort, a bare `moment` (still works if PATH is sane).
    """
    found = shutil.which("moment")
    if found:
        return [found]
    import sys

    # `python -m moment_cli` is equivalent to the console script.
    return [sys.executable, "-m", "moment_cli"]


def capture_command_string() -> str:
    """The shell command string a Claude Code hook runs (resolved `moment capture --hook`)."""
    return " ".join(_shquote(part) for part in (resolve_moment_command() + CAPTURE_ARGS))


def _shquote(part: str) -> str:
    if part and all(c.isalnum() or c in "-_./=" for c in part):
        return part
    return "'" + part.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Claude Code runtime — Stop + SessionEnd hooks in ~/.claude/settings.json
# --------------------------------------------------------------------------- #

# The session-end-ish events we install the capture hook on. `Stop` fires every
# turn-end (primary); `SessionEnd` is a belt-and-suspenders flush on graceful exit.
CLAUDE_EVENTS = ("Stop", "SessionEnd")
_CAPTURE_MARKER = "moment capture"  # idempotency marker inside a hook command


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _claude_event_present(data: dict, event: str) -> bool:
    """True iff an `event` hook whose command mentions `moment capture` exists."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_hooks = entry.get("hooks")
        if not isinstance(entry_hooks, list):
            continue
        for hook in entry_hooks:
            if isinstance(hook, dict) and _CAPTURE_MARKER in str(hook.get("command", "")):
                return True
    return False


def claude_hooks_present(data: dict) -> bool:
    """True iff our capture hook is installed for *every* required Claude event."""
    return all(_claude_event_present(data, event) for event in CLAUDE_EVENTS)


# Back-compat alias for the original single-event helper name (tests + onboard import it).
def stop_hook_present(data: dict) -> bool:
    return claude_hooks_present(data)


def claude_detected() -> bool:
    return claude_settings_path().exists() or shutil.which("claude") is not None


def claude_status() -> str:
    """present | absent | failed | not-detected (read-only)."""
    if not claude_detected():
        return "not-detected"
    settings_path = claude_settings_path()
    if not settings_path.exists():
        return "absent"
    try:
        data = json.loads(settings_path.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return "failed"
    if not isinstance(data, dict):
        return "failed"
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return "failed"
    if isinstance(hooks, dict):
        for event in CLAUDE_EVENTS:
            entries = hooks.get(event)
            if entries is not None and not isinstance(entries, list):
                return "failed"
    return "present" if claude_hooks_present(data) else "absent"


def install_claude(*, command: Optional[str] = None) -> str:
    """Merge Stop + SessionEnd capture hooks into ~/.claude/settings.json.

    Returns added | updated | present | failed. Idempotent: any event already
    carrying a `moment capture` command is left untouched; all other hooks and
    top-level keys are preserved.
    """
    command = command or capture_command_string()
    settings_path = claude_settings_path()
    try:
        data: dict = {}
        if settings_path.exists():
            data = json.loads(settings_path.read_text() or "{}")
        if not isinstance(data, dict):
            return "failed"

        # How many of our events were already present *before* we touch anything?
        already = sum(1 for event in CLAUDE_EVENTS if _claude_event_present(data, event))
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            return "failed"

        added_any = False
        for event in CLAUDE_EVENTS:
            entries = hooks.setdefault(event, [])
            if not isinstance(entries, list):
                return "failed"
            if _claude_event_present(data, event):
                continue
            entries.append({"hooks": [{"type": "command", "command": command}]})
            added_any = True

        if not added_any:
            return "present"

        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        # "added": none of our events were present before; "updated": we filled in a
        # missing event alongside one that already existed.
        return "added" if already == 0 else "updated"
    except Exception:
        return "failed"


# --------------------------------------------------------------------------- #
# Codex runtime — notify dispatcher shim + ~/.codex/config.toml repoint
# --------------------------------------------------------------------------- #

def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def codex_shim_path() -> Path:
    return Path.home() / ".local" / "bin" / "moment-codex-notify"


def codex_detected() -> bool:
    return codex_config_path().exists() or shutil.which("codex") is not None


# --- TOML notify read/write (targeted, comment-preserving) --------------------
#
# Python <3.13 has no stdlib TOML *writer*, and we must not destroy a user's
# hand-maintained config.toml (comments, ordering, other keys). So we read the
# notify value with tomllib, and rewrite ONLY the top-level `notify` line(s) with
# a line-oriented splice. Everything else in the file is preserved byte-for-byte.

def read_codex_notify(text: str) -> Optional[list]:
    """Parse the top-level `notify` array from a config.toml's text. None if unset/invalid."""
    try:
        import tomllib

        data = tomllib.loads(text)
    except Exception:
        return None
    value = data.get("notify")
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return None


def _toml_str(s: str) -> str:
    """Render a Python string as a TOML basic string (double-quoted, escaped)."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + out + '"'


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_str(str(i)) for i in items) + "]"


def _splice_notify(text: str, items: list[str]) -> str:
    """Return config.toml text with the top-level `notify` set to `items`.

    Removes any existing top-level `notify = [...]` (handles a multi-line array) that
    precedes the first `[table]` header, then inserts the new value into the top-level
    section — *before* the first table header, so it stays at top level. Preserves
    everything else — comments, ordering, other keys, and any `notify` inside a sub-table
    (which TOML scopes differently and we never touch).
    """
    lines = text.splitlines()
    out: list[str] = []
    first_table_idx: Optional[int] = None  # index in `out` of the first table header
    in_table = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        is_table_header = stripped.startswith("[") and not stripped.startswith("[[")
        if is_table_header and not in_table:
            in_table = True
            first_table_idx = len(out)
        # Only a *top-level* `notify =` (before any table header) is the slot we own.
        if (
            not in_table
            and stripped.startswith("notify")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == "notify"
        ):
            # Skip this assignment, including a multi-line array continuation.
            rhs = stripped.split("=", 1)[1]
            open_brackets = rhs.count("[") - rhs.count("]")
            i += 1
            while open_brackets > 0 and i < n:
                open_brackets += lines[i].count("[") - lines[i].count("]")
                i += 1
            continue
        out.append(line)
        i += 1

    new_line = f"notify = {_toml_array(items)}"
    if first_table_idx is not None:
        # Insert at top level, just before the first table header (with a blank gap).
        head = out[:first_table_idx]
        tail = out[first_table_idx:]
        head_text = "\n".join(head).rstrip()
        block = (head_text + "\n" if head_text else "") + new_line + "\n"
        tail_text = "\n".join(tail).strip("\n")
        return block + "\n" + tail_text + "\n"
    body = "\n".join(out).rstrip()
    if body:
        return body + "\n" + new_line + "\n"
    return new_line + "\n"


# --- The shim itself ----------------------------------------------------------

def render_codex_shim(*, moment_argv: list[str], chained: Optional[list[str]]) -> str:
    """Generate the `moment-codex-notify` dispatcher shim source.

    Codex invokes `notify` as `program [args...] <json>`, where the final argv is a
    JSON event ({type, cwd, ...}). The shim:
      1. Parses argv[-1] JSON; on `agent-turn-complete`, runs `moment capture --hook`
         in `cwd` (best-effort — NEVER fails the session).
      2. Forwards the *entire original argv* to any previously-configured notify
         program (`chained`), so e.g. the Computer Use client keeps working.

    `moment_argv` / `chained` are baked in as literals at install time so the shim is
    self-contained and PATH-independent.
    """
    moment_literal = json.dumps(moment_argv)
    chained_literal = json.dumps(chained) if chained else "None"
    return f'''#!/usr/bin/env python3
"""Moment Codex notify dispatcher — generated by `moment hooks install`. Do not edit.

Codex calls this as `moment-codex-notify <json>` once per turn. We run ambient git
capture in the turn's cwd, then forward to any previously-configured notify program.
Never fails the session.
"""
import json
import subprocess
import sys

MOMENT_ARGV = {moment_literal}
CHAINED = {chained_literal}


def _payload():
    if len(sys.argv) < 2:
        return {{}}
    try:
        return json.loads(sys.argv[-1])
    except Exception:
        return {{}}


def main():
    payload = _payload()
    cwd = payload.get("cwd")
    event = payload.get("type")
    # Capture git drift for the repo the turn ran in (best-effort, non-fatal).
    if event in (None, "agent-turn-complete") and cwd:
        try:
            subprocess.run(MOMENT_ARGV + ["capture", "--hook"], cwd=cwd, timeout=60)
        except Exception:
            pass
    # Forward the original argv to the previously-configured notify program.
    if CHAINED:
        try:
            subprocess.run(list(CHAINED) + sys.argv[1:], timeout=60)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
'''


def codex_shim_present() -> bool:
    """True iff our shim file exists and is the Moment dispatcher."""
    path = codex_shim_path()
    if not path.exists():
        return False
    try:
        head = path.read_text(errors="replace")[:400]
    except OSError:
        return False
    return "Moment Codex notify dispatcher" in head


def read_shim_chained() -> Optional[list]:
    """Extract the baked-in CHAINED target from the existing shim (None if unset/absent).

    Lets a re-run regenerate the shim WITHOUT dropping the previously-chained program
    (e.g. the Computer Use client).
    """
    path = codex_shim_path()
    if not path.exists():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    import re as _re

    match = _re.search(r"^CHAINED = (.+)$", text, _re.MULTILINE)
    if not match:
        return None
    raw = match.group(1).strip()
    if raw == "None":
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, list) else None


def codex_notify_points_at_shim(notify: Optional[list]) -> bool:
    if not notify:
        return False
    return Path(str(notify[0])).resolve() == codex_shim_path().resolve()


def codex_status() -> str:
    """present | absent | failed | not-detected (read-only)."""
    if not codex_detected():
        return "not-detected"
    config_path = codex_config_path()
    if not config_path.exists():
        return "absent"
    try:
        text = config_path.read_text()
    except OSError:
        return "failed"
    notify = read_codex_notify(text)
    if codex_notify_points_at_shim(notify) and codex_shim_present():
        return "present"
    return "absent"


def install_codex(*, moment_argv: Optional[list[str]] = None) -> dict:
    """Install the Codex notify dispatcher shim and repoint config.toml at it.

    Returns {"status": added|updated|present|failed, "chained": [...]|None, "shim": path}.

    Idempotent: if `notify` already points at our shim, we only refresh the shim file
    (no-op repoint). If it points elsewhere, that program is captured as the chained
    target so it keeps running. The config.toml edit preserves all other keys/comments.
    """
    moment_argv = moment_argv or resolve_moment_command()
    config_path = codex_config_path()
    shim_path = codex_shim_path()
    try:
        existed = config_path.exists()
        text = config_path.read_text() if existed else ""
        notify = read_codex_notify(text)

        already_ours = codex_notify_points_at_shim(notify)
        # The chained target: whatever notify pointed at before, unless it's already us —
        # in which case preserve the chain the existing shim already baked in (don't
        # self-wrap, and don't drop the prior program on a re-run / CLI upgrade).
        if already_ours:
            chained = read_shim_chained()
        else:
            chained = list(notify) if notify else None

        # Write/refresh the shim (always, so an upgraded CLI refreshes the baked-in argv).
        shim_path.parent.mkdir(parents=True, exist_ok=True)
        shim_path.write_text(render_codex_shim(moment_argv=moment_argv, chained=chained))
        _make_executable(shim_path)

        # Repoint notify at the shim (idempotent / non-destructive).
        if already_ours:
            status = "present"
            if not existed:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(_splice_notify(text, [str(shim_path)]))
                status = "added"
        else:
            new_text = _splice_notify(text, [str(shim_path)])
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(new_text)
            status = "updated" if existed else "added"

        return {"status": status, "chained": chained, "shim": str(shim_path)}
    except Exception:
        return {"status": "failed", "chained": None, "shim": str(shim_path)}


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------- #
# Runtime registry
# --------------------------------------------------------------------------- #

class Runtime:
    def __init__(
        self,
        key: str,
        label: str,
        detect: Callable[[], bool],
        status: Callable[[], str],
        install: Callable[[], dict],
        describe: Callable[[], str],
    ) -> None:
        self.key = key
        self.label = label
        self.detect = detect
        self.status = status
        self.install = install
        self.describe = describe


def _claude_install_adapter() -> dict:
    cmd = capture_command_string()
    status = install_claude(command=cmd)
    return {"status": status, "command": cmd, "target": str(claude_settings_path())}


def _claude_describe() -> str:
    return f"Stop + SessionEnd hooks in {claude_settings_path()}"


def _codex_describe() -> str:
    return f"notify dispatcher shim ({codex_shim_path()}) via {codex_config_path()}"


def registry() -> list[Runtime]:
    return [
        Runtime(
            key="claude",
            label="Claude Code",
            detect=claude_detected,
            status=claude_status,
            install=_claude_install_adapter,
            describe=_claude_describe,
        ),
        Runtime(
            key="codex",
            label="Codex",
            detect=codex_detected,
            status=codex_status,
            install=install_codex,
            describe=_codex_describe,
        ),
    ]


def runtime_keys() -> list[str]:
    return [r.key for r in registry()]


def select_runtimes(only: Optional[list[str]]) -> list[Runtime]:
    """All runtimes, or the subset named in `only` (raises ValueError on unknown key)."""
    runtimes = registry()
    if not only:
        return runtimes
    by_key = {r.key: r for r in runtimes}
    selected: list[Runtime] = []
    for key in only:
        key = key.strip().lower()
        if key not in by_key:
            raise ValueError(f"Unknown runtime '{key}'. Known: {', '.join(by_key)}.")
        selected.append(by_key[key])
    return selected
