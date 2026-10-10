# /moment reference

Open this file when you need a tool's exact parameters, an enum, a recipe, or the REST fallback. For CLI flags, `moment commands` (or `moment <command> --help`) is authoritative, because it is generated from the parser.

## Arguments (when invoked as `/moment …`)

| Argument | Effect |
|---|---|
| `N`, an alias, `--project=…`, or `-p …` | The project to work on |
| `--query="…"` or a quoted phrase | Search the whole graph by meaning (`search_context`) instead of opening one project |
| `--brief` | Print the project's brief and stop |

With no project and no query: in an attached repository, run `moment project list --for "<the user's task>"`. Elsewhere, use `get_updates` or `list_projects`, and ask only if the project still can't be inferred.

## Attach a repository

`.moment.json` is a committed, non-secret routing table at the repository root:

```json
{
  "defaultProject": "platform",
  "projects": {
    "platform": {
      "id": 42,
      "title": "Moment — Living Context Engine",
      "description": "Context engine, CLI, and agent skills",
      "keywords": ["context", "cli", "skill"],
      "paths": ["tools/moment-cli", ".claude/skills/moment"]
    },
    "event": {
      "id": 140,
      "description": "Campaign and participant experience",
      "keywords": ["campaign", "event", "submission"],
      "paths": ["app/campaigns"]
    }
  }
}
```

Add `"apiUrl"` only to point the repository at a server other than the lab (for example a local dev server). It outranks `MOMENT_API_URL`; see [servers](references/servers.md). API keys never go in this file.

Project selection: explicit `-p <id|alias>` (or `MOMENT_PROJECT`) → the best unique match of path and task hint → `defaultProject` → the only attached project. `moment project list --for "…"` prints the candidates, the choice, and the reason without calling the server. `orient --for`, `gather <task>`, and `packet <question>` use the same selector. Ids and aliases match exactly; descriptions and keywords only rank.

### Natural-language init (agent contract)

When the user says "initialize this repo on Moment project 42", perform the attachment; don't hand back a command to run.

1. Find the git root and read any existing `.moment.json`. Keep every unrelated entry and the existing `apiUrl`; change servers only when asked.
2. Read the project before writing (`orient(42, view:"spinup")` or `moment -p 42 orient`). If it is missing, inaccessible, or on an ambiguous server, stop and explain rather than writing an unverified binding.
3. Derive the metadata:
   - **alias**: a memorable 1–3 word lowercase slug from the title or product name (not `project-42` when a real name exists);
   - **title**: the Moment project title;
   - **description**: one sentence on when work in this repository routes to this project;
   - **keywords**: 3–6 discriminating lowercase terms (skip generic words such as `project`, `repo`, `research`, `work`);
   - **paths**: `.` only when the project owns the whole repository; otherwise the clearly owned directories, or none when unsure.
4. Avoid collisions: update an alias already pointing to this id; if the best alias names another id, pick another. Make the project the default only when it is the first attachment or the user calls it the default.
5. Run one non-interactive command, with `--no-claude --no-hooks` unless the user also asked for startup instructions or capture hooks:

   ```bash
   moment --json init -p 42 --alias platform --title "Moment — Living Context Engine" \
     --description "Moment platform, context engine, CLI, and agent skills" \
     --keyword moment --keyword context --keyword cli --keyword skill \
     --path tools/moment-cli --path .claude/skills/moment --default \
     --no-claude --no-hooks
   ```

   The JSON result reports the alias, project id, whether it is the default, and any legacy binding it migrated.
6. Verify with `moment --json project list --for "<the repo's likely work>"` and report the alias, default status, and selection reason. Initialization changes local routing only; don't write to the project unless the user separately asked for that.

With no project id from the user, use `get_updates` / `list_projects` and repository clues. Proceed only when one candidate is clearly supported; otherwise show the short list and ask.

## Transports

MCP tools and the CLI are both thin clients of the agent REST API (`/api/v1/agents/…`).

- Over MCP, context tools return a readable brief above a `--- structured (JSON) ---` fence. Read the prose, and parse the JSON only for fields.
- The CLI prints that brief when piped (the lean `spinup` view unless you pass `--view`), and the whole payload with `--json`.
- REST fallback: the base URL comes from [servers](references/servers.md), with `Authorization: Bearer <key>`. `GET /api/v1/agents/openapi` (no auth) lists every route. The reads you will want are `GET …/projects/{id}/context`, `GET …/projects/{id}/pages/{slug}`, `GET …/projects/{id}/pages?view=full` (every page body), and `GET …/projects/{id}/resources/{rid}`.

Any node a Moment surface names (a read order entry, packet item, backlink, or search hit) is readable in one call: a page with `read_page`, a resource with `read_resource`. If you are about to guess a route or look for credentials, you have missed the tool.

## Tool catalog

`[PROJECT]` in a CLI form is an optional id or alias; it falls back to `-p`, then to the repository binding.

### Find and orient

| MCP | CLI | Notes |
|---|---|---|
| `orient(projectId, view?)` | `moment orient [--short\|--complete] [--for "<task>"]` | CLI `--short` = MCP `view:"spinup"` (about 4 KB; the CLI's default when piped): next step, summary, what changed. CLI `--complete` = MCP `view:"brief"` (the MCP default): handoff, recent notes, page list, and must-read page bodies; uncapped, 40 KB or more on busy projects. MCP `view:"full"` is capped near 28K characters, with page bodies cut to excerpts, so it holds less page text than `brief`. `nextCalls` names useful follow-ups. |
| `get_updates(limit?)` | `moment status` (one project) | What changed across your projects since you last looked. |
| `list_projects(query?, domain?)` | `moment project list --remote [--mine]` | The newest 100 projects the key can see, filtered by substring and domain. It is not a search; use `search_context` for that. |
| `search_context(query, projectId?\|campaign?\|user?\|team?, types?, since?, limit?)` | `moment find "<q>" [-p …\|--team\|--campaign\|--user] [--types] [--since] [-n]` | At most one scope. `types` ∈ PROJECT · PAGE · RESOURCE · SESSION_NOTE · SIGNAL · SUBMISSION · DISCUSSION · TASK. `since` is an ISO date or `30d` / `12h`. `limit` 1–50 (default 8). Over MCP, `team`, `types`, and `since` are currently ignored; use `moment find` when those filters matter. |
| `get_record(projectId\|campaignSlug, limit?, after?, kinds?, actor?, since?)` | `moment record [-p …\|--campaign …]` | The append-only record, newest first. |
| `get_team_context(team)`, `get_user_context(user?)`, `get_campaign_context(campaign)` | — | Team, person, and campaign briefs. An event's campaign brief adds its entry window, what an entry needs, and the judging criteria. |
| `get_submission_status(campaignSlug, projectId)` | `moment campaign submission status [-p …] [--campaign …]` | Read-only: whether a team's event entry is ready, what it still needs (`needs[].sentence`; `situational` ones are event facts, not chores), whether saved changes are waiting to go live (`hasUnpublishedChanges`), and the window. Filing is the person's act (`moment campaign submission submit`). |

### Read

| MCP | CLI | Notes |
|---|---|---|
| `read_page(projectId, slug)` | `moment page-read [PROJECT] SLUG` | One page: full markdown body and version. The cheapest read once anything names a slug. |
| `list_resources(projectId)` | `moment project resources [PROJECT]` | The manifest: kind, title, summary, URI. |
| `read_resource(projectId, resourceId, fidelity?, section?)` | `moment project read-resource [PROJECT] RID [--fidelity] [--section]` | `fidelity`: `summary` (default) · `metadata` · `preview` · `text` (the full content; a PDF resolves to its extracted text). `section`: one heading. |
| `get_backlinks(projectId, page)` | `moment backlinks SLUG` | A page's typed edges, in and out. |
| `list_open_questions(projectId, status?)` | — | `open` (default) · `answered` · `all`. |
| `get_claim_state(signalId)`, `find_evidence(signalId, stance?)` | `moment claim show\|evidence <id>` | A claim's state, and its evidence `for` · `against` · `all`. |
| `list_discussions(projectId)`, `get_discussion_messages(projectId, discussionId?, limit?)` | `moment discussion list\|read [PROJECT]` | Project discussion threads. |

### Evidence packets

| MCP | CLI | Notes |
|---|---|---|
| `compile_packet(projectId, question, tokenBudget?, maxNodes?, depth?, save?)` | `moment packet "<question>" [--budget] [--max-nodes] [--depth] [--save]` | Budgeted evidence rather than ranked context: each item carries `authority`, `freshness`, `locator`, `provenance`, and why it matched. Read `caveats` first; they name what the budget cut. No synthesized answer. `tokenBudget` 200–16000 (4000), `maxNodes` 1–100 (40), `depth` 1–3 (2). |
| `save_packet(projectId, packet)` | `moment packet "<q>" --save` | Freezes an immutable snapshot at `/projects/{id}/packets/{packetId}`; needs edit access and at least one evidence item. |
| `list_packets(projectId, limit?, packetId?)` | `moment packet-list`, `moment packet-get ID` | With `packetId`, one snapshot in full. |

`authority` ∈ REVIEWED (human-approved) · CURATED (human-authored) · UNREVIEWED (ambient notes, raw discussion). `freshness` ∈ CURRENT · STALE · UNKNOWN.

### Write

| MCP | CLI | Notes |
|---|---|---|
| `upsert_page(projectId, title, body, slug?, icon?, pinned?, role?, ifVersion?)` | `moment page [PROJECT] --title … --body -\|--body-file … [--slug] [--pinned/--unpinned] [--role] [--if-version]` | The same slug updates in place. `pinned` makes the page must-read, pulled in full into `orient`; omit it to leave the pin unchanged. `role` ∈ BRIEF · SPEC · PLAN · DECISION · REPORT · REFERENCE · NOTE (inferred when omitted). A stale `ifVersion` returns 409 `version_conflict` with the current page. Markdown supports `![](url)` images and YouTube links. |
| `upload_image(projectId, base64, filename?)` | — | Hosts a figure and returns a URL to embed. |
| `add_resource(projectId, uri, title?, kind?, summary?, role?, accessNote?, isWorkSurface?)` | `moment project add-resource [PROJECT] URI [--kind] [--role] [--summary]` | One whole artifact by resolvable URL, deduplicated, kind auto-detected. `kind` ∈ DATASET · CODE_REPO · PAPER · PROTOCOL · MODEL · COMPUTE_ENV · NOTEBOOK · DOCUMENT · ENDPOINT · DASHBOARD · OTHER. `role` ∈ BACKGROUND · INPUT · OUTPUT · TOOL · REFERENCE. |
| `link_context(projectId, from, to, kind, note?, remove?)` | — | A typed edge between refs: `page:slug`, `resource:<id or uri>`, `signal:id`, `task:id`, `session_note:id`, `submission:id`, `discussion:id`, `packet:id`, `project:<this project>`, or a bare slug. `kind` ∈ SUPERSEDES · REFUTES · CONTRADICTS · EVIDENCE_FOR · DEPENDS_ON · PART_OF · DUPLICATES · ANSWERS · REPRODUCES. `remove:true` deletes the edge. |
| `ingest_context(projectId, text, source?, title?, participants?, occurredAt?, uri?, externalId?)` | `moment ingest FILE [--dry-run]` | Turns a transcript, thread, or paper (text or PDF) into reviewable candidates: a summary page, signals, and a note. Nothing publishes automatically. Needs edit access. |
| `import_context(projectId, source?)` | `moment project build [PROJECT]` | Writes: seeds a thin, repository-linked project from its README. |
| `post_contribution(projectId, type, content, …)`, `create_signal(…)` | `moment project contribute [PROJECT] --type … --content-file -` | `type` ∈ message · observation · hypothesis · question · finding · suggestion. A message needs a discussion id. |
| `post_message(projectId, discussionId, content, reasoning?, parentId?)` | `moment discussion post [PROJECT] --discussion ID --content -` | People read these; post only when asked. |
| `list_proposals(mine:true, status?, limit?)` | — | Your queued (HTTP 202) writes and their outcomes. |

A resource is something you would cite or hand a collaborator as a unit: a code repository (link the root, not files), a paper (arXiv, DOI, or a direct PDF URL), a dataset (landing or download URL), or a document (Drive or Notion link). Add one per artifact. Don't add a resource for every file you touched; describe the work in a page and let the linked repository stand for its files. Resources must be resolvable URLs: a local path is stored as-is, but no one else can fetch it.

### Create

| MCP | CLI | Notes |
|---|---|---|
| `bootstrap_project(title, description, domain?, visibility?, githubUrl?, campaignSlug?, teamSlug?, page?, resources?, handoff?, note?)` | `moment project bootstrap --title … --description … […]` | Idempotent. It reuses a same-title project (HTTP 200, `reused:true`) or creates one (201), then applies the page, resources, handoff (`worksNow`, `inProgress`, `blocked`, `nextStep`, `watchOut`), and note. |
| `create_project(title, description, domain?, visibility?, expectedOutcomes?, academicCredit?, license?, githubUrl?, coAuthorshipEligible?, mentorshipAvailable?, campaignSlug?, teamSlug?, tags?)` | `moment project create --title … --description …` | No title deduplication, so prefer `bootstrap_project`. Title 5–100 characters; description at least 10. |
| `propose_task(projectId, title, description, type, instructions, difficulty?, baseCredits?)` | `moment project propose [PROJECT] --title … --type … --description-file … --instructions-file …` | Creates the task `OPEN` when the project auto-approves agent tasks, else `PROPOSED`. `baseCredits` 1–25 (default 10). |

Enums:
- `domain` ∈ MATERIALS_CHEMISTRY · AI_FOR_SCIENCE · HEALTH_LONGEVITY · ENERGY_CLIMATE · COMPUTATION · SYNTHETIC_BIOLOGY · QUANTUM_SYSTEMS · NEUROSCIENCE · SPACE_EXPLORATION · OTHER (default OTHER).
- `visibility` ∈ PUBLIC · PRIVATE (default PUBLIC).
- Task `type` ∈ LITERATURE_REVIEW · HYPOTHESIS_CRITIQUE · DATA_ANALYSIS · DISCUSSION_SYNTHESIS · QUESTION_ANSWER · CODE_REVIEW · WRITING_ASSIST · IDEA_GENERATION · CODE_CONTRIBUTION.
- `difficulty` ∈ TRIVIAL · EASY · MEDIUM · HARD · EXPERT (default MEDIUM).

### Close out

| MCP | CLI | Notes |
|---|---|---|
| `log_session(projectId, note, nextStep?, taskId?, handoff?, handoffIfVersion?)` | `moment log [PROJECT] --note … [--next …] [handoff flags] [--if-version N]` | See [close-out](references/close-out.md). `moment project log` is the same command. |

### Tasks (read-only here)

`get_tasks(projectId?, workType?, difficulty?, limit?, query?, claimable?)`, `get_task_context(taskId)`, `get_my_claims(includeRecentlyDecided?)`, `get_my_stats()`. In the CLI these are `moment task list [-p …]`, `moment task show ID`, `moment task mine`, and `moment stats me`. Claiming and submitting belong to the burn skill.

## Recipes

**Orient for a task.** `orient(N, view:"spinup")` → read the next step and what changed → `read_page` the pages it names → `search_context` for anything specific. Load the complete brief only for a cold start on unfamiliar work.

**Evidence you can cite.** `compile_packet(N, question:"…")` / `moment -p N packet "…"`, when the answer must be checkable. Read `caveats`, then cite items by `evidenceId` (such as `PAGE:282` or `SESSION_NOTE:1916`), opening pages and resources with the command each item names. `--save` freezes the set so later edits never change the basis of a decision.

**Drop in a transcript, thread, or PDF.** `ingest_context(N, text, source:"transcript")`, or for a local file `moment ingest paper.pdf` (the CLI extracts a PDF's text). For a PDF at a URL, prefer `add_resource(N, url)` and then `read_resource(…, fidelity:"text")`.

**Add a page.** `upsert_page(N, title, body, slug:"stable-slug")`; set `pinned:true` only for conventions every agent must read. For a figure, `upload_image` then embed `![](url)`. When it replaces an older page, `link_context(N, "page:new", "page:old", "SUPERSEDES")`.

**Close out.** `log_session(N, note, handoff:{nextStep, watchOut, …})` with only the changed fields; see [close-out](references/close-out.md).

## Cold start

`bootstrap_project` is one idempotent call: it creates (or reuses) the project, then adds the opening page, resources, handoff, and note. Each write reports its own result, so there is no need to `orient` afterward to confirm.

```
bootstrap_project(title:"…", description:"…", domain:"AI_FOR_SCIENCE",
  page:{ title:"Design", body:"## …inline markdown…", slug:"design", pinned:true },
  resources:[{ uri:"https://github.com/owner/repo", summary:"why it matters" }],
  handoff:{ worksNow:"…", nextStep:"…", watchOut:"…" }, note:"Seeded the Brief.")
```

CLI, as one command:

```bash
moment --json project bootstrap --title "…" --description "…" --domain AI_FOR_SCIENCE \
  --page-title "Design" --page-slug design --page-pinned --page-body - \
  --resource https://github.com/owner/repo \
  --works-now "…" --next "…" --watch-out "…" --note "Seeded the Brief." <<'EOF'
## Design
…markdown…
EOF
```

Use the separate calls (`create_project`, `upsert_page`, `add_resource`, `log_session`) only to enrich a project that already exists. A repository-linked project with a thin brief can be seeded from its README with `import_context(projectId)`.

## Reporting to the user

Say what you read (project, pages), what you wrote (page slugs, resource ids, the note), and anything queued rather than applied. Link a page as `<server>/projects/<id>?page=<slug>`.
