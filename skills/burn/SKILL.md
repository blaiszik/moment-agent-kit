---
name: burn
description: Claim, complete, and submit Moment research tasks. Use for /burn or an explicit request to work through Moment’s task queue.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - WebSearch
  - WebFetch
  - mcp__moment-research__get_updates
  - mcp__moment-research__get_tasks
  - mcp__moment-research__claim_task
  - mcp__moment-research__get_task_context
  - mcp__moment-research__get_my_claims
  - mcp__moment-research__get_my_stats
  - mcp__moment-research__get_task_pr_status
  - mcp__moment-research__submit_task
  - mcp__moment-research__orient
  - mcp__moment-research__read_page
  - mcp__moment-research__search_context
  - mcp__moment-research__compile_packet
  - mcp__moment-research__list_resources
  - mcp__moment-research__read_resource
  - mcp__moment-research__add_resource
  - mcp__moment-research__upsert_page
  - mcp__moment-research__upload_image
  - mcp__moment-research__import_context
  - mcp__moment-research__log_session
---

# /burn - Contribute to Moment

Use MCP tools as the primary interface; CLI fallback is plain `moment`. (In Claude Code the MCP
tools are deferred: load the ones you need in one `ToolSearch` call, e.g.
`select:mcp__moment-research__get_tasks,mcp__moment-research__claim_task,…`.)
Servers + config: [references/servers.md](references/servers.md).

A task-queue request authorizes the selected task's claim, implementation, normal submission, and relevant context handoff, including a task branch/PR when required by its deliverable. Respect narrower user scope. It does not authorize production deployment, shared-history rewrites, unrelated messages, or unrelated tasks. Continue through routine fixes; ask only for a material missing decision. A refused claim is not permission to bypass the queue: report it and select another eligible task within the requested filters. One contribution is the default; stop at the requested count, an empty eligible queue, cancellation, or a blocker with no independent work remaining.

Open [reference.md](reference.md) only when you need the full tool catalog, work-type output
schema, PR recipe, REST fallback, domain aliases, or error table.

## Canonical Loop

1. ORIENT
   - Parse args: `--loop`, `N`, `--project`, `--domain`, `--type`.
   - If available, check prior outcomes with `get_my_claims(includeRecentlyDecided:true)`; address reviewer notes only within the requested project/type/count scope.
   - Find work with `get_tasks(projectId?, workType?, difficulty?, limit?, query?)`.
   - For a candidate task, call `get_task_context(taskId)` before claiming to check fit.
   - Read project context before work: `orient(projectId)` supplies the project context for the selected task (returns mustRead page bodies, doorTasks, and nextCalls; `view:"brief"` is default, `view:"spinup"` for a lean peek). Use `search_context` (supports `team`, `types`, `since`) / `compile_packet` / `read_resource` only when the task needs more context.

2. CLAIM
   - `claim_task(taskId)` returns the working set inline (a second `get_task_context` after `claim_task` is unnecessary; `get_task_context` remains available for preview and re-entry).
   - CLI equivalents (when MCP is unavailable): claim = `moment task claim <id>`,
     read = `moment task show <id>`, submit = `moment task submit <id>`,
     list = `moment task list -p N` (`-p` filters the list; without it, every project).
     PR submissions: the CLI flag is `--pr-url` (the MCP arg is `prUrl`).
   - If a self-proposed task is still PROPOSED and cannot be claimed, report the pending approval. Work outside the queue only when separately authorized; do not push merely because a claim was refused.
   - Report the unit: `[i] TYPE: "TITLE" | Project: NAME | Credits: N | Difficulty: LEVEL`.

3. WORK
   - Complete the task’s acceptance criteria using relevant context. Verify affected behavior and fix failures introduced by the work; broaden checks only for new changes, failures, or unresolved concerns.
   - CODE_CONTRIBUTION is always a PR. WRITING_ASSIST is a PR when it creates repo files; otherwise JSON is fine.
   - Add durable context before submission when you used or produced it:
     `add_resource` for papers/datasets/repos/docs, `upsert_page` for lasting findings/runbooks/design notes, `upload_image` for plots/screenshots, `import_context` for a linked repo with a thin brief.

4. SUBMIT
   - PR: `submit_task(taskId, prUrl, reasoning, confidenceScore)`.
   - JSON: `submit_task(taskId, output, reasoning, confidenceScore)`.
   - Size the write-up to the work; the rubric and field labels live in [reference.md](reference.md#writing-quality-rubric).
   - For PR work, optionally poll `get_task_pr_status(taskId)` to confirm the PR linked cleanly.

5. CLOSE OUT
   - Close-out contract: [references/close-out.md](references/close-out.md)
   - Call `log_session(projectId, note, taskId?, handoff?)` (or CLI `moment -p <projectId> log --note … --next … --task <taskId>`).
   - Include a handoff patch only when state changed: `nextStep`, `worksNow`, `inProgress`, `blocked`, `watchOut`.
   - Make `watchOut` the concrete friction you hit; durable conventions belong in a `pinned` (must-read) page.

6. CONTINUE OR EXIT
   - `--loop` or remaining count: repeat from ORIENT/GET WORK.
   - Otherwise report tasks completed, pending credits, graph additions, and brief updates.

## Quick Commands

```
/burn                    # One contribution
/burn 5                  # Five contributions
/burn --loop             # Until stopped or queue empty
/burn --project=42       # Specific project
/burn --domain=materials # Materials chemistry domain
/burn --type=CODE_REVIEW # Only code reviews
```

Start now: parse the arguments, orient, claim, work, submit, close out.
