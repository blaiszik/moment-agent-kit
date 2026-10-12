# Releasing `moment-cli`

Releases are automatic. Bump `version` in `pyproject.toml` **and** the fallback `__version__` in `moment_cli/__init__.py` on your branch. When the bump reaches `main` (the production deploy, `scripts/ship.sh`), `.github/workflows/publish-cli.yml`:

1. skips if PyPI already has that version (so a push that touches the CLI without a bump does nothing), and fails if the two version strings disagree;
2. runs the CLI tests, builds, runs `twine check --strict`, and installs the built wheel in a clean venv to check `moment --help` and the reported version;
3. uploads with the repository secret `PYPI_TOKEN` (scoped to the moment-cli project only);
4. tags the commit `moment-cli-v<version>`.

Releasing with the deploy is deliberate: a CLI release must never call routes production doesn't serve yet. To retry after a PyPI outage, run the workflow by hand (Actions → publish-cli → Run workflow); it still skips a published version. PyPI never accepts the same version twice, so a bad release is fixed with a new version, never a re-upload.

By hand, as a fallback only (from `tools/moment-cli`): `python -m build && twine check dist/* && python -m twine upload dist/*`. A personal token lives in `~/.pypirc` (mode 600) or is pasted at twine's prompt, never in a shell profile or this repository.

To rename the distribution, change exactly these two package-name constants together:

- `PACKAGE_NAME` in `tools/moment-cli/moment_cli/__init__.py`
- `MOMENT_CLI_PACKAGE` in `app/lib/agents/install-block.ts`

Then rebuild, run `twine check dist/*`, synchronize the agent kit, and verify its install instructions before publishing.

The first upload (2.6.0) landed on 2026-10-10 and every install line now reads `moment-cli`. If the package is ever pulled, reverse these steps (install lines fall back to `git+https://github.com/blaiszik/moment-agent-kit#subdirectory=cli`). What the first upload changed:

1. Set `MOMENT_CLI_ON_PYPI = true` in `app/lib/agents/install-block.ts` (the site, `/developers`, the project's connect card and the agents page all read it).
2. Change the install block at the top of `tools/moment-cli/README.md` back to `uv tool install moment-cli`.
3. Run `npm run kit:sync` and `scripts/publish-agent-kit.sh`.
