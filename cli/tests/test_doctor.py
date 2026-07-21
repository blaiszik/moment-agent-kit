"""Standalone tests for moment_cli.doctor (run_doctor / CheckResult).

Never hits the network beyond localhost on ports we bind-and-release ourselves
(get_unused_port). cwd and env (including HOME, so ~/.moment.json never leaks
into a case) are monkeypatched onto temp dirs via os.chdir / os.environ, always
restored in a finally block.

run_doctor is a STUB as of the 2026-07-21 wave (it raises NotImplementedError
until the commands lane lands a real body). Each case calls through a wrapper
that treats NotImplementedError as an expected-pending result (printed as PEND,
not counted as a failure) so this file is runnable, and informative, whether or
not the implementation has landed yet.

Run (no pytest needed):
    mamba run -n moment python tools/moment-cli/tests/test_doctor.py
Exit code is non-zero on any real (non-pending) failure.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Make moment_cli importable whether or not the package is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from moment_cli.doctor import CheckResult, run_doctor  # noqa: E402

_ok = True
_pending: list[str] = []

# Vars that feed moment_cli.config's resolution cascade (see config.py) plus HOME
# (~/.moment.json carries the real secret key on this machine) — always stripped
# before a case applies its own env, so nothing ambient leaks in.
_ISOLATE_VARS = [
    "MOMENT_API_KEY", "MOMENT_API_URL",
    "MOMENT_MCP_API_KEY", "MOMENT_MCP_KEY", "MOMENT_MCP_API_URL",
    "MCP_MOMENT_API_URL", "MOMENT_PROJECT_ID",
    "HOME",
]


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


def get_unused_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@contextmanager
def sandboxed(cwd: Path, home: Path, env: dict):
    """chdir into cwd + isolate MOMENT_*/HOME env, always restored."""
    old_cwd = os.getcwd()
    old_env = dict(os.environ)
    try:
        os.chdir(cwd)
        for k in _ISOLATE_VARS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(home)
        os.environ.update(env)
        yield
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def by_name(results: list[CheckResult], name: str) -> CheckResult | None:
    for r in results:
        if r.name == name:
            return r
    return None


def _mkdirs(prefix: str) -> tuple[Path, Path]:
    cwd = Path(tempfile.mkdtemp(prefix=f"doc-{prefix}-cwd-"))
    home = Path(tempfile.mkdtemp(prefix=f"doc-{prefix}-home-"))
    return cwd, home


def _write_mcp_config(cwd: Path, port: int) -> None:
    (cwd / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "moment-research": {
                "command": "moment-mcp",
                "args": ["--api-url", f"http://localhost:{port}"],
            }
        }
    }))


# --- case 1: no .moment.json anywhere -------------------------------------------

def case1_no_binding() -> None:
    # Interpretation call: the landed run_doctor() gates "later checks skip when
    # earlier ones fail" only on the checks that actually *depend* on the failed
    # one — auth needs a resolved key + a reachable API; project needs a bound
    # projectId. api-key and api-reach are each independently evaluated (a key
    # can resolve from env/home with no .moment.json at all; api-reach always
    # probes *some* URL, defaulting to localhost:3000 when nothing else is
    # configured). So with no .moment.json AND an env key present: binding fails,
    # api-key still resolves (ok), api-reach still runs (against an explicit
    # unused-port override here, so it never touches the real localhost:3000 dev
    # server) and fails, and only auth/project — the two checks with a real
    # prerequisite — skip.
    cwd, home = _mkdirs("nocfg")
    try:
        port = get_unused_port()
        with sandboxed(cwd, home, {"MOMENT_API_KEY": "mk_test_shouldnotleak_0001"}):
            status, val = call_expect(run_doctor, api_url_override=f"http://127.0.0.1:{port}")

        if status == "pending":
            pending("case 1: no .moment.json anywhere (doctor stub not implemented)")
            return
        if status == "raised":
            check("case 1: run_doctor never raises", False)
            print(f"      raised: {val!r}")
            return

        results = val
        check("case 1: run_doctor never raises", True)

        binding = by_name(results, "binding")
        check("case 1: binding check present", binding is not None)
        if binding is not None:
            check("case 1: binding -> fail", binding.status == "fail")

        api_key = by_name(results, "api-key")
        check("case 1: api-key -> ok (resolves from env despite no binding)", api_key is not None and api_key.status == "ok")

        api_reach = by_name(results, "api-reach")
        check(
            "case 1: api-reach -> fail (unused-port override, never the real :3000 dev server)",
            api_reach is not None and api_reach.status == "fail",
        )

        for name in ("auth", "project"):
            r = by_name(results, name)
            check(f"case 1: {name} -> skip", r is not None and r.status == "skip")

        check(
            "case 1: no detail leaks the fake key",
            not any("shouldnotleak" in (r.detail or "") for r in results),
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


# --- case 2: valid binding, api unreachable --------------------------------------

def case2_unreachable_api() -> None:
    cwd, home = _mkdirs("unreach")
    try:
        port = get_unused_port()
        (cwd / ".moment.json").write_text(json.dumps({
            "projectId": 42,
            "apiUrl": f"http://127.0.0.1:{port}",
        }))

        with sandboxed(cwd, home, {"MOMENT_API_KEY": "mk_test_unreach_0002"}):
            status, val = call_expect(run_doctor)

        if status == "pending":
            pending("case 2: unreachable api-reach (doctor stub not implemented)")
            return
        if status == "raised":
            check("case 2: run_doctor never raises", False)
            print(f"      raised: {val!r}")
            return

        results = val
        binding = by_name(results, "binding")
        check("case 2: binding -> ok", binding is not None and binding.status == "ok")

        api_reach = by_name(results, "api-reach")
        check("case 2: api-reach -> fail", api_reach is not None and api_reach.status == "fail")
        if api_reach is not None:
            detail_l = (api_reach.detail or "").lower()
            check(
                "case 2: api-reach detail surfaces a connection error",
                "connect" in detail_l or "refused" in detail_l,
            )

        for name in ("auth", "project"):
            r = by_name(results, name)
            check(f"case 2: {name} -> skip", r is not None and r.status == "skip")

        check(
            "case 2: no detail leaks the fake key",
            not any("unreach_0002" in (r.detail or "") for r in results),
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


# --- case 3: mcp-config agreement (the :3010-drift class) -----------------------

def case3_mcp_config_agreement() -> None:
    # 3a: mismatched ports -> fail, detail mentions both.
    cwd, home = _mkdirs("mcp-mismatch")
    try:
        port_a = get_unused_port()
        port_b = get_unused_port()
        while port_b == port_a:
            port_b = get_unused_port()
        (cwd / ".moment.json").write_text(json.dumps({"projectId": 42, "apiUrl": f"http://localhost:{port_a}"}))
        _write_mcp_config(cwd, port_b)

        with sandboxed(cwd, home, {"MOMENT_API_KEY": "mk_test_mcp_0003"}):
            status, val = call_expect(run_doctor)

        if status == "pending":
            pending("case 3a: mcp-config mismatch (doctor stub not implemented)")
        elif status == "raised":
            check("case 3a: run_doctor never raises", False)
            print(f"      raised: {val!r}")
        else:
            mcp = by_name(val, "mcp-config")
            check("case 3a: mcp-config -> fail", mcp is not None and mcp.status == "fail")
            if mcp is not None:
                detail = mcp.detail or ""
                check(
                    "case 3a: detail mentions both ports",
                    str(port_a) in detail and str(port_b) in detail,
                )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)

    # 3b: matching ports -> ok.
    cwd, home = _mkdirs("mcp-match")
    try:
        port = get_unused_port()
        (cwd / ".moment.json").write_text(json.dumps({"projectId": 42, "apiUrl": f"http://localhost:{port}"}))
        _write_mcp_config(cwd, port)

        with sandboxed(cwd, home, {"MOMENT_API_KEY": "mk_test_mcp_0004"}):
            status, val = call_expect(run_doctor)

        if status == "pending":
            pending("case 3b: mcp-config match (doctor stub not implemented)")
        elif status == "raised":
            check("case 3b: run_doctor never raises", False)
            print(f"      raised: {val!r}")
        else:
            mcp = by_name(val, "mcp-config")
            check("case 3b: mcp-config -> ok", mcp is not None and mcp.status == "ok")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)

    # 3c: no .mcp.json at all -> skip.
    cwd, home = _mkdirs("mcp-none")
    try:
        port = get_unused_port()
        (cwd / ".moment.json").write_text(json.dumps({"projectId": 42, "apiUrl": f"http://localhost:{port}"}))

        with sandboxed(cwd, home, {"MOMENT_API_KEY": "mk_test_mcp_0005"}):
            status, val = call_expect(run_doctor)

        if status == "pending":
            pending("case 3c: mcp-config absent (doctor stub not implemented)")
        elif status == "raised":
            check("case 3c: run_doctor never raises", False)
            print(f"      raised: {val!r}")
        else:
            mcp = by_name(val, "mcp-config")
            check("case 3c: mcp-config -> skip", mcp is not None and mcp.status == "skip")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


# --- case 4: secret hygiene -------------------------------------------------------

def case4_secret_hygiene() -> None:
    cwd, home = _mkdirs("secret")
    try:
        port = get_unused_port()
        (cwd / ".moment.json").write_text(json.dumps({
            "projectId": 42,
            "apiUrl": f"http://127.0.0.1:{port}",
        }))

        with sandboxed(cwd, home, {"MOMENT_API_KEY": "mk_live_supersecret123"}):
            status, val = call_expect(run_doctor)

        if status == "pending":
            pending("case 4: secret hygiene (doctor stub not implemented)")
            return
        if status == "raised":
            check("case 4: run_doctor never raises", False)
            print(f"      raised: {val!r}")
            return

        results = val
        check(
            "case 4: no check detail anywhere contains the secret",
            not any("supersecret" in (r.detail or "") for r in results),
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    case1_no_binding()
    case2_unreachable_api()
    case3_mcp_config_agreement()
    case4_secret_hygiene()

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
