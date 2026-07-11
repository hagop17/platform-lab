# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Toolchain & commands

- **Package manager:** `uv` — do not use `pip` or `poetry` directly for dependency management.
- **Run dev server:** `uv run fastapi dev main.py`
- **Run full stack (app + Prometheus + Grafana):** `docker compose up -d --build`
- **Lint:** `uv run ruff check .`
- **Format:** `uv run ruff format .`
- **Typecheck:** `uv run pyright` (runs on the full project, no filenames passed)
- **Test:** `uv run pytest` (no tests exist yet, but `pytest` is a dev dependency — add new tests under `tests/`)
- **Install optional LLM provider extra:** `uv sync --extra anthropic` (see Architecture below)
- **Pre-commit hooks:** ruff (`--fix`), ruff-format, pyright, check-yaml, trailing-whitespace, end-of-file-fixer — all must pass before committing.

## Architecture

Flat layout, no `src/`/`app/` package — two top-level modules:

- **`main.py`** — FastAPI app entrypoint. Wires up OpenTelemetry (traces to console via `ConsoleSpanExporter`, metrics to Prometheus pull-model via `PrometheusMetricReader` on port `9464`), then defines routes (`/`, `/health`, `/work`, `/items/{item_id}`, `/api/v1/analyze`).
- **`metrics_analysis.py`** — implements `/api/v1/analyze`: queries Prometheus's `query_range` API for a metric window, formats the time series into compact text, then sends that text to an LLM for anomaly/trend analysis.

### Pluggable LLM provider

`analyze_with_llm` in `metrics_analysis.py` dispatches to a provider adapter based on the `LLM_PROVIDER` env var (defaults to `"groq"`). Providers are registered in the `_PROVIDERS` dict, each mapping to a `_analyze_with_<provider>(prompt: str) -> str` function.

- Adapters import their SDK **lazily inside the function**, not at module top level — this means you only need the SDK installed for the provider you actually use.
- `anthropic` is an **optional dependency** (`[project.optional-dependencies]` in `pyproject.toml`), not part of the default `uv sync`. If `LLM_PROVIDER=anthropic` is set without the extra installed, `_import_provider_sdk` raises an `ImportError` with the exact fix (`uv sync --extra anthropic`) rather than a bare traceback.
- To add a new provider: write `_analyze_with_<name>`, register it in `_PROVIDERS`, and (if it needs a new SDK) add it as an optional extra rather than a hard dependency.

### Metrics flow (important distinction)

Prometheus scrapes `app:9464` continuously in the background (every 5s per `prometheus.yml`), independent of the `/api/v1/analyze` route. Hitting that route does **not** trigger a scrape — it only reads whatever Prometheus has already stored for the requested window. If the stack was just started with no traffic yet, generate some first (e.g. hit `/work` a few times) before expecting a meaningful analysis.

## Environment variables

Set in `.env` (gitignored) — loaded by `docker-compose.yml` into the `app` service:

- `GROQ_API_KEY` — required for the default `groq` provider.
- `ANTHROPIC_API_KEY` — required only if `LLM_PROVIDER=anthropic`.
- `LLM_PROVIDER` — `groq` (default) or `anthropic`.

## Gotchas

- **`Dockerfile` dependency list is manually maintained and can drift from `pyproject.toml`** — it currently `pip install`s an explicit package list rather than using `uv sync`, so newly added/removed dependencies (e.g. `groq`) must be updated in both places.
- Ruff `target-version = "py311"` even though `requires-python = ">=3.12"`.
- `.gitignore` references `data/`, `*.db`, `.terraform/`, `*.tfstate*` — likely relevant for future data store / infra work, not currently used.
- `otel-demo-spec.md` and `otel-demo-llm-analysis-spec.md` are planning specs (the latter originally assumed Podman/`podman-compose`; this repo now uses Docker/`docker compose` — the underlying compose file and commands are equivalent).
