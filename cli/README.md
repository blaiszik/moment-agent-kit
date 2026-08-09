# moment — Moment CLI

`moment` is a Python command-line client for the [Moment](https://www.scientific-intelligence.com)
context hub. It talks to the same Agent REST API as the MCP server, and is what the `moment`
and `burn` skills fall back to when MCP isn't connected. It's also handy on its own in shells
and CI.

## Install

```bash
pip install .          # from this directory
# or, isolated:
pipx install .
```

Requires Python ≥ 3.10. This installs a `moment` console command (entry point
`moment_cli.cli:app`).

## Authenticate

Get an API key from the Moment app's **AI Agents** page, then:

```bash
moment auth login --key YOUR_MOMENT_API_KEY   # writes ~/.moment.json (secret)
moment auth whoami                            # confirm the key resolves
```

With no explicit target, the CLI falls back to `http://localhost:3000` (a local dev
server). To target the cloud instance, set `MOMENT_API_URL=https://www.scientific-intelligence.com`
or pass the global `--api-url` flag.

### Cloud vs local

```bash
moment --api-url https://www.scientific-intelligence.com activity   # one-off against the cloud
moment -p 42 orient                                                 # localhost:3000 fallback
export MOMENT_API_URL=https://www.scientific-intelligence.com       # or make it sticky for the shell session
```

An explicit `--api-url` wins over the environment. Your local server has its own seeded
agent key (distinct from your cloud key) — authenticate against it with
`MOMENT_API_KEY=<LOCAL_DEV_KEY>` or a separate `moment auth login`.

## Configuration cascade

Resolution order (first non-empty wins): **flags → repo `.moment.json` → environment →
`~/.moment.json`**.

- **`~/.moment.json`** — your secret: `{ "apiKey": "…" }` (written by `moment auth login`).
- **repo `.moment.json`** — a committed, **non-secret** binding: `{ "projectId": N, "apiUrl": "…" }`.
  The CLI warns if it finds an `apiKey` here, since the file is git-tracked.
- **Env vars** — `MOMENT_API_KEY`, `MOMENT_API_URL`, `MOMENT_PROJECT_ID`.

Most commands take a project via the global `-p/--project` flag (or the repo binding). Add
`--json` for machine-readable output and `--quiet` to suppress decoration.

## Common commands

```bash
# Orient & read
moment -p 42 orient                       # the spin-up brief: handoff → next → watch-out
moment project context 42                 # full project context
moment find "transformer attention"       # semantic search across the whole graph
moment -p 42 gather "retraction lint"     # budgeted context bundle within one project
moment project resources 42               # the resource manifest
moment project read-resource 42 12        # pull a resource into context
moment backlinks 42 <page-slug>           # typed edges for a page

# Enrich
moment project page 42 --title "Findings" --body-file notes.md   # add/update a page (--body - reads stdin)
moment project add-resource 42 https://github.com/owner/repo --summary "Authoritative source"
moment project contribute 42 ...          # post an observation / hypothesis / finding
moment project bootstrap ...              # idempotent create + seed page/resources/handoff

# The work loop
moment work list                          # AI-eligible tasks
moment work start <taskId>                # claim
moment work context <taskId>              # the task's working set
moment work submit <taskId> ...           # submit structured output
moment work pr <taskId> <prUrl>           # submit a GitHub PR for a code task

# Close out
moment -p 42 log --note "…" --next "…" --watch-out "…"   # session note + handoff patch
moment sync                               # regenerate any local mirror
```

Command groups: `auth`, `work`, `project`, `stats`, `hooks`, `github`. Run `moment --help`
or `moment <group> --help` for the full surface.

## Tests

```bash
pip install pypdf            # used by the ingest tests
python -m pytest tests       # or run a file directly, e.g. python tests/test_stop_hook.py
```
