# Moment servers and configuration

Which server a client talks to, which key it uses, and how to fix the usual mismatches.

## Servers

| Server | URL | Use |
|---|---|---|
| The lab (production) | `https://scientificintelligence.org` | The default for the CLI and the MCP server. |
| Local development | `http://localhost:3000` | Only while developing `moment-next` itself. |

Hosted MCP (no local install):

```bash
claude mcp add --transport http moment-research https://scientificintelligence.org/api/mcp --header "Authorization: Bearer <your key>"
```

Other MCP clients take the same URL and header as JSON: `{ "url": "https://scientificintelligence.org/api/mcp", "headers": { "Authorization": "Bearer <your key>" } }`.

## How the CLI resolves its settings (first match wins)

- **Server:** `--api-url` → `apiUrl` in the nearest `.moment.json` (walking up from the current directory) → `MOMENT_API_URL` → `MOMENT_MCP_API_URL` → `MCP_MOMENT_API_URL` → `apiUrl` in `~/.moment.json` → the `moment-research` entry in `.mcp.json` → the lab.
- **Key:** `--api-key` → `apiKey` in the repository `.moment.json` (warned about, since that file is committed) → `MOMENT_API_KEY` → `MOMENT_MCP_API_KEY` → `MOMENT_MCP_KEY` → `apiKey` in `~/.moment.json` → `.mcp.json`.
- **Project:** `-p` → `MOMENT_PROJECT` / `MOMENT_PROJECT_ID` → the path and task-hint match in `.moment.json` → `defaultProject` → the only attached project.

The stdio MCP server resolves its server as `--api-url` → `--config` / `MOMENT_MCP_CONFIG` file → `MOMENT_MCP_API_URL` → `MCP_MOMENT_API_URL` → `MOMENT_API_URL` → the lab.

`moment doctor` checks the whole chain in one screen: binding, key, reachability, authentication, project access, and MCP drift.

## Where credentials may go

A repository's `.moment.json`, a `--config` file, or a walked-up `.mcp.json` may choose the server, but only the person can vouch for it. The CLI sends the agent key or the person's token only to:

- the lab (`https://scientificintelligence.org`) or a loopback host (`localhost`, `127.0.0.1`);
- a server named on the command line (`--api-url`, before the subcommand) or in `MOMENT_API_URL` / `MOMENT_MCP_API_URL`;
- a server listed in `"trustedApiUrls"` in `~/.moment.json` (`moment init --api-url X` and `moment onboard --api-url X` add X).

Otherwise the command stops with "Not sending your Moment credentials to …", exit code 2, and nothing is sent; `moment doctor` shows the same finding on its `server` line. This keeps a cloned repository, or a hook running inside one, from routing your key to a host you never chose. An agent that meets this refusal reports it; deciding to trust the server is the person's call.

Server text is also sanitized before it is printed or returned as JSON: terminal control sequences and invisible Unicode (tag characters, bidi overrides) are removed.

## Keys are per server

A key minted on one server does not work on another. "Invalid API key" usually means the client is pointed at the wrong server, not that the key is bad. Keep keys in `MOMENT_API_KEY` or `~/.moment.json`, never in a repository's `.moment.json`. `moment auth whoami` shows which identities are configured.

## The common trap

A repository's `.moment.json` outranks `MOMENT_API_URL`. A repository bound to `http://localhost:3000` (as `moment-next` is, for development) sends every command to the local server even when your shell points elsewhere. Pass `--api-url https://scientificintelligence.org` to reach the lab from inside such a repository.
