# Contributing to price-tracker-bot

Thanks for your interest in contributing! All kinds of contributions are welcome: bug reports, feature suggestions, scraper plugins, translations, dashboard panels, code review feedback.

## Code of Conduct

This project follows the [Contributor Covenant v2.1](CODE_OF_CONDUCT.md). By participating you agree to its terms.

## Reporting bugs

Open a GitHub Issue using the **Bug report** template. Include:
- Reproduction steps (minimal, deterministic if possible)
- Expected vs actual behavior
- Environment: Docker or venv, OS, Python version, bot version (`git describe --tags`)
- Logs (redact `TELEGRAM_BOT_TOKEN`)
- Workaround if any

## Suggesting features

Open a GitHub Issue using the **Feature request** template. Describe the problem first, then the proposed solution and alternatives considered.

## Development setup

```bash
git clone https://github.com/bernalli/price-tracker-bot.git
cd price-tracker-bot

# Recommended: uv venv
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Install pre-commit hooks (runs ruff, mypy, gitleaks, bandit on every commit)
pre-commit install
```

## Running tests

```bash
pytest                                       # all tests
pytest -m "not e2e"                          # skip end-to-end tests
pytest --cov=src/price_tracker               # with coverage report
pytest tests/unit/scrapers/test_amazon.py -v # single file verbose
```

Coverage: current enforced gate is ≥75% (`pyproject.toml`). Plan 3 closure targets: global ≥90%, core ≥93%, per-scraper ≥80%.

## Linting & type-checking

```bash
ruff check .
ruff format --check .       # check only
ruff format .               # apply
mypy                                          # respects pyproject.toml [tool.mypy] config
```

## i18n workflow

When you add or change user-facing strings, update the catalog:

```bash
./scripts/i18n.sh extract    # source strings -> messages.pot
./scripts/i18n.sh update     # merge into every locale catalog

# Translate src/price_tracker/locale/<lang>/LC_MESSAGES/messages.po manually

./scripts/i18n.sh compile    # .po -> .mo (both are committed)
```

Use the helper rather than calling `pybabel` directly: extraction needs
`-k N_ -k "ngettext:1,2"` to pick up deferred copy and plural forms, and
compilation goes through `scripts/i18n_compile.py` because `pybabel compile`
misreads a literal percent in prose as a printf template.

Two rules when writing strings:

- Source strings are English, wrapped in `_()`. Interpolate with `str.format`
  (`_("Price: {p}").format(p=price)`), never an f-string — the extractor cannot
  see a call nested inside one.
- Copy declared far from where it is rendered (module-level tables) is marked
  with `N_()` and translated with `_()` at the render site, so it picks up the
  reader's locale rather than the import-time one.

See [docs/i18n.md](docs/i18n.md) for full reference.

## Adding a scraper plugin

See [docs/plugins.md](docs/plugins.md) for the contract and a minimal example. Quick reference:
1. Create `plugins/<name>.py` (gitignored).
2. Subclass `AbstractScraper`, set `domain_patterns` (compiled regex patterns matching the URL host) and optional `priority`.
3. Implement `async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo`.
4. Add an HTML fixture under `tests/fixtures/<name>/` (e.g. `sample_product.html`).
5. Add a test in `tests/unit/scrapers/test_<name>.py`.

## Pull request rules

- Fork the repo, branch from `main`: `feat/<topic>` or `fix/<issue-ref>`.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:`.
- Tests required for new code; coverage on touched files ≥80%.
- `ruff check` + `ruff format --check` + `mypy` (configured strict in `pyproject.toml`) must pass.
- All new tests must pass; baseline must not regress.
- Add a `[Unreleased]` entry to `CHANGELOG.md` for user-visible changes.
- Fill the PR template checklist.

## Commit signing

Optional but encouraged. See [GitHub docs](https://docs.github.com/en/authentication/managing-commit-signature-verification).

## Where to ask questions

GitHub Discussions (enabled after Plan 4 first push) or open an Issue with the `question` label.
