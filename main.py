"""
Minimal FastAPI app with OpenTelemetry instrumentation.
Traces -> console (swap for OTLPSpanExporter to send to a Collector)
Metrics -> Prometheus format, scraped on port 9464
"""

import logging
import random
import time

from fastapi import FastAPI

# --- Tracing setup ---
# --- Metrics setup ---
from opentelemetry import metrics, trace
from opentelemetry.exporter.prometheus import PrometheusMetricReader

# --- Auto-instrumentation for FastAPI ---
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import start_http_server

from metrics_analysis import analyze_metrics

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
request_counter = meter.create_counter("app_requests_total", description="Total requests handled")
work_duration = meter.create_histogram(
    "app_work_duration_seconds", description="Time spent doing work"
)

# --- 3. Create the app and auto-instrument it ---
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
def health():
    return {"status": "ok"}


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


@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None) -> dict:
    return {"item_id": item_id, "q": q}


@app.get("/api/v1/analyze")
def analyze(query: str = "up", minutes: int = 15):
    return {"analysis": analyze_metrics(query, minutes)}
