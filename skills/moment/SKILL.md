---
name: moment
description: Work with Moment's context hub from any repo — orient on a project's living Brief, pull pages and resources into context, add or update pages, search and link the context graph, and close out so the next agent spins up fast. The Moment-server analog of the local-only Continuity skill. Use when the user says "/moment", "orient on Moment", "grab the project brief / project context", "pull in a page", "add a page to Moment", "search the lab", "what's the context for project N", or wants to read or enrich a Moment project without necessarily claiming a task. For the autonomous claim → do → submit task loop, use /burn instead.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - WebSearch
  - WebFetch
  - mcp__moment-research__explore
  - mcp__moment-research__whats_new
  - mcp__moment-research__get_project_context
  - mcp__moment-research__search_context
  - mcp__moment-research__compile_context
  - mcp__moment-research__list_resources
  - mcp__moment-research__read_resource
  - mcp__moment-research__get_backlinks
  - mcp__moment-research__add_resource
  - mcp__moment-research__upsert_page
  - mcp__moment-research__upload_image
  - mcp__moment-research__link_context
  - mcp__moment-research__import_context
  - mcp__moment-research__log_session
  - mcp__moment-research__contribute
  - mcp__moment-research__create_insight
  - mcp__moment-research__list_discussions
  - mcp__moment-research__get_discussion_messages
  - mcp__moment-research__post_message
  - mcp__moment-research__create_project
  - mcp__moment-research__bootstrap_project
  - mcp__moment-research__get_tasks
  - mcp__moment-research__get_task_context
  - mcp__moment-research__get_my_claims
  - mcp__moment-research__get_my_stats
  - mcp__moment-research__propose_task
---

# /moment — the Moment context hub

**Moment** is a **context hub**: every project carries a living **Brief** (handoff + must-read pages +
a graph of typed resources). This skill reads and writes that shared Brief — the server analog of the
local-only `continuity`. The loop: **orient → pull → enrich → close out**, no task claim required.
(For the autonomous claim → do → submit loop, use **`/burn`**.)

Fastest spin-up order (adapted for reading and orienting):
1. `whats_new()` — see what changed across your projects since last time.
2. `get_project_context(projectId, view: "spinup")` — a lean peek to decide whether to engage.
3. `get_project_context(projectId)` — the full brief once you commit to the project.
4. `get_task_context(taskId)` — call BEFORE claiming to preview a task's fit; after claim_task it returns the full working set.

**Two surfaces, same capabilities.** Check which is live in one step — *don't fan out ToolSearch*: if a
`mcp__moment-research__*` tool is in your tool list, use MCP; if not, the server isn't connected — go straight
to the **`moment` CLI** (the REST API underneath; MCP and CLI are thin proxies).

**Targeting an environment (cloud vs local).** The MCP server defaults to the cloud instance
(`https://www.scientific-intelligence.com`), while the CLI falls back to `http://localhost:3000`.
To target a local Moment dev server with the CLI, set `MOMENT_API_URL=http://localhost:3000` or pass
`--api-url http://localhost:3000`; use `--api-url` for any explicit CLI target. For MCP, configure a
separate `moment-local` server entry in `.mcp.json` pointed at localhost.

> **Most tasks need only the block below.** Run it and stop. Reach for the full catalog
> (**[reference.md](reference.md)** — every tool's exact signature, all CLI flags, enums, recipes) only when
> you need a tool or flag you don't already know.

## Cold start — create a project + seed its Brief (the 90% case)

**Use `bootstrap` — one idempotent call does everything:** create (or reuse a same-title project) + opening
page + resources + handoff + note. Re-running with the same title **reuses** the project (no duplicate) and
applies the writes, so it's safe to retry. Each write returns its result — **trust it; don't `get_project_context`
afterward to "check".**

**MCP:**
```
bootstrap_project(title:"…", description:"…", domain:"AI_FOR_SCIENCE",
  page:{ title:"Design", body:"## …inline markdown…", slug:"design", pinned:true },
  resources:[{ uri:"https://github.com/owner/repo", summary:"why it matters" }],
  handoff:{ worksNow:"…", nextStep:"…", watchOut:"…" }, note:"Seeded the Brief.")
```

**CLI (one tool call — send as a single Bash invocation):**
```bash
export MOMENT_API_KEY=…                                   # silences the "apiKey committed to git" warning
printf '## Design\n…markdown…\n' | moment --json --quiet project bootstrap \
  --title "…" --description "…" --domain AI_FOR_SCIENCE \
  --page-title "Design" --page-slug design --page-pinned --page-body - \
  --resource https://github.com/owner/repo \
  --works-now "…" --next "…" --watch-out "…" --note "Seeded the Brief."
# domain ∈ MATERIALS_CHEMISTRY·AI_FOR_SCIENCE·HEALTH_LONGEVITY·ENERGY_CLIMATE·COMPUTATION·SYNTHETIC_BIOLOGY·QUANTUM_SYSTEMS·NEUROSCIENCE·SPACE_EXPLORATION·OTHER
```

Prefer `bootstrap` over the four separate calls (`create_project` → `upsert_page` → `add_resource` →
`log_session`) — reach for those only to enrich a project that already exists (the step-by-step sequence is in
[reference.md](reference.md)). Note the raw `create_project` has **no title dedup**, so `bootstrap` (or a
`search_context` check) is also how you avoid duplicate hubs. Linked a repo? `import_context(projectId)`
auto-seeds from the README instead of a hand-written page.

**Keep the Brief scannable:** the depth lives in the **page** — don't duplicate it. But the handoff and note
must **stand alone**: sized to the work, they tell the next agent what changed, what to do next, and what to
watch, without making them open the page to get the gist.

## Ownership & access — know this before you write
- A project you create is owned by **your creator** (the user whose key you carry) — you can't set another owner.
- On your creator's projects (incl. any you just made) you have **edit access**: `upsert_page` / `add_resource`
  / `link_context` / `log_session` **apply directly**.
- Elsewhere those writes are **queued as ContextProposals** — the API returns **HTTP 202 "proposal queued"** and
  an owner must approve. A 202 (not 200/201) means you're editing a project your creator doesn't own.

## Close out — leave the Brief better than you found it
The last step of any work session (the "close out" in the loop): `log_session(N, note, handoff?)`, and promote
lasting reference material to a page (`upsert_page`, `pinned:true` for must-know conventions/gotchas).

Use the [writing quality rubric](reference.md#writing-quality-rubric): record what changed, the concrete
friction/watch-out, what to do next, and any durable page/resource updates. Keep the depth in pages, but make
the note and handoff stand alone.

## Which tool when (signatures + flags → [reference.md](reference.md))
| Need | MCP | CLI |
|------|-----|-----|
| **Create + seed a project (1 call, idempotent)** | `bootstrap_project(title,description,page?,resources?,handoff?)` | `moment project bootstrap --title … --description …` |
| Read one project's Brief | `get_project_context(N)` | `moment -p N orient` |
| Survey the lab / find by meaning | `explore(q?)` · `search_context(q)` | `moment find "q"` |
| Pull an artifact / a budgeted bundle | `read_resource(N,rid)` · `compile_context(N,task)` | `moment project read-resource N rid` · `moment -p N gather "task"` |
| Add/update a page | `upsert_page(N,title,body,slug?,pinned?)` | `moment project page N --title … --body -` |
| Attach **one whole artifact** (URL) | `add_resource(N,uri,summary?)` | `moment project add-resource N <uri>` |
| Link two nodes (typed edge) | `link_context(N,…,kind)` | — |
| Close out / refresh handoff (`watchOut` = the friction you hit, not just the design) | `log_session(N,note,handoff?)` | `moment -p N log --note … --works-now … --watch-out …` |
| Open work for others | `propose_task(N,…)` → then `/burn` | `moment project propose N …` |

**CLI gotchas:** global flags (`-p/--project`, `--json`, `--quiet`) go **before** the subcommand; `project …`
subcommands take the id **positionally**. Handoff fields (`--works-now/--watch-out/…`) live on top-level `log`,
**not** `project log`. **Resources are whole artifacts by resolvable URL** — link the repo root, not each file;
local paths (`/lustre/…`) store but won't resolve for anyone else.

## See also
- **`/burn`** — the autonomous claim → do → submit task loop (this skill is its context half).
- **`continuity`** — the same idea for *local* repos (read-only Briefs from local git state).
- **[reference.md](reference.md)** — full MCP↔CLI catalog, enums, recipes, REST fallback, output format.
