# platform-lab

[![CI](https://github.com/hagop17/platform-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hagop17/platform-lab/actions/workflows/ci.yml)

An end-to-end reference for instrumenting a service and turning its telemetry into
plain-language insight. A FastAPI app emits **OpenTelemetry** traces and metrics;
**Prometheus** scrapes them and **Grafana** visualizes them; an **LLM** reads a
metric window and reports anomalies. A second track shows **retrieval-augmented
generation (RAG)** grounding an LLM in real source documents — and a side-by-side
endpoint answering the same question *without* retrieval, for comparison.

Built as an infrastructure/platform portfolio piece. The app is deliberately
small — the focus is on the engineering around it: supply-chain-hardened CI
(SHA-pinned actions, `pip-audit`, ruff security rules), non-root containers,
hermetic tests that never touch the network, AI-assisted development run inside
a [default-deny-firewalled dev container](docs/devcontainer-spec.md), and design
docs that record the tradeoffs and gotchas actually hit along the way. The same
service also runs on a Terraform-provisioned EKS cluster, deployed under a
least-privilege IAM role verified by a full create-and-destroy cycle.

---

## What's here

| Capability | Where | Notes |
|---|---|---|
| OpenTelemetry auto- + manual instrumentation | [`main.py`](main.py) | Traces → console, metrics → Prometheus pull endpoint on `:9464` |
| Prometheus + Grafana stack | [`docker-compose.yml`](docker-compose.yml), [`prometheus.yml`](prometheus.yml) | Scrapes the app every 5s |
| LLM-assisted metrics analysis | [`metrics_analysis.py`](metrics_analysis.py) | Queries Prometheus `query_range`, formats the series, asks an LLM for anomalies |
| RAG over U.S. tangible-property tax regulations | [`rag/`](rag/) | Chunks committed eCFR XML + IRS FAQ into ChromaDB, retrieves, builds a grounded prompt |
| Pluggable LLM provider | [`llm_providers.py`](llm_providers.py) | `groq` (default) or `anthropic`, selected by `LLM_PROVIDER`; model per provider via `GROQ_MODEL`/`ANTHROPIC_MODEL`; SDKs imported lazily |
| Sandboxed dev container | [`.devcontainer/`](.devcontainer/) | Default-deny network firewall for unattended agentic work — see [design notes](docs/devcontainer-spec.md) |
| EKS cluster as code | [`terraform/`](terraform/) | Split by lifetime: `bootstrap/` holds permanent identity and ECR, `eks/` holds everything billable and is destroyed after every session |
| Least-privilege deploy role | [`terraform/bootstrap/iam.tf`](terraform/bootstrap/iam.tf), [`boundary.tf`](terraform/bootstrap/boundary.tf) | Hand-written policy plus a permissions boundary, then narrowed from CloudTrail evidence and proven by a full apply → verify → destroy cycle as the deploy role |
| Kubernetes manifests | [`k8s/`](k8s/) | App + Prometheus, no PVCs or LoadBalancers — everything billable stays in Terraform's state so `destroy` reaches it |

The [`rag/ingest.py`](rag/ingest.py) chunker is the most involved piece: it parses
authoritative eCFR XML — fetched by [`rag/fetch_sources.py`](rag/fetch_sources.py)
from the government's own versioner API and committed to
[`docs/tpr-sources/`](docs/tpr-sources/) as a reviewable, sha256-pinned snapshot —
using a boundary rule that resolves the regulation's `(i)`-subsection ambiguity
(subsection letter vs. nested roman numeral), and packs the result into
embedding-window-sized sub-chunks. Because the source text is committed, those
same files double as test fixtures, so the chunker has real tests
([`tests/test_ingest.py`](tests/test_ingest.py)) for the first time. The reasoning
is written up inline and in [`docs/tpr_rag_spec.md`](docs/tpr_rag_spec.md).

## Architecture

One FastAPI process, three stories: the telemetry plumbing that carries metrics and
traces out of it, and two LLM features that differ only in where they get their
grounding context.

### A — Telemetry pipeline (pull model)

Nothing is pushed. The app passively serves its current metric state; Prometheus
decides when to read it.

```
┌───────────────────────────── app container ─────────────────────────────┐
│                                                                         │
│  FastAPI :8000                         OTel SDK (same process)          │
│  ┌────────────────────┐                ┌──────────────────────────────┐ │
│  │ /work              │─ .record() ───▶│ MeterProvider                │ │
│  │                    │─ .add()    ───▶│   app_work_duration_seconds  │ │
│  │ all routes, via    │                │   app_requests_total         │ │
│  │ FastAPIInstrumentor│─ spans ───────▶│ TracerProvider               │ │
│  └────────────────────┘                └──────┬───────────────┬───────┘ │
│                                               │               │         │
│                        BatchSpanProcessor ────┘               │ live    │
│                        → ConsoleSpanExporter                  │ read    │
│                                  │                            │         │
│                                  ▼                    ┌───────▼───────┐ │
│                          container stdout             │ prometheus_   │ │
│                       (docker compose logs app)       │ client WSGI   │ │
│                                                       │ srv :9464     │ │
│                                                       └───────▲───────┘ │
└───────────────────────────────────────────────────────────────┼─────────┘
                                                                │
                                   GET app:9464/metrics every 5s│
                                   (Prometheus initiates)       │
                                        ┌───────────────────────┴───────┐
                                        │ prometheus container :9090    │
                                        │ TSDB — no volume mounted,     │
                                        │ so history dies with the      │
                                        │ container                     │
                                        └───────────────▲───────────────┘
                                                        │ PromQL
                                        ┌───────────────┴───────────────┐
                                        │ grafana container :3000       │
                                        │ (datasource added by hand)    │
                                        └───────────────────────────────┘
```

`start_http_server(port=9464)` in [`main.py`](main.py) runs a second HTTP server, on
its own thread, in the same process as uvicorn — it is not a FastAPI route. It serves
whatever collectors are registered in `prometheus_client`'s global registry, which is
where `PrometheusMetricReader` quietly registers itself on construction.

### B — Metric analysis: grounding in a time-series DB

Note the role reversal against A. Here the app is the client and Prometheus answers.

```
┌───────────────────────── app container ────────────────────────┐
│  FastAPI :8000                                                 │
│  ┌──────────────────┐                                          │
│  │ /api/v1/analyze  │  ← end user: GET ?query=up&minutes=15     │
│  └────────┬─────────┘                                          │
│           │ metrics_analysis.analyze_metrics()                 │
│           │                                                    │
│           │ 1. httpx GET prometheus:9090/api/v1/query_range ───┼──▶ Prometheus
│           │    ◀────────────────────── JSON time series ───────┼───
│           │ 2. format_metrics_for_llm() → compact text         │
│           │ 3. llm_providers.complete(prompt) ─────────────────┼──▶ Groq /
│           │    ◀────────────────────── analysis text ──────────┼─── Anthropic
│           ▼                                                    │
│      {"analysis": "..."}                                       │
└────────────────────────────────────────────────────────────────┘
```

Hitting this route does **not** trigger a scrape — it reads what Prometheus already
stored. On a freshly started stack, generate traffic (`/work`) first or the window is
empty. Both outbound calls above emit spans of their own, via
`HTTPXClientInstrumentor` in [`main.py`](main.py), so they show up in A's trace output.

### C — RAG: grounding in a vector store

Same shape as B, different context source — but split across three phases that happen
at three different times. The `═══` lines are where the artifact changes form.

```
 PHASE 1 — human-run, occasional, NETWORK
 ┌──────────────────────┐   eCFR versioner API (XML)
 │ rag/fetch_sources.py │◀── IRS FAQ (HTML)
 └──────────┬───────────┘
            │ writes verbatim + sha256
            ▼
   docs/tpr-sources/  +  _manifest.json      ← committed to git
            │                    ▲
            │                    └── tests/test_manifest.py re-checks hashes
            │
 ═══════════╪══════════════════════════════════════════════════
 PHASE 2 — docker build, OFFLINE except HF Hub
            │
            ▼
 ┌──────────────────────┐        ┌───────────────────────────┐
 │ rag/ingest.py        │        │ SentenceTransformer        │
 │  parse XML + HTML    │───────▶│ all-MiniLM-L6-v2           │
 │  chunk               │        │ (downloaded at build)      │
 │  embed               │        └───────────────────────────┘
 └──────────┬───────────┘
            │
            ▼
   ChromaDB index @ /home/appuser/.tpr-rag/chroma_data
   ── baked INTO the image, no bind mount ──
            │
 ═══════════╪══════════════════════════════════════════════════
 PHASE 3 — runtime, per request, OFFLINE except the LLM
            │
  POST /api/v1/repair-tax-impact                    ┌──────────┐
            │                                       │ Groq /   │
            ▼                                       │ Anthropic│
   embed question ─▶ retrieve top-k ─▶ grounded ───▶│          │
   (same model,      (from the index    prompt      └────┬─────┘
    now local)        above)                             │
                                                         ▼
                                      {"answer": ..., "sources": [...]}
```

Phase 1 is the only network-touching step, and once run it changes retrieval results
with no human in the loop — which is what
[`tests/test_manifest.py`](tests/test_manifest.py) guards against.
`/api/v1/repair-tax-impact-no-rag` skips phases 1–3 entirely and sends the bare
question to the LLM, for comparison.

### The same topology on EKS

**All three run unchanged on EKS**, because Kubernetes Service DNS matches
Compose service DNS: `prometheus.yml` still scrapes `app:9464`, and
[`metrics_analysis.py`](metrics_analysis.py) still resolves `http://prometheus:9090`.
Two differences worth knowing: **Grafana is not deployed there** — it adds no new
telemetry story and a click-built dashboard dies with the cluster — and nothing is
exposed publicly, so access is `kubectl port-forward` rather than a LoadBalancer. See
the [design spec](docs/superpowers/specs/2026-08-14-eks-cluster-design.md) for why
both are deliberate.

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

### On EKS (needs an AWS account, costs money)

The cluster is deliberately ephemeral — ~$0.19/hr, ~18 minutes to create and ~10 to
destroy — so it exists only while in use.

**Once, ever.** [`terraform/bootstrap/`](terraform/bootstrap/) holds what must outlive
any cluster: the two IAM roles EKS assumes, the deploy role with its permissions
boundary, the ECR repository, and a budget alarm. It is applied by an admin identity and
is never part of a `destroy`.

```bash
cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars
# fill in account_id, the state bucket name, and budget_alert_email — gitignored
terraform -chdir=terraform/bootstrap apply

SHA=$(git rev-parse --short HEAD)
ECR=$(terraform -chdir=terraform/bootstrap output -raw ecr_repository_url)
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin "${ECR%%/*}"
docker build --provenance=false -t "$ECR:$SHA" .    # the flag is load-bearing —
docker push "$ECR:$SHA"                             # see CLAUDE.md's ECR gotcha
```

**Every session.** [`terraform/eks/`](terraform/eks/) holds everything billable — VPC,
cluster, node group — and is torn down each time. The first three lines are one-time
setup per machine; the rest is the cycle:

```bash
cp terraform/eks/backend.hcl.example terraform/eks/backend.hcl
cp terraform/eks/terraform.tfvars.example terraform/eks/terraform.tfvars
# fill both in (also gitignored), then:
terraform -chdir=terraform/eks init -backend-config=backend.hcl

terraform -chdir=terraform/eks apply
aws eks update-kubeconfig --name platform-lab --region us-west-2
kubectl create secret generic platform-lab-secrets --from-env-file=.env
kubectl apply -f k8s/          # except app-deployment.yaml — its ACCOUNT_ID and
                               # REPLACE_WITH_GIT_SHA placeholders are substituted
                               # at deploy time, never committed
kubectl port-forward svc/app 8000:8000

terraform -chdir=terraform/eks destroy    # then confirm: aws eks list-clusters
```

Everything billable lives in Terraform's state, so `destroy` reaches all of it — that
invariant is why there is no LoadBalancer, no PersistentVolumeClaim and no NAT gateway.
The full runbook, including verifying the whole cycle under the least-privilege deploy
role, is Tasks 12–14 of the
[implementation plan](docs/superpowers/plans/2026-08-14-eks-cluster.md).

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
  "answer": "1. Classification: Capitalize\n2. Safe harbor or BAR-test category: ... the replacement of the entire roof would be considered a restoration of a major component and a substantial structural part of the building under paragraphs (k)(1)(vi) and (k)(2) of section 1.263(a)-3\n3. Specific section(s) cited: 1.263(a)-3(k), specifically paragraphs (e)(2)(ii), (k)(1)(vi), (k)(2), and (k)(6)(ii)(A) and (B)...",
  "sources": ["1.263(a)-3(k)"]
}
```

The `sources` array is the tell: grounded answers cite specific CFR subsections;
the `-no-rag` variant returns a confident answer with no citations and no guarantee
it reflects the actual regulation text.

> **Note:** the RAG index is built by [`rag/ingest.py`](rag/ingest.py) from the
> committed eCFR/IRS snapshot in [`docs/tpr-sources/`](docs/tpr-sources/) — no
> network access required. In Docker it's **baked into the image at build time**,
> so there's nothing to run before hitting the RAG endpoints. For local
> (non-Docker) dev, run `uv run python -m rag.ingest` once (offline, fast) to
> build the index at `TPR_RAG_DATA_DIR`. To refresh the underlying regulation
> text itself (occasional, human-run, needs network), see
> `uv run python -m rag.fetch_sources` in [`CLAUDE.md`](CLAUDE.md).

## Development

| Task | Command |
|---|---|
| Install deps | `uv sync` |
| Run dev server | `uv run fastapi dev main.py` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Typecheck | `uv run pyright` |
| Test | `uv run pytest` |
| Validate Terraform | `terraform -chdir=terraform/<stack> init -backend=false && terraform -chdir=terraform/<stack> validate` |
| Validate k8s manifests | `kubeconform -strict -summary k8s/*.yaml` |

Pre-commit runs ruff, ruff-format, pyright, pytest and `terraform fmt -check`. CI
runs those plus `pip-audit`, `terraform validate` on both stacks, and
`kubeconform -strict` over [`k8s/`](k8s/). The infra checks need no cloud
credentials — `-backend=false` skips S3 — so every push validates the Terraform and
manifests offline. Tests never make network or live-LLM calls — dependencies are
stubbed (see [`CLAUDE.md`](CLAUDE.md) → *Testing conventions*).

## Design notes

Longer-form design docs live in [`docs/`](docs/):

- [`tpr_rag_spec.md`](docs/tpr_rag_spec.md) — the RAG feature, chunking strategy, and the eCFR XML parsing rules
- [`devcontainer-spec.md`](docs/devcontainer-spec.md) — the sandboxed dev container and firewall
- [`2026-08-14-eks-cluster-design.md`](docs/superpowers/specs/2026-08-14-eks-cluster-design.md) — the EKS design: cost-driven teardown discipline, the permissions boundary, and an honest exposure assessment of what the deploy role could do
- [`2026-08-14-eks-cluster.md`](docs/superpowers/plans/2026-08-14-eks-cluster.md) — the 14-task execution plan, with each place reality contradicted it recorded in line
- [`guides/rag-embeddings-primer.md`](docs/guides/rag-embeddings-primer.md) — a standalone primer on how embeddings and vector search actually work, with measured latency numbers from this repo's retrieval path

## How this was built

Feature work here runs as a written pipeline rather than ad-hoc prompting, using the
[Superpowers](https://github.com/obra/superpowers) plugin for Claude Code. Each stage
produces a committed artifact:

1. **Brainstorm → design spec.** The problem, the approach, the tradeoffs, the risks,
   and what's explicitly out of scope — written and agreed before any code.
2. **Design spec → implementation plan.** A task-by-task breakdown carrying exact code,
   exact test assertions, and a verification step per task.
3. **Plan → execution.** Each task is implemented and reviewed against the plan before
   the next begins, with a final pass over the whole branch. How much of that is
   delegated to subagents versus done interactively varies by task — the RAG work was
   largely subagent-driven; the Kubernetes manifests were hand-written to learn the
   material, and the live AWS tasks were run at a terminal.

The July 2026 migration of the tangible-property corpus — from scraped Cornell LII HTML
to the authoritative eCFR versioner API — is the worked example:

- [design spec](docs/superpowers/specs/2026-07-31-ecfr-sources-design.md) — why the source changed
- [implementation plan](docs/superpowers/plans/2026-07-31-ecfr-sources.md) — the five tasks it was built from
- the commit history — roughly one commit per task

Those two documents are point-in-time records and are deliberately **not** updated after
the fact. The design spec estimated 500–700 chunks; the shipped index has 467. Keeping
the estimate visible next to the outcome is more useful than quietly correcting it.

The EKS work is the larger example, and the one where the gap between plan and reality
was widest:

- [design spec](docs/superpowers/specs/2026-08-14-eks-cluster-design.md) — eight decisions, a permissions boundary written from intent, and an exposure assessment that asks what the deploy role could do if someone else obtained it
- [implementation plan](docs/superpowers/plans/2026-08-14-eks-cluster.md) — 14 tasks, the last three run against live AWS

Ten of its steps carry a **"Corrected during execution"** note where reality contradicted
the plan, left in place rather than rewritten: a manifest-validation gate that turned out
to need a live cluster, credentials that expired mid-`destroy` and left a cluster running,
a `docker build` whose default provenance attestation quietly reduced ECR retention to one
image, and a deploy step that would have committed an account ID the design forbids. The
deploy role needed one permission it was never granted — discovered fifteen minutes into a
run, under least privilege only — and was then narrowed using CloudTrail evidence, which
removed nine actions and wrongly removed three more that a failed apply put straight back.

## Roadmap

- [x] Terraform to provision the stack (`terraform/bootstrap/`, `terraform/eks/`)
- [x] Kubernetes manifests (`k8s/`)
- [ ] CI/CD deploy to EKS via the existing GitHub OIDC role
- [ ] Ship traces to an OTel Collector and a trace backend — metrics have a full path today, traces go to the console and vanish

## License

MIT — see [LICENSE](LICENSE).

Source documents in [`docs/tpr-sources/`](docs/tpr-sources/) are US federal
regulations and IRS publications — public domain, reproduced verbatim.

[`docs/guides/rag-embeddings-primer.md`](docs/guides/rag-embeddings-primer.md)
is licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), free to
share and adapt, including commercially, with attribution.
