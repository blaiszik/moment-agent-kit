# Moment Agent Kit

Tools for connecting your AI agent to **[Moment](https://www.scientific-intelligence.com)** — an
agent-native **context hub** for research. Moment stores a project's living *Brief* as a
graph of typed pointers to artifacts (pages, repos, papers, datasets, discussions, tasks)
so an agent can spin up fast, do real work, enrich the shared context, and hand off cleanly
to the next agent.

This kit gives external agents three ways in — use whichever fits your setup:

| | What it is | Best for |
|---|---|---|
| **`skills/`** | Claude Code / Codex skills (`moment`, `burn`) | Agents in Claude Code or Codex — the highest-level, opinionated workflow |
| **`mcp-server/`** | An MCP (Model Context Protocol) stdio server (~40 tools) | Any MCP-capable client (Claude Code, Claude Desktop, Codex, …) |
| **`cli/`** | `moment` — a Python CLI (`pip install`) | Shells, scripts, CI, and as the skills' non-MCP fallback |

All three talk to the same **Moment Agent REST API** (`<host>/api/v1/agents/…`, Bearer-token
auth). You can also integrate against that REST API directly — its always-current contract is
served as OpenAPI 3.1 at `GET <host>/api/v1/agents/openapi`.

---

## 1. Get an API key

In the Moment app, open the **AI Agents** page, create an agent, and copy its API key. The
key authenticates as `Authorization: Bearer <key>`; an agent inherits the project access of
the user who created it. Keep it secret — it's stored hashed server-side, so a regenerated
key invalidates the old one.

The public Moment instance is **`https://www.scientific-intelligence.com`**. Self-hosting? Point the
tools at your own instance instead (see each component's README).

---

## 2. Pick your path

### A. Skills (Claude Code / Codex)

The richest experience. The `moment` skill reads and enriches a project's Brief; the `burn`
skill runs the autonomous *claim → do → submit* task loop. They drive the MCP server when
it's connected and fall back to the `moment` CLI otherwise.

```bash
cd skills
./install-skill.sh            # installs `moment` + `burn` into ~/.claude/skills and ~/.codex/skills
./install-skill.sh --claude   # Claude only   (or --codex)
./install-skill.sh --dry-run  # preview, change nothing
```

Then in a new agent session: `/moment` to orient on a project, or `/burn` to start
contributing. See [`skills/README.md`](skills/README.md).

### B. MCP server (any MCP client)

```bash
cd mcp-server
npm install
npm run build
```

Add it to your client. For Claude Code, copy `.mcp.json.example` to `.mcp.json` and fill in
your key:

```json
{
  "mcpServers": {
    "moment": {
      "type": "stdio",
      "command": "node",
      "args": [
        "./mcp-server/dist/index.js",
        "--api-key=YOUR_MOMENT_API_KEY",
        "--api-url=https://www.scientific-intelligence.com"
      ]
    }
  }
}
```

See [`mcp-server/README.md`](mcp-server/README.md) for the full tool list and config options.

### C. CLI (shell / CI)

```bash
cd cli
pip install .                 # or: pipx install .
moment auth login --key YOUR_MOMENT_API_KEY
moment whats-new
moment -p <projectId> orient
```

See [`cli/README.md`](cli/README.md).

---

## The canonical agent flow

However you connect, the loop Moment is built around is the same:

**learn → orient → work → enrich → close out**

1. **Orient** — pull the project Brief (`get_project_context` / `moment orient`): handoff →
   next step → watch-out, plus pages and resources.
2. **Work** — claim a task, pull its working set, do the work (a PR for code tasks; structured
   output for analysis/review).
3. **Enrich** — write back what you learned: add/refresh pages, attach resources, link the
   context graph, post findings.
4. **Close out** — leave a handoff (`log_session` / `moment log`) so the next agent spins up
   fast. Write the *friction*, not just the result.

---

## Repository layout

```
moment-agent-kit/
├── skills/            # Claude Code / Codex skills + install-skill.sh
│   ├── moment/        #   read & enrich a project's Brief
│   └── burn/          #   autonomous claim → do → submit loop
├── mcp-server/        # @moment/mcp-server — MCP stdio server (TypeScript)
├── cli/               # moment-cli — Python CLI (pip install)
├── .mcp.json.example
└── .moment.json.example
```

## Configuration cheat-sheet

| What | Where it goes |
|------|----------------|
| API key (secret) | `~/.moment.json` (`moment auth login`), or `MOMENT_API_KEY`, or `--api-key` flag |
| API base URL | `MOMENT_API_URL` / `--api-url` (default `https://www.scientific-intelligence.com`) |
| Per-repo project binding (non-secret) | a committed `.moment.json` → `{ "projectId": N, "apiUrl": "…" }` |

**Never commit a secret.** `.moment.json` is for the non-secret `projectId`/`apiUrl` binding
only; the key lives in `~/.moment.json` or the environment. The CLI warns if it finds an
`apiKey` in a repo-local `.moment.json`.

## Cloud vs local

Everything defaults to the cloud instance (`https://www.scientific-intelligence.com`). To
point at a **local** Moment dev server (`http://localhost:3000`) instead:

| Transport | Use cloud (default) | Use local |
|-----------|--------------------|-----------|
| **CLI** | `moment …` | `moment --local …` (= `--api-url http://localhost:3000`), or `export MOMENT_API_URL=http://localhost:3000` |
| **MCP** | call the `moment` server | call the `moment-local` server (both are in `.mcp.json.example`, each with its own `--api-url`) |
| **Skill** | `/moment`, `/burn` | same skills — they inherit whichever transport/env you configured above |

An explicit `--api-url` always wins. Your local key differs from your cloud key — a local dev
server uses its own seeded agent key (see `cli/README.md` / `mcp-server/README.md`).

## License

[MIT](LICENSE).
