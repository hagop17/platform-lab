# Docker Official Python image — Python's own build, pinned to an exact
# interpreter version and patched promptly for CVEs, so there's no
# self-managed Python install to maintain. `-slim` (Debian minimal + glibc)
# over `-alpine` (musl) so the precompiled manylinux wheels for torch /
# sentence-transformers / chromadb install directly instead of recompiling.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Deps only depend on pyproject.toml/uv.lock — copying them first (before
# app code) keeps this layer cached across code-only changes.
COPY pyproject.toml uv.lock ./
# --extra anthropic is opt-in via build arg since the SDK/key aren't needed
# by default (see CLAUDE.md "Pluggable LLM provider"). CPU-only torch is
# pinned in pyproject.toml's [tool.uv.sources] — this never does GPU
# inference, so --frozen won't pull the CUDA-bundled build.
ARG WITH_ANTHROPIC=false
RUN uv sync --frozen --no-install-project $(if [ "$WITH_ANTHROPIC" = "true" ]; then echo --extra anthropic; fi)

COPY main.py metrics_analysis.py llm_providers.py ./
COPY rag/ ./rag/

# Bake the embedding model into the image at build time, so container
# startup never depends on HF Hub being reachable (and doesn't risk
# unauthenticated rate limits). Cached under the default HF cache dir
# (~/.cache/huggingface, i.e. /root/... since this image runs as root),
# which sentence-transformers reuses automatically at runtime — same model
# name, same cache location, no re-download.
RUN uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

EXPOSE 8000 9464
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
