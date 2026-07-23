# otel-demo — LLM Metrics Analysis (Tier 1)

## Goal

Add a `GET /api/v1/analyze` route to the existing `otel-demo` FastAPI app that:

1. Queries Prometheus for a metric over a recent time window
2. Formats the result into compact, readable text
3. Sends that text to the Anthropic API and asks for anomaly/trend analysis
4. Returns the LLM's analysis as JSON

This is a **Tier 1** integration — no RAG, no vector DB, no embeddings. Just:
Prometheus data -> formatted text -> single LLM call -> response. It's meant
to demonstrate "demonstrable experience integrating API-based AI models into
software workflows" directly, as a standalone building block before any
RAG/vector-search work is attempted.

Builds on top of the existing `otel-demo` stack (FastAPI + OTel + Prometheus +
Grafana via Podman Compose) — do not change the existing routes, exporters,
or docker-compose/podman-compose setup. This is additive only.

## How it works (important distinction)

Prometheus **scrapes continuously in the background**, independent of this
feature — it pulls metrics from `app:9464` every 15s per the existing
`prometheus.yml` config, whether or not `/api/v1/analyze` is ever called.

Hitting `/api/v1/analyze` does **not** trigger a scrape. It only:

1. Queries Prometheus's already-stored data for the requested window
   (`query_prometheus`)
2. Formats that stored data into readable text (`format_metrics_for_llm`)
3. Sends the text to Claude for analysis (`analyze_with_llm`)

```
Prometheus (continuously, in background)
    scrapes app:9464 every 15s  ->  stores time-series data

Client hits GET /api/v1/analyze
    -> query_prometheus()       reads recent stored data from Prometheus
    -> format_metrics_for_llm() turns it into text
    -> analyze_with_llm()       sends text to Claude, gets analysis back
    -> response returned to client
```

**Implication for testing:** the quality of the analysis depends entirely on
what Prometheus has already recorded by the time `/analyze` is called. If the
stack was just started with no traffic yet, the queried window will be thin
or empty (handled by the `if not results: return "No data returned..."`
guard in `format_metrics_for_llm`). Always generate traffic first — see
Verification Steps below.

## Dependencies to add

```bash
uv add httpx anthropic
```

(`httpx` may already be present as a FastAPI dependency — check before adding.)

## Environment variable

Requires `ANTHROPIC_API_KEY` to be set in the environment. For local Podman
Compose runs, add it to the `app` service in `docker-compose.yml` /
`podman-compose.yml`:

```yaml
services:
  app:
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

This pulls from the host shell's env or a `.env` file next to the compose
file — never bake the key into the image or commit it. `.env` must already
be gitignored (confirm it is).

## New file: `app/metrics_analysis.py`

```python
import time
from datetime import datetime, timezone

import httpx
from anthropic import Anthropic

PROMETHEUS_URL = "http://prometheus:9090"  # service name inside the compose network


def query_prometheus(query: str, minutes: int = 15) -> dict:
    end_ts = time.time()
    start_ts = end_ts - (minutes * 60)

    resp = httpx.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": start_ts,
            "end": end_ts,
            "step": "15s",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def format_metrics_for_llm(prom_response: dict) -> str:
    """Turn Prometheus's nested JSON into compact, readable lines instead of dumping raw JSON."""
    results = prom_response.get("data", {}).get("result", [])
    if not results:
        return "No data returned for this query."

    lines = []
    for series in results:
        metric_labels = series.get("metric", {})
        label_str = ", ".join(f"{k}={v}" for k, v in metric_labels.items()) or "no labels"
        lines.append(f"Series [{label_str}]:")

        for ts, value in series.get("values", []):
            time_str = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
            lines.append(f"  {time_str} -> {value}")

    return "\n".join(lines)


def analyze_with_llm(metrics_text: str) -> str:
    client = Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": (
                "Here is recent Prometheus metrics data:\n\n"
                f"{metrics_text}\n\n"
                "Identify any anomalies, trend changes, or concerning patterns. "
                "Be specific and concise."
            ),
        }],
    )
    return msg.content[0].text


def analyze_metrics(query: str, minutes: int = 15) -> str:
    """End-to-end: fetch -> format -> analyze."""
    raw = query_prometheus(query, minutes)
    formatted = format_metrics_for_llm(raw)
    return analyze_with_llm(formatted)
```

## Route wiring in `app/main.py`

Add a new route (reuse the existing FastAPI `app` object, don't create a
second one):

```python
from metrics_analysis import analyze_metrics

@app.get("/api/v1/analyze")
def analyze(query: str = "up", minutes: int = 15):
    return {"analysis": analyze_metrics(query, minutes)}
```

## Verification steps

1. Rebuild/restart the stack: `podman-compose up --build`
2. Generate some traffic against existing routes so Prometheus has data to
   query, e.g.:
   ```bash
   for i in {1..20}; do curl http://localhost:8000/work; sleep 1; done
   ```
3. Hit the new route:
   ```bash
   curl "http://localhost:8000/api/v1/analyze?query=up&minutes=5"
   ```
4. Confirm the response is a JSON object with an `"analysis"` key containing
   a plain-English summary, not an error.
5. Try a query with no matching data (e.g. a nonexistent metric name) and
   confirm it returns "No data returned for this query." instead of crashing
   or sending an empty prompt to the LLM.

**Where to see the result:** there is no dashboard for this — it's a plain
HTTP response, not a metric, so it will not appear in Grafana or Prometheus.
View it via `curl` (optionally piped through `jq`), or via Swagger UI at
`http://localhost:8000/docs` (find `GET /api/v1/analyze`, "Try it out",
fill in `query`/`minutes`, Execute — the analysis renders in the browser).

## Error handling to include

- If `ANTHROPIC_API_KEY` is missing, the Anthropic client raises on
  instantiation — let this surface as a normal 500 for now (no need to
  catch/mask it in this demo).
- If Prometheus is unreachable or returns a non-2xx, `resp.raise_for_status()`
  raises `httpx.HTTPStatusError` — let it propagate for now; no custom retry
  logic needed at this stage.

## Explicitly out of scope for this pass

- No RAG / vector store / embeddings (that's Tier 2, separate spec later)
- No persistence of analysis results
- No caching of repeated queries
- No streaming responses from the LLM
- No changes to OTel trace/metric export configuration
