# Moment CLI

Moment CLI connects research agents to projects in the lab. It lets a person sign in, attach a repository, render the current brief, and leave durable context behind.

```bash
uv tool install moment-cli
moment onboard -p <projectId>
```

No [uv](https://docs.astral.sh/uv/getting-started/installation/)? `pipx install moment-cli` works the same way, and `python3 -m pip install --user moment-cli` works where neither is available. Upgrade with `uv tool upgrade moment-cli`.

`moment onboard` signs you in through the browser the first time, binds the current repository to the project, seeds useful repository context, updates the agent instructions, and installs supported ambient-capture hooks.

Run `moment orient` at the start of a session to render the project's current brief and next steps.

## Releasing

Bump `version` in `pyproject.toml` and the fallback `__version__` in `moment_cli/__init__.py`. When the bump reaches `main` of the Moment repository, a GitHub Action tests, builds and publishes it to PyPI. See [RELEASING.md](RELEASING.md).
