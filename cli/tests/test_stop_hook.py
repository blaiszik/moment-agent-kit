"""Standalone tests for the generalized capture-hook installer (moment_cli.hooks).

Covers the code paths the `--dry-run` smoke test can't reach: the real
read-merge-write of `~/.claude/settings.json` (Stop + SessionEnd) and the Codex
`notify` dispatcher shim (payload parsing, chaining, and the config.toml repoint).
All paths are monkeypatched onto a temp dir so nothing touches the real user config.

Run (no pytest needed):
    mamba run -n moment python tools/moment-cli/tests/test_stop_hook.py
Exit code is non-zero on any failure.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# Make moment_cli importable whether or not the package is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import moment_cli.cli as c  # noqa: E402
import moment_cli.hooks as h  # noqa: E402

_ok = True


def check(name: str, cond: bool) -> None:
    global _ok
    print(("PASS  " if cond else "FAIL  ") + name)
    _ok = _ok and bool(cond)


def test_claude(tmp: Path) -> None:
    print("\n--- Claude Code: Stop + SessionEnd ---")
    settings = tmp / ".claude" / "settings.json"
    # Redirect off the real ~/.claude (both the cli shim and the hooks module read this).
    h.claude_settings_path = lambda: settings
    cmd = "moment capture --hook"  # fixed command so assertions don't depend on PATH

    # 1. Fresh install creates BOTH events.
    check("fresh install returns 'added'", h.install_claude(command=cmd) == "added")
    check("settings file created", settings.exists())
    data = json.loads(settings.read_text())
    check("Stop event installed", h._claude_event_present(data, "Stop"))
    check("SessionEnd event installed", h._claude_event_present(data, "SessionEnd"))
    check("claude_hooks_present True after install", h.claude_hooks_present(data))
    # cli back-compat shim sees it too.
    check("cli _stop_hook_present back-compat", c._stop_hook_present(data))

    # 2. Idempotent re-run: reports present, no duplicates.
    check("re-run returns 'present'", h.install_claude(command=cmd) == "present")
    data = json.loads(settings.read_text())
    check("exactly one Stop entry", len(data["hooks"]["Stop"]) == 1)
    check("exactly one SessionEnd entry", len(data["hooks"]["SessionEnd"]) == 1)

    # 3. Partial state (only Stop present) -> fills in SessionEnd, returns 'updated'.
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "moment capture --hook"}]}]},
    }))
    check("partial state -> 'updated'", h.install_claude(command=cmd) == "updated")
    data = json.loads(settings.read_text())
    check("SessionEnd added on update", h._claude_event_present(data, "SessionEnd"))
    check("Stop not duplicated on update", len(data["hooks"]["Stop"]) == 1)

    # 4. Preserve unrelated existing config (other top-level keys, other hooks).
    settings.write_text(json.dumps({
        "model": "sonnet",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo unrelated"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": []}],
        },
    }))
    check("adds alongside existing hook -> 'added'", h.install_claude(command=cmd) == "added")
    merged = json.loads(settings.read_text())
    check("preserved top-level 'model' key", merged.get("model") == "sonnet")
    check("preserved unrelated 'PreToolUse'", "PreToolUse" in merged["hooks"])
    check("kept existing Stop hook + added ours (2 entries)", len(merged["hooks"]["Stop"]) == 2)
    check("SessionEnd created alongside", h._claude_event_present(merged, "SessionEnd"))
    check("our capture hook present across both events", h.claude_hooks_present(merged))

    # 5. Malformed JSON -> 'failed' and the file is NOT clobbered.
    settings.write_text("{ this is not json")
    check("malformed settings -> 'failed'", h.install_claude(command=cmd) == "failed")
    check("malformed file left untouched", settings.read_text() == "{ this is not json")


def test_codex_shim_payload() -> None:
    print("\n--- Codex: shim payload parsing + chaining (static analysis of generated source) ---")
    moment_argv = ["/usr/local/bin/moment"]
    chained = ["/Apps/Computer Use.app/Bin/Client", "turn-ended"]
    src = h.render_codex_shim(moment_argv=moment_argv, chained=chained)

    # The shim bakes in the resolved moment argv and the chained program as literals.
    check("shim embeds moment argv", json.dumps(moment_argv) in src)
    check("shim embeds chained program", json.dumps(chained) in src)
    check("shim reads payload from argv[-1]", "sys.argv[-1]" in src)
    check("shim runs capture --hook", '["capture", "--hook"]' in src)
    check("shim forwards original argv to chained", "list(CHAINED) + sys.argv[1:]" in src)

    # Execute the generated shim against a sample {type, cwd} payload and confirm it
    # (a) runs capture in cwd and (b) forwards to the chained program.
    ns: dict = {}
    # Replace subprocess.run with a recorder so nothing actually spawns.
    calls = []

    class _FakeSub:
        @staticmethod
        def run(argv, **kw):
            calls.append({"argv": list(argv), "cwd": kw.get("cwd")})

    exec(compile(src, "<shim>", "exec"), ns)
    ns["subprocess"] = _FakeSub
    payload = {"type": "agent-turn-complete", "cwd": "/Users/x/repo", "thread-id": "t1"}
    ns["sys"].argv = ["moment-codex-notify", json.dumps(payload)]
    rc = ns["main"]()
    check("shim main() returns 0", rc == 0)
    check("capture ran with cwd from payload", any(
        cc["cwd"] == "/Users/x/repo" and cc["argv"][-2:] == ["capture", "--hook"] for cc in calls
    ))
    check("capture argv leads with moment", any(
        cc["argv"][0] == "/usr/local/bin/moment" for cc in calls if cc["cwd"] == "/Users/x/repo"
    ))
    check("chained program invoked with forwarded json", any(
        cc["argv"][0] == "/Apps/Computer Use.app/Bin/Client"
        and cc["argv"][-1] == json.dumps(payload)
        for cc in calls
    ))

    # Malformed argv JSON must not crash and must not run capture (no cwd).
    calls.clear()
    ns["sys"].argv = ["moment-codex-notify", "{not json"]
    check("malformed payload -> main() returns 0", ns["main"]() == 0)
    check("malformed payload -> capture NOT run", not any(
        cc["argv"][-2:] == ["capture", "--hook"] for cc in calls
    ))


def test_codex_config(tmp: Path) -> None:
    print("\n--- Codex: config.toml notify repoint (idempotent + non-destructive) ---")
    config = tmp / ".codex" / "config.toml"
    shim = tmp / ".local" / "bin" / "moment-codex-notify"
    h.codex_config_path = lambda: config
    h.codex_shim_path = lambda: shim
    moment_argv = ["/usr/local/bin/moment"]

    # 0. No config.toml -> create a minimal one pointing at the shim.
    out = h.install_codex(moment_argv=moment_argv)
    check("missing config -> 'added'", out["status"] == "added")
    check("shim file written", shim.exists())
    check("shim is executable", os.access(shim, os.X_OK))
    notify = h.read_codex_notify(config.read_text())
    check("notify points at shim", h.codex_notify_points_at_shim(notify))
    check("no chained target when none existed", out["chained"] is None)

    # 1. Existing config with a DIFFERENT notify + unrelated keys/comments.
    config.write_text(
        "# my codex config — keep me\n"
        'model = "gpt-5.5"\n'
        'notify = ["/Apps/Computer Use.app/Bin/Client", "turn-ended"]\n'
        "\n"
        "[tui]\n"
        "theme = \"dark\"\n"
    )
    out = h.install_codex(moment_argv=moment_argv)
    check("existing different notify -> 'updated'", out["status"] == "updated")
    check("prior program captured as chained", out["chained"] == ["/Apps/Computer Use.app/Bin/Client", "turn-ended"])
    text = config.read_text()
    notify = h.read_codex_notify(text)
    check("notify now points at shim", h.codex_notify_points_at_shim(notify))
    check("preserved comment", "# my codex config — keep me" in text)
    check("preserved unrelated 'model'", 'model = "gpt-5.5"' in text)
    check("preserved [tui] table + theme", "[tui]" in text and 'theme = "dark"' in text)
    # The shim now chains to the prior Computer Use client.
    shim_src = shim.read_text()
    check("shim chains prior Computer Use client", "/Apps/Computer Use.app/Bin/Client" in shim_src)

    # 2. Re-run: notify already points at our shim -> 'present', preserves the original
    #    chain (no self-wrap, no dropping the prior program on a re-run / CLI upgrade).
    out2 = h.install_codex(moment_argv=moment_argv)
    check("re-run when ours -> 'present'", out2["status"] == "present")
    check("re-run preserves original chained client (no self-wrap, no drop)",
          out2["chained"] == ["/Apps/Computer Use.app/Bin/Client", "turn-ended"])
    text2 = config.read_text()
    check("still exactly one top-level notify line",
          sum(1 for ln in text2.splitlines() if ln.strip().startswith("notify")) == 1)
    # Re-running must NOT replace the chained Computer Use client with the shim itself.
    check("shim still chains the ORIGINAL client (no self-wrap)",
          "/Apps/Computer Use.app/Bin/Client" in shim.read_text())

    # 3. status() reflects an installed Codex runtime.
    h.codex_detected = lambda: True
    check("codex_status() == 'present'", h.codex_status() == "present")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    test_claude(tmp)
    test_codex_shim_payload()
    test_codex_config(Path(tempfile.mkdtemp()))
    print("\nRESULT: " + ("ALL PASS" if _ok else "FAILURES"))
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
