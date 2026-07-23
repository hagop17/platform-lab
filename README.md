# platform-lab

[![CI](https://github.com/hagop17/platform-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hagop17/platform-lab/actions/workflows/ci.yml)

An end-to-end reference for instrumenting a service and turning its telemetry into
plain-language insight. A FastAPI app emits **OpenTelemetry** traces and metrics;
**Prometheus** scrapes them and **Grafana** visualizes them; an **LLM** reads a
metric window and reports anomalies. A second track shows **retrieval-augmented
generation (RAG)** grounding an LLM in real source documents — and a side-by-side
endpoint demonstrating how the same question answered *without* retrieval goes
wrong.

Built as an infrastructure/platform portfolio piece. Roadmap below covers the
Terraform, Kubernetes, and CI/CD layers being added on top.

---

## What's here

| Capability | Where | Notes |
|---|---|---|
| OpenTelemetry auto- + manual instrumentation | [`main.py`](main.py) | Traces → console, metrics → Prometheus pull endpoint on `:9464` |
| Prometheus + Grafana stack | [`docker-compose.yml`](docker-compose.yml), [`prometheus.yml`](prometheus.yml) | Scrapes the app every 5s |
| LLM-assisted metrics analysis | [`metrics_analysis.py`](metrics_analysis.py) | Queries Prometheus `query_range`, formats the series, asks an LLM for anomalies |
| RAG over U.S. tangible-property tax regulations | [`rag/`](rag/) | Scrapes + chunks CFR/IRS pages into ChromaDB, retrieves, builds a grounded prompt |
| Pluggable LLM provider | [`llm_providers.py`](llm_providers.py) | `groq` (default) or `anthropic`, selected by `LLM_PROVIDER`; SDKs imported lazily |
| Sandboxed dev container | [`.devcontainer/`](.devcontainer/) | Default-deny network firewall for unattended agentic work — see [design notes](docs/devcontainer-spec.md) |

The [`rag/ingest.py`](rag/ingest.py) chunker is the most involved piece: it parses
real-world government HTML (with documented traps around non-unique DOM ids and
false subsection boundaries) and packs regulation text into embedding-window-sized
sub-chunks. The reasoning is written up inline and in [`docs/tpr_rag_spec.md`](docs/tpr_rag_spec.md).

## Architecture

```
                 ┌────────────────────────┐
   HTTP  ───────▶│   FastAPI (main.py)    │
                 │   OpenTelemetry SDK     │
                 └──────┬──────────┬───────┘
             traces →   │          │  metrics (:9464, Prometheus format)
             console    │          │
                        │          ▼
                        │   ┌─────────────┐   scrape    ┌───────────┐
                        │   │ Prometheus  │◀────────────│  (5s)     │
                        │   └──────┬──────┘             └───────────┘
                        │          │ query_range
                        ▼          ▼
              ┌───────────────────────────┐        ┌───────────┐
              │ /api/v1/analyze            │───────▶│    LLM    │
              │  format series → prompt    │        │ (groq /   │
              └───────────────────────────┘        │ anthropic)│
                                                    └────▲──────┘
   RAG track:                                            │
   ┌───────────────┐  embed + retrieve  ┌──────────┐     │
   │ /repair-tax-  │───────────────────▶│ ChromaDB │     │
   │  impact       │   grounded prompt  └──────────┘     │
   └───────────────┴──────────────────────────────────────┘

   Grafana (:3000) reads Prometheus for dashboards.
```

## Quickstart

Requires Docker and a [Groq API key](https://console.groq.com) (free tier is enough).

```bash
# 1. Configure secrets
cp .env.example .env        # then edit: set GROQ_API_KEY=...

# 2. Bring up app + Prometheus + Grafana
docker compose up -d --build

# 3. Generate some telemetry (the analyze route reads stored data — it
#    does not trigger a scrape), then analyze it
for i in $(seq 20); do curl -s localhost:8000/work >/dev/null; done
sleep 10
curl -s "localhost:8000/api/v1/analyze?query=app_requests_total&minutes=15" | jq
```

- App: <http://localhost:8000> (route index at `/`, OpenAPI UI at `/docs`)
- Prometheus: <http://localhost:9090>
- Grafana: <http://localhost:3000> (`admin` / `admin` — local demo only)

### Local dev (no Docker)

```bash
uv sync
PROMETHEUS_URL=http://localhost:9090 uv run fastapi dev main.py
```

`PROMETHEUS_URL` defaults to the compose-internal hostname `http://prometheus:9090`;
override it when running the app on the host against a Prometheus you've exposed on
localhost.

## Example: metrics analysis

```bash
curl -s "localhost:8000/api/v1/analyze?query=rate(app_requests_total[1m])&minutes=15" | jq -r .analysis
```

```
The request rate for /work climbed steadily from ~0.2 req/s to ~1.1 req/s over the
window, with no gaps or drops — consistent with a ramp of synthetic load rather
than an incident. No anomalous spikes or flatlines. app_work_duration_seconds stayed
within its expected 0.05–0.30s band, so latency tracked load without degradation.
```

## Example: RAG vs. no-RAG

The RAG endpoint answers only from retrieved regulation text and cites its sources;
the no-RAG endpoint sends the same question straight to the LLM.

```bash
curl -s localhost:8000/api/v1/repair-tax-impact \
  -H 'content-type: application/json' \
  -d '{"description": "I replaced the entire roof on a rental property."}' | jq
```

```json
{
  "answer": "Classification: capitalize. Replacing an entire roof is a restoration...",
  "sources": ["1.263(a)-3(k)", "1.263(a)-3(j)", "1.263(a)-1(f)"]
}
```

The `sources` array is the tell: grounded answers cite specific CFR subsections;
the `-no-rag` variant returns a confident answer with no citations and no guarantee
it reflects the actual regulation text.

> **Note:** the RAG index is built by [`rag/ingest.py`](rag/ingest.py), which scrapes
> live CFR/IRS pages. Run it once on the host (`uv run python -m rag.ingest`) before
> hitting the RAG endpoints — see [`docs/tpr_rag_spec.md`](docs/tpr_rag_spec.md) for
> why the index lives outside the repo and how Docker mounts it.

## Development

| Task | Command |
|---|---|
| Install deps | `uv sync` |
| Run dev server | `uv run fastapi dev main.py` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Typecheck | `uv run pyright` |
| Test | `uv run pytest` |

Pre-commit runs ruff, pyright, and pytest; CI runs the same on every push. Tests
never make network or live-LLM calls — dependencies are stubbed (see
[`CLAUDE.md`](CLAUDE.md) → *Testing conventions*).

## Design notes

Longer-form design docs live in [`docs/`](docs/):

- [`otel-demo-spec.md`](docs/otel-demo-spec.md) — the observability stack
- [`otel-demo-llm-analysis-spec.md`](docs/otel-demo-llm-analysis-spec.md) — LLM metrics analysis
- [`tpr_rag_spec.md`](docs/tpr_rag_spec.md) — the RAG feature, chunking strategy, and HTML-parsing traps
- [`devcontainer-spec.md`](docs/devcontainer-spec.md) — the sandboxed dev container and firewall

## Roadmap

- [ ] Terraform to provision the stack
- [ ] Kubernetes manifests / Helm chart
- [ ] Grafana dashboards as code (provisioned, not click-configured)
- [ ] Ship traces to an OTel Collector instead of console

## License

MIT — see [LICENSE](LICENSE).
