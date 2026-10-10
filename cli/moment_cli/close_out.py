"""Close-out enrichment — git truth appended to session notes.

STUB (wave 2026-07-21): the commands lane replaces bodies WITHOUT changing
signatures. Tests import from here.

Why this module exists: handoffs state beliefs about the working tree that the
tree contradicts (the 2026-07-20 case: "campaign-closure work untouched" hid a
560-line, near-complete lib). `moment log` therefore appends a compact,
mechanical tree fingerprint — and, when asked, a commit-range summary — so the
next agent calibrates against facts, not prose.

Both functions are pure-ish helpers: they run git read-only, never raise (a
close-out must never fail because git hiccuped), and return None when there is
nothing to say (clean tree / empty range / not a git repo).
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

# Caps keep the note compact — a fingerprint, not a diff.
MAX_FINGERPRINT_FILES = 20
MAX_COMMIT_LINES = 20

# Short — a close-out must never hang on a git hiccup.
_GIT_TIMEOUT = 5


def _run_git(args: list[str], cwd: str) -> Optional[str]:
    """Run a read-only git command; return stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _discover_repo_root(repo_root: Optional[str]) -> Optional[str]:
    start = repo_root or os.getcwd()
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    if not out or not out.strip():
        return None
    return out.strip()


def _count_lines(path: str) -> Optional[int]:
    """Line count of a text file, or None if unreadable / binary (null-byte sniff)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\x00" in raw:  # best-effort binary sniff
        return None
    return len(raw.decode("utf-8", errors="replace").splitlines())


def _dir_stats(dirpath: str) -> tuple[int, int]:
    """(text-file count, total line count) for an untracked directory, skipping binaries."""
    files = 0
    lines = 0
    for root, _dirs, names in os.walk(dirpath):
        for name in names:
            count = _count_lines(os.path.join(root, name))
            if count is None:  # binary / unreadable — skip, best effort
                continue
            files += 1
            lines += count
    return files, lines


def _unquote_porcelain_path(path: str) -> str:
    """Porcelain quotes paths with special chars in double quotes; unquote best-effort."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        try:
            return path[1:-1].encode().decode("unicode_escape")
        except Exception:
            return path[1:-1]
    return path


def tree_fingerprint(repo_root: Optional[str] = None) -> Optional[str]:
    """Compact working-tree summary for a close-out note.

    Returns a block like:

        Working tree at close-out:
          M  app/lib/campaigns/close.ts (560 lines)
          ?? app/api/campaigns/[slug]/close/ (3 files, 151 lines)
          (clean otherwise)

    - One line per modified/untracked path from `git status --porcelain`,
      capped at MAX_FINGERPRINT_FILES (then "… and N more").
    - Modified files carry their current line count; untracked directories
      carry file count + total line count (text files only, best effort).
    - Returns None when the tree is clean or git is unavailable.
    """
    root = _discover_repo_root(repo_root)
    if not root:
        return None
    status = _run_git(["status", "--porcelain"], cwd=root)
    if status is None:
        return None

    raw_entries = [line for line in status.split("\n") if line.strip()]
    if not raw_entries:
        return None  # clean tree — nothing to say

    lines: list[str] = []
    for line in raw_entries[:MAX_FINGERPRINT_FILES]:
        code = line[:2]
        rest = line[3:] if len(line) > 3 else line[2:].lstrip()
        # Renames render as "old -> new"; the new path is what exists on disk.
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rel = _unquote_porcelain_path(rest)
        abspath = os.path.join(root, rel)

        if rel.endswith("/"):
            files, total = _dir_stats(abspath)
            detail = f"{files} file{'s' if files != 1 else ''}, {total} line{'s' if total != 1 else ''}"
            lines.append(f"  {code} {rel} ({detail})")
        else:
            count = _count_lines(abspath)
            if count is None:
                lines.append(f"  {code} {rel}")
            else:
                lines.append(f"  {code} {rel} ({count} line{'s' if count != 1 else ''})")

    extra = len(raw_entries) - MAX_FINGERPRINT_FILES
    if extra > 0:
        lines.append(f"  … and {extra} more")

    return "Working tree at close-out:\n" + "\n".join(lines)


def commits_summary(range_str: str, repo_root: Optional[str] = None) -> Optional[str]:
    """`git log --oneline <range>` as an indented block for a close-out note.

    Returns a block like:

        Commits this session (fab0b83..98d5419):
          98d5419 Probe: teams deploy signatures; ...
          ...

    Capped at MAX_COMMIT_LINES (then "… and N more"). Returns None when the
    range is empty or invalid (never raises).
    """
    if not range_str or not range_str.strip() or range_str.strip().startswith("-"):
        return None  # a range never starts with "-"; git would read it as an option
    root = _discover_repo_root(repo_root)
    if not root:
        return None
    out = _run_git(["log", "--oneline", range_str.strip()], cwd=root)
    if out is None:
        return None
    commits = [line for line in out.split("\n") if line.strip()]
    if not commits:
        return None  # empty range

    lines = [f"  {c}" for c in commits[:MAX_COMMIT_LINES]]
    extra = len(commits) - MAX_COMMIT_LINES
    if extra > 0:
        lines.append(f"  … and {extra} more")

    return f"Commits this session ({range_str.strip()}):\n" + "\n".join(lines)
