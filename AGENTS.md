# platform-lab

> See `CLAUDE.md` for the current, up-to-date guidance — kept here as a pointer for tools that look for `AGENTS.md` specifically.

FastAPI Tax-Engine Prototype.

## Toolchain

- **Package manager:** `uv` (do not use `pip` or `poetry`)
- **Run dev server:** `uv run fastapi dev main.py`
- **Lint:** `uv run ruff check .` (line-length: 100, target: py311)
- **Format:** `uv run ruff format .` (double quotes)
- **Typecheck:** `uv run pyright` (full project, no filenames passed)
- **Test:** `uv run pytest` (see CLAUDE.md "Testing conventions" — no network/LLM/RAG calls, monkeypatch for stubs, parametrize for same-shape cases)
- **Pre-commit hooks:** ruff (`--fix`), ruff-format, pyright, pytest (offline, `HF_HUB_OFFLINE=1`), check-yaml, trailing-whitespace, end-of-file-fixer

## Project Structure

- Flat layout — no `src/` or `app/` package. Top-level modules (`main.py`,
  `metrics_analysis.py`, `llm_providers.py`) plus a `rag/` package.
- Entrypoint: `main.py` defines `app = FastAPI()`, wires up OpenTelemetry, and
  mounts `rag.router`.
- Runtime deps include `fastapi[standard]`, OpenTelemetry + Prometheus client,
  `groq` (default LLM provider), and the RAG stack (`chromadb`,
  `sentence-transformers`, `beautifulsoup4`). See `pyproject.toml`; `anthropic`
  is an opt-in extra. Full architecture in `CLAUDE.md`.
- Python >=3.12.

## Conventions

- Must pass ruff lint+format **and** pyright before committing. Pre-commit enforces all three.
- Ruff `target-version = "py311"` (even though runtime is 3.12+).
- Tests live under `tests/` — see CLAUDE.md "Testing conventions".

## Gotchas

- `.gitignore` references `data/`, `*.db`, `.terraform/`, `*.tfstate*` — these may become relevant for future data store / infra work.
