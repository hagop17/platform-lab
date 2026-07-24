# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Dev Container Constraints

This repo runs in a sandboxed dev container for unattended work (`--dangerously-skip-permissions`). Full details in `docs/devcontainer-spec.md` — read it before assuming a network failure is a code bug rather than a firewall restriction.

Key constraints to know:
- **Network is default-deny.** Only npm, PyPI, Anthropic's API, GitHub (read-only), and a few VS Code/telemetry domains are reachable. Anything else fails at the network layer, not the application layer.
- **GitHub access is read-only by design.** No push credentials exist in this container. `git push` will fail on authentication, not connectivity — that's expected, not a bug to work around. Pushing happens from the host terminal.
- **`.claude` config is a container-scoped volume**, separate from the host's real Claude Code credential — this is intentional isolation, not a misconfiguration.

If a task requires reaching a domain not in the allowlist, or requires pushing to GitHub, surface that as a blocker rather than trying to work around it.

## Toolchain & commands

- **Package manager:** `uv` — do not use `pip` or `poetry` directly for dependency management.
- **Run dev server:** `uv run fastapi dev main.py`
- **Run full stack (app + Prometheus + Grafana):** `docker compose up -d --build`
- **Lint:** `uv run ruff check .`
- **Format:** `uv run ruff format .`
- **Typecheck:** `uv run pyright` (runs on the full project, no filenames passed)
- **Test:** `uv run pytest` — unit tests live under `tests/`, no `__init__.py` (see `pythonpath = ["."]` in `pyproject.toml`'s `[tool.pytest.ini_options]`, which is what lets tests `import metrics_analysis` / `import rag...` directly).
- **Install optional LLM provider extra (local dev):** `uv sync --extra anthropic` (see Architecture below)
- **Build the Docker image with Anthropic support:** `WITH_ANTHROPIC=true docker compose up -d --build` — the `anthropic` extra isn't installed in the image by default (see `Dockerfile`'s `WITH_ANTHROPIC` build arg).
- **Pre-commit hooks:** ruff (`--fix`), ruff-format, pyright, pytest (offline, `HF_HUB_OFFLINE=1`), check-yaml, trailing-whitespace, end-of-file-fixer — all must pass before committing.

## Architecture

Flat layout, no `src/`/`app/` package — top-level modules plus a `rag/` package:

- **`main.py`** — FastAPI app entrypoint. Wires up OpenTelemetry (traces to console via `ConsoleSpanExporter`, metrics to Prometheus pull-model via `PrometheusMetricReader` on port `9464`), mounts `rag_router`, then defines routes (`/` route index, `/health`, `/work`, `/api/v1/analyze`).
- **`metrics_analysis.py`** — implements `/api/v1/analyze`: queries Prometheus's `query_range` API for a metric window, formats the time series into compact text, then calls `llm_providers.complete()` for anomaly/trend analysis.
- **`llm_providers.py`** — the shared LLM dispatch layer (see below). Used by both `metrics_analysis.py` and `rag/`.
- **`rag/`** — tangible-property-regulations RAG demo. `rag/ingest.py` scrapes/chunks CFR + IRS FAQ pages into a persistent ChromaDB collection; `rag/tpr_rag.py` embeds a question, retrieves matching chunks, and builds the grounded prompt; `rag/router.py` exposes `/api/v1/repair-tax-impact` (RAG-grounded) and `/api/v1/repair-tax-impact-no-rag` (same question sent straight to the LLM, for comparison).

### Pluggable LLM provider

`complete(prompt: str) -> str` in `llm_providers.py` is the single shared entrypoint — it dispatches to a provider adapter based on the `LLM_PROVIDER` env var (defaults to `"groq"`). Providers are registered in the `_PROVIDERS` dict, each mapping to a `_complete_with_<provider>(prompt: str) -> str` function.

- Adapters import their SDK **lazily inside the function**, not at module top level — this means you only need the SDK installed for the provider you actually use.
- `anthropic` is an **optional dependency** (`[project.optional-dependencies]` in `pyproject.toml`), not part of the default `uv sync`. If `LLM_PROVIDER=anthropic` is set without the extra installed, `_import_provider_sdk` raises an `ImportError` with the exact fix (`uv sync --extra anthropic`) rather than a bare traceback. In Docker, the equivalent is the `WITH_ANTHROPIC` build arg (default `false`) — `docker-compose.yml` passes it through from the `WITH_ANTHROPIC` env var, and the `Dockerfile` conditionally adds `--extra anthropic` to its `uv sync` when it's `true`.
- To add a new provider: write `_complete_with_<name>`, register it in `_PROVIDERS`, and (if it needs a new SDK) add it as an optional extra rather than a hard dependency.

### Metrics flow (important distinction)

Prometheus scrapes `app:9464` continuously in the background (every 5s per `prometheus.yml`), independent of the `/api/v1/analyze` route. Hitting that route does **not** trigger a scrape — it only reads whatever Prometheus has already stored for the requested window. If the stack was just started with no traffic yet, generate some first (e.g. hit `/work` a few times) before expecting a meaningful analysis.

## Testing conventions

- Tests must not make network calls or invoke real LLM/RAG calls (no live Prometheus, ChromaDB, embedding inference, or LLM API hits) — stub dependencies instead.
- Use `monkeypatch` (pytest's built-in fixture) to stub functions/dependencies, not `unittest.mock` — no extra import, auto-reverts per-test, and nothing here needs call-count/argument assertions that would justify `Mock`'s extra ceremony.
- Use `@pytest.mark.parametrize` (with explicit `id=` per case) when multiple test cases share the same call-and-assert shape and only the input/output data differs — one function, one `assert`, many data rows. Reach for separate `test_*` functions instead when the setup or the behavior under test genuinely differs between cases.
- `rag/tpr_rag.py` loads `SentenceTransformer`/`ChromaDB` at *import time* (not lazily), so any test that imports it risks a network download on a machine with a cold Hugging Face cache. `tests/test_tpr_rag.py` guards this with a module-level `pytest.skip(..., allow_module_level=True)` if the model isn't already cached — follow that pattern rather than importing it unconditionally.

## Environment variables

Set in `.env` (gitignored) — loaded by `docker-compose.yml` into the `app` service:

- `GROQ_API_KEY` — required for the default `groq` provider.
- `ANTHROPIC_API_KEY` — required only if `LLM_PROVIDER=anthropic`.
- `LLM_PROVIDER` — `groq` (default) or `anthropic`.
- `WITH_ANTHROPIC` — Docker build arg (not a runtime var), `false` by default; set `true` to install the `anthropic` extra in the image (see Toolchain & commands above).
- `TPR_RAG_DATA_DIR` — optional; where the RAG feature's ChromaDB index lives (`rag/ingest.py`, `rag/tpr_rag.py`). Defaults to `~/.tpr-rag/chroma_data`, deliberately outside the repo. In Docker, `docker-compose.yml` sets this to `/home/appuser/.tpr-rag/chroma_data` (the app container runs as a non-root `appuser`, uid 1000 — see `Dockerfile`) and bind-mounts the host's `~/.tpr-rag/chroma_data` into it — see `docs/tpr_rag_spec.md` for the full rationale.

## Gotchas

- **`Dockerfile` installs deps via `uv sync --frozen`** (not a manually maintained `pip install` list) — `pyproject.toml`/`uv.lock` are copied in and installed before app code, so dependency changes there are picked up automatically; no separate list to keep in sync.
- **`torch` is pinned to the CPU-only wheel index** (`[tool.uv.index]`/`[tool.uv.sources]` in `pyproject.toml`, Linux only) — this app never does GPU inference (`sentence-transformers` for RAG embeddings), so this avoids pulling several GB of unused CUDA libraries. If `uv sync` ever starts pulling the CUDA build again, check this override still matches the current `torch` version constraints.
- **The RAG embedding model is baked into the Docker image at build time** (`Dockerfile`, right after `uv sync`) so container startup never depends on Hugging Face Hub being reachable. This means image builds now have a one-time network dependency on HF Hub that didn't exist before RAG was added.
- Ruff `target-version = "py311"` even though `requires-python = ">=3.12"`.
- `.gitignore` references `data/`, `*.db`, `.terraform/`, `*.tfstate*` — likely relevant for future data store / infra work, not currently used.
- `docs/` holds two design docs: `tpr_rag_spec.md` (RAG design + post-build findings) and `devcontainer-spec.md` (sandboxed dev container). Two earlier planning specs (`otel-demo-spec.md`, `otel-demo-llm-analysis-spec.md`) were deleted as stale/superseded — don't reference them.
