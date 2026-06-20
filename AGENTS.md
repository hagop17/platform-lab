# fastapi-demo

FastAPI Tax-Engine Prototype.

## Toolchain

- **Package manager:** `uv` (do not use `pip` or `poetry`)
- **Run dev server:** `uv run fastapi dev main.py`
- **Lint:** `uv run ruff check .` (line-length: 100, target: py311)
- **Format:** `uv run ruff format .` (double quotes)
- **Typecheck:** `uv run pyright` (full project, no filenames passed)
- **Test:** `uv run pytest` (no tests written yet but pytest is a dev dep)
- **Pre-commit hooks:** ruff (`--fix`), ruff-format, pyright, check-yaml, trailing-whitespace, end-of-file-fixer

## Project Structure

- Flat layout — no `src/` or `app/` package. Single module `main.py`.
- Entrypoint: `main.py` defines `app = FastAPI()`.
- Runtime deps: only `fastapi[standard]` (includes uvicorn, httpx, jinja2, etc.).
- Python >=3.12.

## Conventions

- Must pass ruff lint+format **and** pyright before committing. Pre-commit enforces all three.
- Ruff `target-version = "py311"` (even though runtime is 3.12+).
- No existing tests — add under `tests/` when creating them.

## Gotchas

- `.gitignore` references `data/`, `*.db`, `.terraform/`, `*.tfstate*` — these may become relevant for future data store / infra work.
