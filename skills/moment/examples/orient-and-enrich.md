# Example: orient, pull context, add a page, close out

One pass of the context loop on project 15, with no task claim.

## 1. Orient

```
orient(projectId: 15, view: "spinup")
```

The spinup view returns the next step, the summary, and what changed since you last looked. Read it, then read only the pages it names:

```
read_page(projectId: 15, slug: "retraction-lint-design")
```

## 2. Pull what you need

```
list_resources(projectId: 15)
read_resource(projectId: 15, resourceId: 12, fidelity: "text")      # full text; the default is a summary
compile_packet(projectId: 15, question: "retraction lint design")    # budgeted evidence you can cite
```

## 3. Enrich the graph

```
add_resource(projectId: 15, uri: "https://github.com/owner/retraction-db",
             summary: "Authoritative retraction list used by the lint")
upsert_page(projectId: 15, title: "Retraction-lint design",
            slug: "retraction-lint-design",
            body: "## Approach\n...\n![plot](<url from upload_image>)")
link_context(projectId: 15, from: "page:retraction-lint-design",
             to: "page:old-lint-notes", kind: "SUPERSEDES")
```

## 4. Close out

```
log_session(projectId: 15,
  note: "Drafted the retraction-lint design page and linked the DB resource; next is wiring it into CI.",
  handoff: { nextStep: "Wire retraction lint into CI", watchOut: "The DB API rate-limits at 60 requests/min" })
```

Contract and merge rules: [../references/close-out.md](../references/close-out.md).

## The same pass with the CLI

Global options such as `-p` and `--json` work before or after the subcommand. Piped output is plain text, so the brief and pages read cleanly.

```bash
moment -p 15 orient
moment -p 15 page-read retraction-lint-design
moment -p 15 project resources
moment -p 15 project read-resource 12 --fidelity text
moment -p 15 packet "retraction lint design"
moment -p 15 project add-resource https://github.com/owner/retraction-db --summary "Authoritative retraction list"
moment -p 15 page --title "Retraction-lint design" --slug retraction-lint-design --body-file design.md
moment -p 15 log --note "Drafted the design page; next is CI wiring." \
  --next "Wire retraction lint into CI" --watch-out "The DB API rate-limits at 60 requests/min"
```
