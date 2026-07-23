# OTel + Prometheus + Grafana Demo (Podman)

## Goal

A local Podman Compose stack demonstrating OpenTelemetry instrumentation in a
FastAPI app, exporting metrics to Prometheus (pull model) and traces to
console, with Grafana visualizing the metrics. This is a standalone learning
demo, not part of the main fastapi-demo portfolio app.

## Structure to create

```
otel-demo/
├── docker-compose.yml
├── prometheus.yml
└── app/
    ├── Dockerfile
    └── main.py
```

## Requirements

- Use **Podman / podman-compose**, not Docker (commands: `podman-compose up
  --build` or `podman compose up --build`)
- FastAPI app exposing routes: `/`, `/items/{item_id}`, `/health`, `/work`
- OTel traces exported via `ConsoleSpanExporter` (no Collector for this demo)
- OTel metrics exported via `PrometheusMetricReader`, exposed on port `9464`
- FastAPI itself serves on port `8000`
- Prometheus scrapes `app:9464` every 5s
- Grafana runs on port `3000` (`admin`/`admin`); Prometheus
  (`http://prometheus:9090`) should be added as a data source manually after
  first login
- The `prometheus.yml` bind mount may need the `:Z` SELinux relabel flag on
  AlmaLinux/WSL2 rootless Podman — add `:Z` to the volume mount if Prometheus
  fails to read the file

## `app/main.py`

```python
"""
Minimal FastAPI app with OpenTelemetry instrumentation.
Traces -> console (swap for OTLPSpanExporter to send to a Collector)
Metrics -> Prometheus format, scraped on port 9464
"""

import time
import random
import logging

from fastapi import FastAPI

# --- Tracing setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource

# --- Metrics setup ---
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import start_http_server

# --- Auto-instrumentation for FastAPI ---
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

logging.basicConfig(level=logging.INFO)

# A Resource identifies this service in whatever backend receives the data
resource = Resource.create({"service.name": "fastapi-demo"})

# --- 1. Configure tracing ---
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(__name__)

# --- 2. Configure metrics (Prometheus pull model) ---
prometheus_reader = PrometheusMetricReader()
meter_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Start the Prometheus scrape endpoint on a separate port
start_http_server(port=9464)

# Custom instruments
request_counter = meter.create_counter(
    "app_requests_total", description="Total requests handled"
)
work_duration = meter.create_histogram(
    "app_work_duration_seconds", description="Time spent doing work"
)

# --- 3. Create the app and auto-instrument it ---
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None) -> dict:
    return {"item_id": item_id, "q": q}


@app.get("/work")
def do_work():
    with tracer.start_as_current_span("do_work") as span:
        start = time.time()
        duration = random.uniform(0.05, 0.3)
        time.sleep(duration)

        span.set_attribute("simulated_duration", duration)

        work_duration.record(time.time() - start, {"endpoint": "/work"})
        request_counter.add(1, {"endpoint": "/work", "status": "ok"})

        return {"status": "done", "duration": duration}
```

## `app/Dockerfile`

```dockerfile
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
```

## `docker-compose.yml`

```yaml
services:
  app:
    build: ./app
    ports:
      - "8000:8000"   # FastAPI app
      - "9464:9464"   # OTel Prometheus metrics endpoint

  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:Z
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
```

## `prometheus.yml`

```yaml
global:
  scrape_interval: 5s

scrape_configs:
  - job_name: "fastapi-demo"
    static_configs:
      - targets: ["app:9464"]
```

## Verification steps (after `podman-compose up --build`)

1. `curl http://localhost:8000/` and `curl http://localhost:8000/items/42?q=test`
   — confirm both return expected JSON
2. `curl http://localhost:8000/work` a few times — generates custom metrics
3. `podman-compose logs -f app` — confirm trace spans print to console for
   all routes (auto-instrumented ones and the manual `do_work` span)
4. `curl http://localhost:9464/metrics` — confirm `app_requests_total` and
   `app_work_duration_seconds` appear in Prometheus exposition format
5. Open `http://localhost:9090/targets` — confirm the `fastapi-demo` target
   shows state `UP`
6. Open `http://localhost:3000` (login `admin`/`admin`) — add Prometheus
   (`http://prometheus:9090`) as a data source, then build a panel querying
   `rate(app_requests_total[1m])` or
   `histogram_quantile(0.95, rate(app_work_duration_seconds_bucket[5m]))`

## Task for Claude Code

Create the files and structure described above exactly as specified, then
run `podman-compose up --build` (or `podman compose up --build`) and walk
through the verification steps to confirm the full pipeline works end to end.
If the Prometheus container fails to read `prometheus.yml`, check whether the
`:Z` SELinux flag needs adjusting for the local environment.
