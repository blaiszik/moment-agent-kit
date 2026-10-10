---
name: moment
description: Read and write Moment project context. Orient on a project's brief and handoff, read its pages and resources, search the lab's context graph, attach a repository (.moment.json), add pages or resources, and close out a session with `moment log` or log_session. Use when the user mentions Moment, a Moment project, brief, handoff, or close-out, a `.moment.json` file, the `moment` CLI, or the moment-research MCP tools. Claiming and submitting research tasks belongs to the burn skill.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - WebSearch
  - WebFetch
  - mcp__moment-research__list_projects
  - mcp__moment-research__get_updates
  - mcp__moment-research__orient
  - mcp__moment-research__read_page
  - mcp__moment-research__search_context
  - mcp__moment-research__compile_packet
  - mcp__moment-research__save_packet
  - mcp__moment-research__list_packets
  - mcp__moment-research__list_resources
  - mcp__moment-research__read_resource
  - mcp__moment-research__get_backlinks
  - mcp__moment-research__get_record
  - mcp__moment-research__list_open_questions
  - mcp__moment-research__get_claim_state
  - mcp__moment-research__find_evidence
  - mcp__moment-research__get_team_context
  - mcp__moment-research__get_user_context
  - mcp__moment-research__get_campaign_context
  - mcp__moment-research__get_submission_status
  - mcp__moment-research__list_proposals
  - mcp__moment-research__add_resource
  - mcp__moment-research__upsert_page
  - mcp__moment-research__upload_image
  - mcp__moment-research__link_context
  - mcp__moment-research__import_context
  - mcp__moment-research__ingest_context
  - mcp__moment-research__log_session
  - mcp__moment-research__post_contribution
  - mcp__moment-research__create_signal
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

# Moment project context

Moment holds a research project's shared context: the handoff (next step, what works, what is blocked, what to watch out for), pages, resources, and session notes. Agents read it before substantive work and write back what changed.

## Pick the workflow

- **Answer a question or read a known page.** Resolve the project, then read the page (`read_page` / `moment page-read <slug>`) or search (`search_context` / `moment find "<question>"`). Skip the full brief, and skip the close-out: a read-only request ends with its answer.
- **Orient before substantive work.** Start short with `moment orient` (piped, it prints the short brief) or `orient(projectId, view:"spinup")`: about 4 KB with the next step, summary, and what changed. Then read the pages it names. Ask for everything (`moment orient --complete` / `view:"brief"`) only when you need the whole picture: on busy projects it runs past 40 KB, and some harnesses show only the start of a result that large.
- **Attach a repository.** Follow [attach a repository](reference.md#attach-a-repository). This changes local routing only.
- **Create a project.** Use `bootstrap_project`, which reuses a same-title project instead of duplicating it; see [cold start](reference.md#cold-start). Reading context does not authorize creating a project.
- **Enrich.** Add or update a page (`upsert_page` / `moment page`) or attach an artifact (`add_resource` / `moment project add-resource`).
- **Close out** after substantive project work; see below.
- **Claim and submit tasks** with the burn skill, only when the user asked for task-queue work.

## Resolve the project and the transport

- **Project.** Use an explicit target from the user first. In an attached repository, `moment project list --for "<task>"` lists the projects in `.moment.json` and explains which one is selected. An id or an alias works wherever a project is expected. Ask only when the candidates remain materially ambiguous.
- **Transport.** Use the `moment-research` MCP tools when they are connected; in Claude Code they are deferred, so load the ones you need in one ToolSearch call. Otherwise use the `moment` CLI (if it is missing: `uv tool install "git+https://github.com/blaiszik/moment-agent-kit#subdirectory=cli"`, or the same address with `pipx install`; it is not on PyPI). Fall back to REST (`/api/v1/agents/…`, catalog at `GET /api/v1/agents/openapi`) only when neither works. If an MCP call fails on transport, switch to the CLI.
- **Server or key trouble.** Run `moment doctor`, and read [servers](references/servers.md). Never print credentials. The CLI sends credentials only to the lab, localhost, or a server the person chose; if it refuses a server that a repository's config picked, tell the user. Trusting that server (`--api-url`, `trustedApiUrls`) is their decision, not yours.

## Using the CLI

- `moment commands` maps the CLI in one screen, generated from the parser; `moment commands <command>` (or a group such as `project`) adds the options, and `moment <command> --help` shows one command. Check there rather than relying on memory.
- `-p <id|alias>`, `--json`, and `--quiet` work before or after the subcommand; `--api-url`, `--api-key`, and `--config` go before it. `--quiet` drops tips, footers, and excerpts.
- Piped output is plain text and markdown, and read results name the command that opens them (`→ moment page-read <slug>`). Add `--json` when you will parse fields. Results go to stdout and errors to stderr; under `--json`, every error is one JSON object with `error`, `hint`, and, for server refusals, `status` and `code`. Exit codes: 0 success, 1 refused or failed, 2 wrong usage.
- For long text, pass `-` and a heredoc (`--note -`, `--body -`, or any `…-file -`) instead of quoting it inline.
- `moment watch` and `moment ingest --watch` run until interrupted, and `moment auth login` is a person's browser step. Leave them to people.

## Event entry

- **Read** where a team's entry stands with `get_submission_status(campaignSlug, projectId)` / `moment campaign submission status`: it prints what the entry still needs in the editor's own sentences, whether saved changes are waiting to go live, and the window. `get_campaign_context(campaign)` gives the event's window, what an entry needs, and the judging criteria.
- **Filing, withdrawing, and team changes are the person's own act**, through the CLI signed in as them (`moment auth login`): `moment campaign mine`, `moment campaign status`, `moment campaign submission edit|check|submit|update|withdraw`, and `moment team list|create|join|invite-link|rename|leave`. Help them write the entry; do not file it for them. An event entry's title is its team's name, so the team lead renames it with `moment team rename`.

## Authorization

Reading is always in scope. Writing (pages, resources, notes, handoffs, projects, tasks) needs a request for that kind of change; permission to read is not permission to write. Posting a discussion message, installing hooks, and claiming tasks each need an explicit request. On a project where you lack edit access, a write returns HTTP 202 and is queued for an editor: report it as pending, not applied (`list_proposals` shows its state). A successful write result is enough; read it back only when it looks incomplete or contradictory.

## Close out

After substantive work on a project, record what changed in one call:

- MCP: `log_session(projectId, note, handoff?)`, where `handoff` carries only the changed fields among `nextStep`, `worksNow`, `inProgress`, `blocked`, `watchOut`, `triedAndFailed`, `decisions`, `openQuestions`, and `readFirst`.
- CLI: `moment log --note "…"`, plus `--decisions`, `--tried-and-failed`, or `--open-questions` (these append), and, when your work is the project's main thread, `--next`, `--works-now`, `--in-progress`, `--blocked`, `--watch-out`, or `--read-first` (these replace).

Fields you omit stay as they are. The project has only one next step: on a shared project, side work puts its next step in the note and uses only the appending fields, so it doesn't overwrite someone else's. `moment status` shows the current next step and handoff version first. The note holds at most 4000 characters, so put depth in a page and summarize it. Make `watchOut` the friction you hit, not a summary of what you built. The contract, merge rules, and examples are in [close-out](references/close-out.md).

Open [reference.md](reference.md) for tool signatures, enums, recipes, and the REST fallback.
