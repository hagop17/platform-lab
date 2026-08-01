# Docker Official Python image — Python's own build, pinned to an exact
# interpreter version and patched promptly for CVEs, so there's no
# self-managed Python install to maintain. `-slim` (Debian minimal + glibc)
# over `-alpine` (musl) so the precompiled manylinux wheels for torch /
# sentence-transformers / chromadb install directly instead of recompiling.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Non-root: a container compromise (e.g. an RCE in a dependency) then only
# has this user's privileges, not root's. Fixed UID 1000 — the common
# first-non-root-user convention — so the image behaves predictably if
# anything is ever bind-mounted into it.
RUN useradd --create-home --uid 1000 --shell /bin/bash appuser
WORKDIR /app
RUN chown appuser:appuser /app
USER appuser

# Deps only depend on pyproject.toml/uv.lock — copying them first (before
# app code) keeps this layer cached across code-only changes.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
# --extra anthropic is opt-in via build arg since the SDK/key aren't needed
# by default (see CLAUDE.md "Pluggable LLM provider"). CPU-only torch is
# pinned in pyproject.toml's [tool.uv.sources] — this never does GPU
# inference, so --frozen won't pull the CUDA-bundled build.
ARG WITH_ANTHROPIC=false
RUN uv sync --frozen --no-install-project $(if [ "$WITH_ANTHROPIC" = "true" ]; then echo --extra anthropic; fi)

COPY --chown=appuser:appuser main.py metrics_analysis.py llm_providers.py ./
COPY --chown=appuser:appuser rag/ ./rag/

# Bake the embedding model into the image at build time, so container
# startup never depends on HF Hub being reachable (and doesn't risk
# unauthenticated rate limits). Cached under appuser's HF cache dir
# (~/.cache/huggingface, i.e. /home/appuser/... since we run as appuser),
# which sentence-transformers reuses automatically at runtime — same model
# name, same cache location, no re-download.
RUN uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Build the RAG index into the image. Placed after the model bake because
# ingest needs the embedder, and after the code copy because it imports rag/.
# The source text is committed (docs/tpr-sources/, sha256-pinned by its
# manifest), so this step is fully offline — no scraping at build time and no
# network dependency at container start. TPR_RAG_DATA_DIR must match what
# docker-compose.yml sets at runtime, or the app would open an empty collection.
ENV TPR_RAG_DATA_DIR=/home/appuser/.tpr-rag/chroma_data
COPY --chown=appuser:appuser docs/tpr-sources/ ./docs/tpr-sources/
RUN uv run python -m rag.ingest

EXPOSE 8000 9464
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
