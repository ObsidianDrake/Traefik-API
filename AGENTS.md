# Repository Guidelines

## Project Structure & Module Organization

`route_api/` implements the Python 3.9+ FastAPI service: `main.py` defines endpoints, `models.py` validates routes and renders Traefik configuration, `settings.py` reads environment variables, `probe.py` checks upstreams, and `store.py` handles persistence and conflicts. `run_api.py` is the CLI entry point. Tests live in `tests/test_api.py`; `scripts/smoke_traefik.py` provides live integration checks. Deployment uses the standalone binary directly on the host. Generated artifacts belong in `build/` and `dist/`; local route data defaults to `data/config/`.

## Build, Test, and Development Commands

- `python3 -m venv venv` — create the local environment.
- `venv/bin/python -m pip install -r requirements-dev.txt` — install runtime and test dependencies.
- `venv/bin/python run_api.py --check-config` — validate loaded environment settings.
- `venv/bin/python run_api.py --host 127.0.0.1 --port 8000` — run locally with one worker.
- `venv/bin/python -m pytest -q` — run the automated suite.
- `venv/bin/python -m pip check` — check dependency compatibility.
- `make` — build `dist/traefik-route-api` using PyInstaller; `make clean` removes build outputs.
- `venv/bin/python scripts/smoke_traefik.py --traefik /path/to/traefik` — exercise Traefik v3 reload behavior on a Linux host permitting sockets.

## Coding Style & Naming Conventions

Use four-space indentation, snake_case functions and variables, PascalCase classes, and uppercase constants. Preserve public API field names such as `pathPrefix` and `probePath`. Keep Python 3.9 compatibility and match surrounding code. No formatter or linter is configured. Keep dependency pins in the appropriate `requirements*.txt` file.

## Testing Guidelines

Use pytest with AnyIO's asyncio backend, HTTPX `ASGITransport` and `MockTransport`, and `tmp_path` for isolated storage. Name tests `test_<behavior>` in `tests/test_*.py`. Add regression coverage for changed validation, takeover, authentication, or persistence behavior. The suite requires no network sockets; no numerical coverage threshold is configured.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout, so existing commit conventions cannot be verified. Use concise, imperative subjects, such as `Fix route conflict detection`. PRs should describe behavior changes, link relevant issues, report validation results, and update README examples when API or configuration behavior changes.

## Security & Configuration

Copy `.env.example` to `.env`, configure a random `API_TOKEN` of at least 24 characters and explicit upstream allowlists, then start the application. It automatically reads `.env` from the current working directory; the CLI also accepts `--env-file PATH`. Existing environment variables take precedence; shell commands and variable expansion are not evaluated. Never commit tokens or generated route data. Preserve single-worker operation, atomic writes, and protection of manually managed Traefik files.
