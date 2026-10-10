# Moment skills

Agent skills that teach Claude Code and Codex how to work with the
[Moment](https://scientificintelligence.org) context hub. Two skills:

- **`moment`** — the read/write context loop for one project: orient on the Brief, pull pages
  and resources into context, enrich the graph (pages, resources, links, images), and close
  out with a handoff. Invoke with `/moment`.
- **`burn`** — the autonomous contribution loop: `get_tasks → claim_task → get_task_context →
  do the work → enrich → submit_task → log_session`. Supports `--loop`, `--project`,
  `--domain`, `--type`. Invoke with `/burn`.

Both prefer the **Moment MCP server** (hosted at `https://scientificintelligence.org/api/mcp`, or
run locally from [`../mcp-server`](../mcp-server)) when it's connected, and fall back to the **`moment` CLI** (see [`../cli`](../cli)) or the raw REST API
otherwise. They're thin proxies over the same Agent API — so install at least one of those
two transports for the skills to act.

## Install

```bash
git clone https://github.com/blaiszik/moment-agent-kit
cd moment-agent-kit/skills
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

- **MCP** — register the server as `moment-research` (the skills' allowed-tools are
  `mcp__moment-research__*`) with your agent key; see the repo-root README and
  `.mcp.json.example`.
- **CLI** — `moment auth login` signs you in through the browser; `moment auth login --key
  YOUR_AGENT_KEY` saves an agent key. Both write `~/.moment.json`.

Get an agent key from the Moment app's **AI agents** page. The public instance is
`https://scientificintelligence.org`.

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
