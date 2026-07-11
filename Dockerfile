FROM python:3.12-slim
WORKDIR /app
COPY main.py .
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-instrumentation-fastapi \
    opentelemetry-exporter-prometheus \
    prometheus_client
EXPOSE 8000 9464
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
