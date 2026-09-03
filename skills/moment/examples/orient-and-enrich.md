# Example — orient on a project, pull context, add a page, close out

A walkthrough of the `/moment` context loop on a single project (no task claim).

## 1. Orient
```
get_project_context(projectId: 15)
```
Read the handoff (works-now / next step / watch-out), the must-read pages, and the resource manifest.
The Brief comes back as prose above a `--- structured (JSON) ---` fence — read the prose first.

## 2. Pull what you need
```
list_resources(projectId: 15)
read_resource(projectId: 15, resourceId: 12)          # full text of the dataset doc
compile_context(projectId: 15, task: "retraction lint design")   # budgeted neighborhood bundle
```

## 3. Enrich the graph
```
add_resource(projectId: 15, uri: "https://github.com/owner/retraction-db",
             summary: "Authoritative retraction list used by the lint")
upsert_page(projectId: 15, title: "Retraction-lint design",
            slug: "retraction-lint-design", pinned: true,
            body: "## Approach\n...\n![plot](<url from upload_image>)")
link_context(projectId: 15, srcType: "PAGE", srcRef: "retraction-lint-design",
             dstType: "PAGE", dstRef: "old-lint-notes", kind: "SUPERSEDES")
```

## 4. Close out — keep the Brief fresh
```
log_session(projectId: 15,
  note: "Drafted the retraction-lint design page and linked the DB resource; next is wiring it into CI.",
  handoff: { nextStep: "Wire retraction lint into CI", watchOut: "DB API rate-limits at 60/min" })
```

## CLI equivalents (humans / scripts)
Global flags (`-p`, `--json`) go **before** the subcommand; `project …` takes the id positionally.
```bash
moment -p 15 orient
moment project resources 15
moment project read-resource 15 12
moment -p 15 gather "retraction lint design"
moment project add-resource 15 https://github.com/owner/retraction-db --summary "Authoritative retraction list"
moment project page 15 --title "Retraction-lint design" --body-file design.md
# Handoff fields live on the top-level `log` (global -p), not `project log`:
moment -p 15 log --note "Drafted the design page; next is CI wiring." --next "Wire retraction lint into CI" --watch-out "DB API rate-limits at 60/min"
```
