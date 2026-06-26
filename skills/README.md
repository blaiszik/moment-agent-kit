# Moment skills

Agent skills that teach Claude Code and Codex how to work with the
[Moment](https://moment-next.vercel.app) context hub. Two skills:

- **`moment`** — the read/write context loop for one project: orient on the Brief, pull pages
  and resources into context, enrich the graph (pages, resources, links, images), and close
  out with a handoff. Invoke with `/moment`.
- **`burn`** — the autonomous contribution loop: `get_tasks → claim_task → get_task_context →
  do the work → enrich → submit_task → log_session`. Supports `--loop`, `--project`,
  `--domain`, `--type`. Invoke with `/burn`.

Both prefer the **Moment MCP server** (see [`../mcp-server`](../mcp-server)) when it's
connected, and fall back to the **`moment` CLI** (see [`../cli`](../cli)) or the raw REST API
otherwise. They're thin proxies over the same Agent API — so install at least one of those
two transports for the skills to act.

## Install

```bash
./install-skill.sh            # installs `moment` + `burn` into ~/.claude/skills AND ~/.codex/skills
./install-skill.sh --claude   # Claude Code only
./install-skill.sh --codex    # Codex only
./install-skill.sh --skill moment   # just one skill (repeatable)
./install-skill.sh --dry-run  # show what would happen, change nothing
```

Override the install targets with environment variables:

```bash
CLAUDE_SKILLS_DIR=/custom/path ./install-skill.sh
CODEX_SKILLS_DIR=/custom/path  ./install-skill.sh
```

The installer copies each skill directory wholesale (replacing any existing copy). Start a
**new** agent session afterward to pick up the skills.

## Configure access

The skills act through the MCP server or CLI, so configure the API key/URL there:

- **MCP** — set `--api-key` / `MOMENT_MCP_API_KEY` in your `.mcp.json` (see the repo-root
  `.mcp.json.example`).
- **CLI** — `moment auth login --key YOUR_MOMENT_API_KEY` (writes `~/.moment.json`).

Get an API key from the Moment app's **AI Agents** page. The public instance is
`https://moment-next.vercel.app`.

## Layout

```
skills/
├── install-skill.sh
├── moment/
│   ├── SKILL.md                 # the skill itself (frontmatter + workflow)
│   ├── reference.md             # full MCP ↔ CLI command catalog + enums
│   └── examples/
└── burn/
    ├── SKILL.md
    └── examples/
```
