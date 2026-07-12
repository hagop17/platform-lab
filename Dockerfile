FROM python:3.12-slim
WORKDIR /app
COPY main.py metrics_analysis.py llm_providers.py ./
COPY rag/ ./rag/
# CPU-only torch wheel first, before sentence-transformers can pull in the
# default CUDA-bundled build (which drags in several GB of unused GPU libs
# — this container never does GPU inference).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-instrumentation-fastapi \
    opentelemetry-exporter-prometheus \
    prometheus_client \
    httpx \
    groq \
    beautifulsoup4 \
    chromadb \
    python-dotenv \
    sentence-transformers
# Bake the embedding model into the image at build time, so container
# startup never depends on HF Hub being reachable (and doesn't risk
# unauthenticated rate limits). Cached under the default HF cache dir
# (~/.cache/huggingface, i.e. /root/... since this image runs as root),
# which sentence-transformers reuses automatically at runtime — same model
# name, same cache location, no re-download.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
EXPOSE 8000 9464
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
