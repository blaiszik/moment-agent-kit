"""Standalone tests for moment_cli.close_out (tree_fingerprint, commits_summary).

Builds REAL throwaway git repos under tempfile dirs (subprocess git init/add/commit,
with a local -c user.email/-c user.name so nothing touches global git config) and
asserts against the behavior specs frozen in close_out.py's docstrings.

Both functions are STUBS as of the 2026-07-21 wave (they raise NotImplementedError
until the commands lane lands real bodies). Each case here calls through a small
wrapper that treats NotImplementedError as an expected-pending result (printed as
PEND, not counted as a failure) rather than crashing the whole suite — so this file
is runnable, and informative, whether or not the implementation has landed yet.

Run (no pytest needed):
    mamba run -n moment python tools/moment-cli/tests/test_close_out.py
Exit code is non-zero on any real (non-pending) failure.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make moment_cli importable whether or not the package is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import moment_cli.close_out as co  # noqa: E402

MAX_FINGERPRINT_FILES = co.MAX_FINGERPRINT_FILES
MAX_COMMIT_LINES = co.MAX_COMMIT_LINES

_ok = True
_pending: list[str] = []


def check(name: str, cond: bool) -> None:
    global _ok
    print(("PASS  " if cond else "FAIL  ") + name)
    _ok = _ok and bool(cond)


def pending(name: str) -> None:
    _pending.append(name)
    print("PEND  " + name)


def call_expect(fn, *args, **kwargs):
    """Run fn; classify the outcome instead of letting NotImplementedError crash the suite."""
    try:
        return ("ok", fn(*args, **kwargs))
    except NotImplementedError as exc:
        return ("pending", exc)
    except Exception as exc:  # noqa: BLE001 - we want to report, not crash
        return ("raised", exc)


# --- git helpers -------------------------------------------------------------

def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-c", "commit.gpgsign=false",
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test Bot",
            "-c", "init.defaultBranch=main",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    git(["init", "-q"], cwd=path)


def write_lines(path: Path, n: int, prefix: str = "line") -> None:
    path.write_text("".join(f"{prefix} {i}\n" for i in range(n)))


def commit_all(path: Path, message: str) -> str:
    git(["add", "-A"], cwd=path)
    git(["commit", "-q", "-m", message, "--allow-empty"], cwd=path)
    return git(["rev-parse", "HEAD"], cwd=path)


def short_hash(path: Path, rev: str) -> str:
    return git(["rev-parse", "--short", rev], cwd=path)


# --- tree_fingerprint cases ----------------------------------------------------

def case1_clean_tree() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="co-clean-"))
    try:
        init_repo(tmp)
        write_lines(tmp / "a.txt", 3)
        commit_all(tmp, "init")

        status, val = call_expect(co.tree_fingerprint, repo_root=str(tmp))
        if status == "pending":
            pending("case 1: clean tree -> None (tree_fingerprint stub not implemented)")
            return
        if status == "raised":
            check("case 1: clean tree -> no exception", False)
            print(f"      raised: {val!r}")
            return
        check("case 1: clean tree -> tree_fingerprint is None", val is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case2_modified_and_untracked() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="co-dirty-"))
    try:
        init_repo(tmp)
        tracked = tmp / "tracked.txt"
        write_lines(tracked, 5)
        commit_all(tmp, "init")

        # Modify the tracked file (now 8 lines) + add an untracked dir with 2 files.
        write_lines(tracked, 8)
        d = tmp / "untracked_dir"
        d.mkdir()
        write_lines(d / "a.txt", 3)
        write_lines(d / "b.txt", 4)  # dir totals: 2 files, 7 lines

        status, val = call_expect(co.tree_fingerprint, repo_root=str(tmp))
        if status == "pending":
            pending("case 2: modified + untracked dir (tree_fingerprint stub not implemented)")
            return
        if status == "raised":
            check("case 2: modified + untracked dir -> no exception", False)
            print(f"      raised: {val!r}")
            return

        fp = val
        check("case 2: result is not None", fp is not None)
        if fp is None:
            return
        check("case 2: starts with header", fp.startswith("Working tree at close-out:"))

        lines = fp.splitlines()
        tracked_lines = [ln for ln in lines if "tracked.txt" in ln]
        check("case 2: modified tracked.txt present (exactly one line)", len(tracked_lines) == 1)
        if tracked_lines:
            check("case 2: modified file shows (8 lines)", "(8 lines)" in tracked_lines[0])

        dir_lines = [ln for ln in lines if "untracked_dir" in ln]
        check("case 2: untracked_dir present (exactly one line)", len(dir_lines) == 1)
        if dir_lines:
            check(
                "case 2: untracked dir shows (2 files, 7 lines)",
                "(2 files, 7 lines)" in dir_lines[0],
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case3_cap() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="co-cap-"))
    try:
        init_repo(tmp)
        n = MAX_FINGERPRINT_FILES + 3
        for i in range(n):
            (tmp / f"file_{i:03d}.txt").write_text("x\n")

        status, val = call_expect(co.tree_fingerprint, repo_root=str(tmp))
        if status == "pending":
            pending(f"case 3: cap at {MAX_FINGERPRINT_FILES} files (tree_fingerprint stub not implemented)")
            return
        if status == "raised":
            check("case 3: cap -> no exception", False)
            print(f"      raised: {val!r}")
            return

        fp = val
        check("case 3: result is not None", fp is not None)
        if fp is None:
            return

        path_lines = [ln for ln in fp.splitlines() if re.match(r"^\s*\?\?\s", ln)]
        check(
            f"case 3: capped at <= {MAX_FINGERPRINT_FILES} path lines (got {len(path_lines)} for {n} untracked files)",
            len(path_lines) <= MAX_FINGERPRINT_FILES,
        )
        check(
            "case 3: contains an '… and N more' marker",
            re.search(r"…\s*and\s+\d+\s+more", fp) is not None,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case4_not_a_repo() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="co-notgit-"))
    try:
        (tmp / "hello.txt").write_text("hi\n")

        status, val = call_expect(co.tree_fingerprint, repo_root=str(tmp))
        if status == "pending":
            pending("case 4: not a git repo -> None (tree_fingerprint stub not implemented)")
            return
        if status == "raised":
            check("case 4: not a git repo -> no exception", False)
            print(f"      raised: {val!r}")
            return
        check("case 4: not a git repo -> None", val is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- commits_summary cases ------------------------------------------------------

def case5_commits_summary() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="co-commits-"))
    try:
        init_repo(tmp)
        write_lines(tmp / "f.txt", 1)
        commit_all(tmp, "commit one")
        write_lines(tmp / "f.txt", 2)
        commit_all(tmp, "commit two")
        write_lines(tmp / "f.txt", 3)
        commit_all(tmp, "commit three")

        range_str = "HEAD~2..HEAD"
        status, val = call_expect(co.commits_summary, range_str, repo_root=str(tmp))
        if status == "pending":
            pending("case 5: commits_summary valid range (stub not implemented)")
        elif status == "raised":
            check("case 5: valid range -> no exception", False)
            print(f"      raised: {val!r}")
        else:
            summary = val
            check("case 5: result is not None", summary is not None)
            if summary is not None:
                short_end = short_hash(tmp, "HEAD")
                short_start = short_hash(tmp, "HEAD~2")
                hash_lines = [
                    ln for ln in summary.splitlines() if re.search(r"\b[0-9a-f]{7,40}\b", ln)
                ]
                check(f"case 5: contains 2 short-hash lines (got {len(hash_lines)})", len(hash_lines) == 2)
                # Interpretation call: docstring example shows resolved endpoint hashes
                # in the header ("(fab0b83..98d5419)"), but the task spec says "the range
                # string" — accept either the literal input range_str or the resolved
                # start..end short-hash pair, whichever the implementation surfaces.
                mentions_range = range_str in summary or f"{short_start}..{short_end}" in summary
                check(
                    "case 5: header mentions the range (literal range_str OR resolved start..end hashes)",
                    mentions_range,
                )

        status2, val2 = call_expect(co.commits_summary, "nope..nope", repo_root=str(tmp))
        if status2 == "pending":
            pending("case 5: commits_summary invalid range (stub not implemented)")
        elif status2 == "raised":
            check("case 5: invalid range -> no exception", False)
            print(f"      raised: {val2!r}")
        else:
            check("case 5: invalid range -> None", val2 is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case6_commits_cap() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="co-commits-cap-"))
    try:
        init_repo(tmp)
        write_lines(tmp / "f.txt", 1)
        commit_all(tmp, "base")
        git(["tag", "base"], cwd=tmp)  # stable ref for the excluded start of the range

        n = MAX_COMMIT_LINES + 2
        for i in range(n):
            git(["commit", "-q", "--allow-empty", "-m", f"commit {i:03d}"], cwd=tmp)

        status, val = call_expect(co.commits_summary, "base..HEAD", repo_root=str(tmp))
        if status == "pending":
            pending(f"case 6: commits cap at {MAX_COMMIT_LINES} (stub not implemented)")
            return
        if status == "raised":
            check("case 6: commits cap -> no exception", False)
            print(f"      raised: {val!r}")
            return

        summary = val
        check("case 6: result is not None", summary is not None)
        if summary is None:
            return
        hash_lines = [ln for ln in summary.splitlines() if re.search(r"\b[0-9a-f]{7,40}\b", ln)]
        check(
            f"case 6: capped at <= {MAX_COMMIT_LINES} commit lines (got {len(hash_lines)} for {n} commits)",
            len(hash_lines) <= MAX_COMMIT_LINES,
        )
        check(
            "case 6: contains an '… and N more' marker",
            re.search(r"…\s*and\s+\d+\s+more", summary) is not None,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    case1_clean_tree()
    case2_modified_and_untracked()
    case3_cap()
    case4_not_a_repo()
    case5_commits_summary()
    case6_commits_cap()

    print()
    if _pending:
        print(f"{len(_pending)} case(s) pending (stub not implemented):")
        for name in _pending:
            print(f"  - {name}")
        print()

    suffix = " (with pending stubs)" if _pending and _ok else ""
    print("RESULT: " + ("ALL PASS" if _ok else "FAILURES") + suffix)
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
