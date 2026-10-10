# Moment close-out contract

How to end a work session so the next agent can start without rediscovery: one session note, plus a patch to the project's handoff (the part of the brief that says what to do next).

## When to close out

Close out after substantive work on a project, or when asked. A read-only question or a trivial self-contained fix needs no shared-context write unless requested. Record only what changed. Logging a handoff does not authorize deployment, messages, or unrelated project changes.

## What to record

- **`note`** (required, up to 4000 characters): what changed, why, what you verified, what you tried that failed. Size it to the work: full detail for real friction or new findings (approaches ruled out, traps, PR links), one line for a trivial fix. Put longer material in a page and summarize it here.
- **`nextStep`**: the single next concrete action, specific enough for another agent to start cold.
- **`worksNow`**: state verified working as a result of this session.
- **`inProgress`**: work in flight.
- **`blocked`**: what is blocked, on what, and any decision needed.
- **`watchOut`**: the friction or trap you hit, with the file, command, or repro. Ask what cost you time that the next agent would otherwise repeat. Durable conventions belong in a pinned page, not only in a note that scrolls away.
- **`triedAndFailed`**, **`decisions`**, **`openQuestions`**: approaches ruled out, decisions made, questions still open.
- **`readFirst`**: what the next agent should read before touching anything, as items `{label, ref?, why?}` (label up to 200 characters, `ref` a path or URL up to 500, `why` up to 300; at most 20 items).

## How a handoff patch merges

Several people and agents may close out on one project within the hour, so a patch is not a blind overwrite.

| Field | On a patch |
|---|---|
| `decisions`, `openQuestions`, `triedAndFailed` | Append by line, dropping duplicates. Re-running the same close-out changes nothing. |
| `nextStep`, `worksNow`, `inProgress`, `blocked`, `watchOut`, `readFirst` | Replace. There is only one next step; `readFirst` replaces the whole list, so send every item. |
| Any field you omit | Unchanged. A note with no handoff fields leaves the handoff alone. |

Each handoff field holds up to 4000 characters.

Because the replaced fields are shared, patch them only when your work is the project's main thread. For side work on a busy project, describe your next step in the note and use the appending fields; `moment status` shows the current next step and handoff version before you write.

To guard the replaced fields, pass the handoff version you read (`moment status` shows it as `Brief: v165`; so do `orient --json` and the MCP `orient` payload) as `handoffIfVersion` / `--if-version`. If someone else changed the handoff in the meantime, the server returns 409 `version_conflict` with the expected version, the current version, and the current handoff, and saves nothing, not even the note. Resending is safe. Both clients retry once against the new version and stop with every version they saw if it moves again.

On a project where you lack edit access, the call returns HTTP 202: the note is queued for an editor. Report it as pending.

## MCP

```text
log_session(projectId, note, nextStep?, taskId?, handoff?, handoffIfVersion?)
```

- `handoff`: `{ nextStep?, worksNow?, inProgress?, blocked?, watchOut?, triedAndFailed?, decisions?, openQuestions?, readFirst?: [{ label, ref?, why? }] }`, with only the fields that changed.
- The top-level `nextStep` is the note's own next step (up to 2000 characters). To change what the brief shows as the next step, put it in `handoff.nextStep`.

## CLI

```bash
moment log --note "<what changed and what you verified>" --next "<the next concrete step>"
```

`--next` sets the handoff's next step (and the note's). Add flags only for the fields that changed:

```bash
moment log --note - --next "<next step>" \
  --works-now "<verified state>" --in-progress "<in flight>" --blocked "<blocker and what it waits on>" \
  --watch-out "<the trap>" --tried-and-failed "<approach and why it failed>" \
  --decisions "<decision>" --open-questions "<question>" \
  --read-first "docs/contract.md :: the rules this change follows" \
  --if-version <version you read> <<'EOF'
<a multi-line note, read from stdin>
EOF
```

- The project comes from `-p <id|alias>` (before or after `log`), a leading positional (`moment log platform --note …`), or the repository binding. Pass it explicitly when several projects are attached.
- `--decisions`, `--tried-and-failed`, `--open-questions`, and `--read-first` repeat. `--read-first` takes `LABEL` or `LABEL :: why`.
- The CLI appends a short working-tree fingerprint to the note (`--no-tree` to skip it) and, with `--commits A..B`, a commit summary; it drops either rather than exceed the limit.
- `moment project log` is the same command.

## Writing quality

Keep the brief scannable:

1. Depth belongs in a page (`upsert_page`, pinned when it is a convention every agent must follow).
2. The note and handoff stand alone: sized to the work, they tell the next agent what changed, what to do next, and what to watch without hunting through pages.
3. `watchOut` is the friction you hit, not the architecture you landed. Record what failed and why.

Thin close-out, for a trivial fix:

```text
note: "Fixed the off-by-one in the date parser; tests pass."
```

Rich close-out, for substantive work with friction:

```text
note: "Added retraction lint to the DOI pipeline. Tried the Crossref /works filter first; it silently drops withdrawn DOIs. Switched to the RetractionWatch dump, added it as a resource, and joined on DOI. Files: lib/lint/retraction.ts. PR #214."
handoff: {
  worksNow: "Retraction lint passes against local fixtures.",
  watchOut: "RetractionWatch DOIs are lowercased and ours are not; normalize before any join.",
  triedAndFailed: "Crossref /works filter: silently drops withdrawn DOIs.",
  nextStep: "Backfill existing DOIs through the new lint (PR #214).",
  readFirst: [{ label: "lib/lint/retraction.ts", why: "the lint entry point" }]
}
```
