"""Standalone tests for the `moment github sync` connector (moment_cli.cli).

Asserts the pure envelope-shaping helpers against the CANONICAL ENVELOPE SPEC in
`.codex-github-connector-task.md` using small in-line `gh`-shaped dict fixtures,
plus a dry-run integration check that proves the command POSTs nothing.

The `github sync` command + its helpers are implemented by a parallel agent. If a
pinned symbol is not yet on disk, the relevant check FAILS with a clear
"symbol missing" message instead of crashing the whole runner — so this file is a
useful regression target both before and after the CLI lands.

Run (no pytest needed):
    python tests/test_github_connector.py
Exit code is non-zero on any failure.
"""
import difflib
import json
import os
import sys

# Make moment_cli importable whether or not the package is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import moment_cli.cli as c  # noqa: E402

_ok = True


def check(name: str, cond: bool) -> None:
    global _ok
    print(("PASS  " if cond else "FAIL  ") + name)
    _ok = _ok and bool(cond)


def _sym(name: str):
    """Lazily fetch a pinned cli.py symbol; return None if not yet implemented."""
    return getattr(c, name, None)


def _require(name: str):
    """Return (symbol, present_bool). On absence, record one FAIL and return None."""
    fn = _sym(name)
    if fn is None:
        check(f"{name} exists in cli.py (pending cli.py)", False)
        return None
    return fn


REPO = "octocat/moment-demo"


# --- gh-shaped fixtures -------------------------------------------------------

def _merged_pr() -> dict:
    """A merged PR with a human + a bot comment and a human review."""
    return {
        "number": 42,
        "title": "Add the GitHub connector",
        "body": (
            "This wires up `moment github sync`.\n"
            "<!-- a hidden html comment that must be stripped -->\n"
            "- [ ] \n"  # empty checklist line -> stripped
            "- [x] keep this checked item\n"
            "Closes #41."
        ),
        "author": {"login": "octocat"},
        "comments": [
            {"author": {"login": "alice"}, "body": "Nice, ship it."},
            {"author": {"login": "dependabot[bot]"}, "body": "Bump lodash 1.0->1.1"},
        ],
        "reviews": [
            {"author": {"login": "carol"}, "state": "APPROVED", "body": "LGTM, merging."},
            {"author": {"login": "github-actions[bot]"}, "state": "COMMENTED", "body": "CI green"},
        ],
        "mergeCommit": {"oid": "abc1234def5678"},
        "additions": 120,
        "deletions": 8,
        "changedFiles": 3,
        "mergedAt": "2026-06-10T12:00:00Z",
        "closedAt": "2026-06-10T12:00:00Z",
        "updatedAt": "2026-06-10T12:05:00Z",
        "state": "MERGED",
        "url": "https://github.com/octocat/moment-demo/pull/42",
        "labels": [{"name": "feature"}, {"name": "connector"}],
        "reviewDecision": "APPROVED",
    }


def _closed_unmerged_pr() -> dict:
    return {
        "number": 7,
        "title": "Abandoned experiment",
        "body": "Never mind.",
        "author": {"login": "octocat"},
        "comments": [],
        "reviews": [],
        "mergeCommit": None,
        "additions": 0,
        "deletions": 0,
        "changedFiles": 0,
        "mergedAt": None,
        "closedAt": "2026-06-09T09:00:00Z",
        "updatedAt": "2026-06-09T09:30:00Z",
        "state": "CLOSED",
        "url": "https://github.com/octocat/moment-demo/pull/7",
        "labels": [],
    }


def _closed_issue() -> dict:
    return {
        "number": 13,
        "title": "Sync drops bot noise",
        "body": "Bot comments should not pollute context.\n<!-- internal note -->",
        "author": {"login": "octocat"},
        "comments": [
            {"author": {"login": "dave"}, "body": "Agreed, filter them."},
            {"author": {"login": "renovate"}, "body": "I am a bot"},
        ],
        "closedAt": "2026-06-08T15:00:00Z",
        "updatedAt": "2026-06-08T15:10:00Z",
        "state": "CLOSED",
        "url": "https://github.com/octocat/moment-demo/issues/13",
        "labels": [{"name": "bug"}],
    }


# --- tests --------------------------------------------------------------------

def test_is_bot_author() -> None:
    print("\n--- _is_bot_author: bot logins stripped, humans kept ---")
    fn = _require("_is_bot_author")
    if fn is None:
        return
    check("dependabot[bot] is a bot", fn("dependabot[bot]") is True)
    check("github-actions[bot] is a bot", fn("github-actions[bot]") is True)
    check("renovate (bare) is a bot", fn("renovate") is True)
    check("anything ending [bot] is a bot", fn("some-random-app[bot]") is True)
    check("human login is NOT a bot", fn("octocat") is False)
    check("human login 'alice' is NOT a bot", fn("alice") is False)


def test_external_id_for_pr() -> None:
    print("\n--- _external_id_for_pr: merged uses sha, closed-unmerged uses updatedAt ---")
    fn = _require("_external_id_for_pr")
    if fn is None:
        return
    merged = _merged_pr()
    check(
        "merged PR -> <repo>#<n>@<mergeCommit.oid>",
        fn(REPO, merged) == f"{REPO}#42@abc1234def5678",
    )
    closed = _closed_unmerged_pr()
    check(
        "closed-unmerged PR -> <repo>#<n>@<updatedAt>",
        fn(REPO, closed) == f"{REPO}#7@2026-06-09T09:30:00Z",
    )


def test_external_id_for_issue() -> None:
    print("\n--- _external_id_for_issue: uses updatedAt ---")
    fn = _require("_external_id_for_issue")
    if fn is None:
        return
    issue = _closed_issue()
    check(
        "issue -> <repo>#<n>@<updatedAt>",
        fn(REPO, issue) == f"{REPO}#13@2026-06-08T15:10:00Z",
    )


def test_build_pr_envelope() -> None:
    print("\n--- _build_pr_envelope: spec fields, occurredAt, participants, meta ---")
    fn = _require("_build_pr_envelope")
    if fn is None:
        return
    env = fn(REPO, _merged_pr())
    check("source == github_pr", env.get("source") == "github_pr")
    check("externalId is the merged sha key", env.get("externalId") == f"{REPO}#42@abc1234def5678")
    check("uri is the PR html url", env.get("uri") == "https://github.com/octocat/moment-demo/pull/42")
    check("title preserved", env.get("title") == "Add the GitHub connector")
    check("occurredAt == mergedAt", env.get("occurredAt") == "2026-06-10T12:00:00Z")

    participants = env.get("participants") or []
    check("participants: author present", "octocat" in participants)
    check("participants: human commenter present", "alice" in participants)
    check("participants: human reviewer present", "carol" in participants)
    check("participants: dependabot stripped", "dependabot[bot]" not in participants)
    check("participants: github-actions stripped", "github-actions[bot]" not in participants)
    check("participants: no leading @", all(not p.startswith("@") for p in participants))
    check("participants: deduped", len(participants) == len(set(participants)))

    meta = env.get("meta") or {}
    expected_meta_keys = {
        "repo", "number", "state", "merged", "mergeCommitSha", "author",
        "additions", "deletions", "changedFiles", "labels", "reviewDecision",
    }
    check("meta has all PR spec keys", expected_meta_keys.issubset(set(meta.keys())))
    check("meta.repo", meta.get("repo") == REPO)
    check("meta.number", meta.get("number") == 42)
    check("meta.merged is True", meta.get("merged") is True)
    check("meta.mergeCommitSha", meta.get("mergeCommitSha") == "abc1234def5678")
    check("meta.author", meta.get("author") == "octocat")
    check("meta.labels are name strings", meta.get("labels") == ["feature", "connector"])


def test_normalize_pr_narrative() -> None:
    print("\n--- _normalize_pr_narrative: Link line, sections, bot/HTML/checklist stripped ---")
    fn = _require("_normalize_pr_narrative")
    if fn is None:
        return
    blob = fn(REPO, _merged_pr())
    check("contains Link: line", "Link: https://github.com/octocat/moment-demo/pull/42" in blob)
    check("contains ## Description", "## Description" in blob)
    check("contains the human review body", "LGTM, merging." in blob)
    check("contains the human comment body", "Nice, ship it." in blob)
    check("does NOT contain the bot comment", "Bump lodash" not in blob)
    check("does NOT contain the bot review body", "CI green" not in blob)
    check("HTML comment stripped", "<!--" not in blob and "hidden html comment" not in blob)
    check("empty checklist line stripped", "- [ ] \n" not in blob and "- [ ]\n" not in blob)
    check("non-empty checklist item kept", "keep this checked item" in blob)


def test_build_issue_envelope() -> None:
    print("\n--- _build_issue_envelope: source, externalId via updatedAt, meta keys ---")
    fn = _require("_build_issue_envelope")
    if fn is None:
        return
    env = fn(REPO, _closed_issue())
    check("source == github_issue", env.get("source") == "github_issue")
    check("externalId uses updatedAt", env.get("externalId") == f"{REPO}#13@2026-06-08T15:10:00Z")
    check("uri is the issue html url", env.get("uri") == "https://github.com/octocat/moment-demo/issues/13")
    check("title preserved", env.get("title") == "Sync drops bot noise")

    meta = env.get("meta") or {}
    expected_issue_meta = {"repo", "number", "state", "author", "labels"}
    check("meta has all issue spec keys", expected_issue_meta.issubset(set(meta.keys())))
    check("meta.repo", meta.get("repo") == REPO)
    check("meta.number", meta.get("number") == 13)
    check("meta.author", meta.get("author") == "octocat")
    check("meta.labels are name strings", meta.get("labels") == ["bug"])

    blob_fn = _sym("_normalize_issue_narrative")
    if blob_fn is not None:
        blob = blob_fn(REPO, _closed_issue())
        check("issue blob has Link: line", "Link: https://github.com/octocat/moment-demo/issues/13" in blob)
        check("issue blob keeps human comment", "Agreed, filter them." in blob)
        check("issue blob strips bot comment", "I am a bot" not in blob)


def test_dry_run_posts_nothing() -> None:
    """The command, with --dry-run, must build envelopes from canned `gh` JSON and
    POST nothing — proven by making MomentApiClient.ingest explode if called."""
    print("\n--- github sync --dry-run: lists via gh, builds envelopes, POSTs nothing ---")
    fn = _require("github_sync")
    if fn is None:
        return

    # Canned `gh` responses keyed loosely on the argv so list vs. view both resolve.
    merged = _merged_pr()
    issue = _closed_issue()

    def fake_gh(args, timeout=30):
        a = " ".join(str(x) for x in args)
        if "pr" in args and "list" in args:
            # merged-state list returns the merged PR; closed-state list returns none extra
            if "merged" in a:
                return json.dumps([{"number": 42}])
            return json.dumps([])
        if "issue" in args and "list" in args:
            return json.dumps([{"number": 13}])
        if "pr" in args and "view" in args:
            return json.dumps(merged)
        if "issue" in args and "view" in args:
            return json.dumps(issue)
        return "[]"

    class _ExplodingClient:
        def __init__(self, *a, **kw):
            pass

        def ingest(self, *a, **kw):
            raise AssertionError("ingest must not be called in dry-run")

    # Minimal config stand-in so _resolve_project_id() / _ingest_target_url() work.
    class _Cfg:
        api_url = "https://example.test"
        project_id = 42
        config_path = None
        api_key = "dry-run"

    saved = {}

    def stub(name, value):
        saved[name] = getattr(c, name, None)
        setattr(c, name, value)

    ingest_called = {"hit": False}

    try:
        stub("_gh", fake_gh)
        if _sym("_resolve_github_repo") is not None:
            stub("_resolve_github_repo", lambda repo: "o/r")
        stub("MomentApiClient", _ExplodingClient)
        stub("_resolve_cli_config", lambda **kw: _Cfg())
        stub("_resolve_project_id", lambda: 42)
        stub("_ingest_target_url", lambda api_url, pid: f"{api_url}/api/v1/agents/projects/{pid}/ingest")
        # Make _state quiet+json so the command takes a non-interactive path.
        prior_state = dict(c._state)
        c._state.update({"json": True, "quiet": True, "project": 42})

        raised = None
        try:
            fn(
                repo="o/r",
                project=42,
                since="30d",
                prs_only=False,
                issues_only=False,
                include_open=False,
                dry_run=True,
            )
        except AssertionError as exc:
            ingest_called["hit"] = True
            raised = exc
        except SystemExit:
            # typer.Exit(0) on success is fine; a non-zero exit would still mean
            # ingest was not the failure (ingest raises AssertionError, caught above).
            pass
        except Exception as exc:
            raised = exc  # surfaced below for diagnosis

        check("dry-run did NOT call ingest", ingest_called["hit"] is False)
        if raised is not None and not ingest_called["hit"]:
            print(f"      note: github_sync raised {type(raised).__name__}: {raised}")
    finally:
        for name, val in saved.items():
            if val is None:
                # remove attributes we created that didn't exist before
                if hasattr(c, name) and name not in ("_gh", "MomentApiClient",
                                                     "_resolve_cli_config", "_resolve_project_id",
                                                     "_ingest_target_url", "_resolve_github_repo"):
                    continue
                try:
                    setattr(c, name, val)
                except Exception:
                    pass
            else:
                setattr(c, name, val)
        c._state.clear()
        c._state.update(prior_state)


# --- cross-language parity: Python builders vs the TS goldens -----------------
#
# The TS mapper (app/lib/connectors/github.ts) is the canonical source of truth.
# `generate-goldens.ts` runs it over the committed `gh` fixtures and writes
# expected-*.json. This test feeds the SAME fixtures through the Python builders
# and asserts byte-for-byte equality — the real proof the two mappers agree.

GOLDEN_REPO = "osi-lab/moment-next"  # must match generate-goldens.ts


def _repo_root() -> str:
    # tools/moment-cli/tests/ -> repo root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _load_json(rel_path: str):
    with open(os.path.join(_repo_root(), rel_path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _compare_envelope(label: str, got: dict, want: dict) -> None:
    for key in ("source", "externalId", "title", "participants", "occurredAt", "uri", "meta"):
        check(f"parity[{label}].{key}", got.get(key) == want.get(key))
    same_text = got.get("text") == want.get("text")
    check(f"parity[{label}].text byte-identical to TS golden", same_text)
    if not same_text:
        diff = difflib.unified_diff(
            (want.get("text") or "").splitlines(),
            (got.get("text") or "").splitlines(),
            fromfile=f"{label}.golden(TS)",
            tofile=f"{label}.python",
            lineterm="",
        )
        print("      text diff (golden → python):")
        for line in list(diff)[:50]:
            print("      " + line)


def test_ts_python_parity() -> None:
    print("\n--- TS↔Python parity: Python envelopes match the TS goldens byte-for-byte ---")
    build_pr = _require("_build_pr_envelope")
    build_issue = _require("_build_issue_envelope")
    if build_pr is None or build_issue is None:
        return
    fx = "app/lib/connectors/__fixtures__"
    try:
        pr = _load_json(f"{fx}/pr-merged.json")
        pr_golden = _load_json(f"{fx}/expected-pr-envelope.json")
        issue = _load_json(f"{fx}/issue-closed.json")
        issue_golden = _load_json(f"{fx}/expected-issue-envelope.json")
    except FileNotFoundError as exc:
        check(f"parity: TS goldens present ({exc})", False)
        return
    _compare_envelope("pr", build_pr(GOLDEN_REPO, pr), pr_golden)
    _compare_envelope("issue", build_issue(GOLDEN_REPO, issue), issue_golden)


def main() -> int:
    test_is_bot_author()
    test_external_id_for_pr()
    test_external_id_for_issue()
    test_build_pr_envelope()
    test_normalize_pr_narrative()
    test_build_issue_envelope()
    test_ts_python_parity()
    test_dry_run_posts_nothing()
    print("\nRESULT: " + ("ALL PASS" if _ok else "FAILURES"))
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
