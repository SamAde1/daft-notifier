# Agent notes

## Commits

Never add this trailer. Strip it if tooling injects it:

`Co-authored-by: Cursor <cursoragent@cursor.com>`

Enforcement is a git `commit-msg` / `prepare-commit-msg` hook in `.githooks/`. Copy both scripts (and `strip-cursor-coauthor.sh`) into `.git/hooks/` on every clone. Do not set `core.hooksPath` unless the human asks. After any commit, if that trailer is present, amend it out before push.

## Frozen runtime contracts

Do not rename or relocate these without an explicit breaking-change decision:

- Import and CLI: `daft_monitor` / `python -m daft_monitor`
- Environment prefix: `DAFT_MONITOR_*`
- Docker `CMD` (`python -m daft_monitor`) and health port `8080`
- Live config filenames and mount points: `config.yaml` / `config.sales.yaml` at the working directory
- Image name: `daft-notifier`
- Compose files at the repo root (`docker-compose.yml`, `docker-compose.sales.yml`, and the two `*.dev.yml` files)
- Persisted lifecycle identifiers: SQLite meta `lifecycle_v2_started_at`, `Storage.ensure_lifecycle_v2_started`, hop name `"lifecycle_v2"`

The GitHub repo and image are `daft-notifier`. The Python package import is `daft_monitor`. That mismatch is intentional.

## Layout

- Flat application layout: `daft_monitor/` at the repo root. No `src/`.
- User-facing examples stay at root: `config.example.yaml`, `config.sales.example.yaml`.
- Unraid hosting guide: [docs/unraid.md](docs/unraid.md) (formerly `README.unraid.md`).
- Install from `pyproject.toml`: `pip install -e .` and `pip install -e ".[scripts]"` for analytics extras.
- Tests live in `tests/`. The ntfy sender is `scripts/send_test_notification.py`, not a unit test.

## Style

Ruff is the linter/formatter. Do not add mypy or pre-commit unless asked.
