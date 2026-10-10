# Moment CLI

Moment CLI connects research agents to projects in the lab. It lets a person sign in, attach a repository, render the current brief, and leave durable context behind.

```bash
uv tool install "git+https://github.com/blaiszik/moment-agent-kit#subdirectory=cli"
moment onboard -p <projectId>
```

No [uv](https://docs.astral.sh/uv/getting-started/installation/)? `pipx install "git+https://github.com/blaiszik/moment-agent-kit#subdirectory=cli"` works the same way, and `python3 -m pip install --user` with the same address works where neither is available. From a clone of the kit, `uv tool install ./cli` does the same. The CLI is not on PyPI yet (see RELEASING.md).

`moment onboard` signs you in through the browser the first time, binds the current repository to the project, seeds useful repository context, updates the agent instructions, and installs supported ambient-capture hooks.

Run `moment orient` at the start of a session to render the project's current brief and next steps.
