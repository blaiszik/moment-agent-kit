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

Verify the TestPyPI installation before uploading the same artifacts to PyPI. Publishing is a manual Ben-owned step.

To rename the distribution, change exactly these two package-name constants together:

- `PACKAGE_NAME` in `tools/moment-cli/moment_cli/__init__.py`
- `MOMENT_CLI_PACKAGE` in `app/lib/agents/install-block.ts`

Then rebuild, run `twine check dist/*`, synchronize the agent kit, and verify its install instructions before publishing.

Until the first upload, every install line installs from the public kit (`git+https://github.com/blaiszik/moment-agent-kit#subdirectory=cli`). After the upload succeeds:

1. Set `MOMENT_CLI_ON_PYPI = true` in `app/lib/agents/install-block.ts` (the site, `/developers`, the project's connect card and the agents page all read it).
2. Change the install block at the top of `tools/moment-cli/README.md` back to `uv tool install moment-cli`.
3. Run `npm run kit:sync` and `scripts/publish-agent-kit.sh`.
