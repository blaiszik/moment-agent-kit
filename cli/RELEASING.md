# Releasing `moment-cli`

Ben owns the PyPI account and release token. The token stays in Ben's local credential store or CI secret storage and is never committed to this repository.

From `tools/moment-cli`:

```bash
python -m build
twine check dist/*
python -m twine upload --repository testpypi dist/*
pipx install --index-url https://test.pypi.org/simple/ moment-cli
python -m twine upload dist/*
```

Verify the TestPyPI installation before uploading the same artifacts to PyPI. Publishing is a manual Ben-owned step. Bump `version` in pyproject.toml for every upload (PyPI never accepts the same version twice). The upload token lives in `~/.pypirc` (mode 600) or is pasted at twine's prompt, never in a shell profile or this repository.

To rename the distribution, change exactly these two package-name constants together:

- `PACKAGE_NAME` in `tools/moment-cli/moment_cli/__init__.py`
- `MOMENT_CLI_PACKAGE` in `app/lib/agents/install-block.ts`

Then rebuild, run `twine check dist/*`, synchronize the agent kit, and verify its install instructions before publishing.

The first upload (2.6.0) landed on 2026-10-10 and every install line now reads `moment-cli`. If the package is ever pulled, reverse these steps (install lines fall back to `git+https://github.com/blaiszik/moment-agent-kit#subdirectory=cli`). What the first upload changed:

1. Set `MOMENT_CLI_ON_PYPI = true` in `app/lib/agents/install-block.ts` (the site, `/developers`, the project's connect card and the agents page all read it).
2. Change the install block at the top of `tools/moment-cli/README.md` back to `uv tool install moment-cli`.
3. Run `npm run kit:sync` and `scripts/publish-agent-kit.sh`.
