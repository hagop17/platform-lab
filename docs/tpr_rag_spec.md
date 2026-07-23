# TPR RAG — Tangible Property Regulations Question Answering

## Goal

Build a standalone Python RAG project that answers natural-language questions
about whether a repair/improvement must be capitalized or can be deducted,
grounded in the actual U.S. tangible property regulations (TPR) — not the
LLM's general training knowledge.

This lives inside the existing `platform-lab` repo, in its own subdirectory
(`rag/`) — separate from the OTel/Prometheus/Grafana pieces (which are about
observability, not RAG), but not a standalone repo.

End goal: a `POST /api/v1/repair-tax-impact` endpoint that takes a plain-English
description of a repair and returns an answer on its tax treatment, with
citations to the specific regulation section(s) used.

## Architecture (two stages)

**Ingestion pipeline (offline, run once, re-run only when source docs change):**
```
Regulation texts (Cornell LII, IRS, Federal Register)
  -> chunked by regulation subsection
  -> embedded (local model, sentence-transformers)
  -> stored in ChromaDB (persisted to local disk)
```

**Query pipeline (runtime, per request):**
```
User's repair description / question
  -> embedded (same model as ingestion)
  -> ChromaDB similarity search (top-k chunks)
  -> chunks + question assembled into a prompt
  -> sent to Claude
  -> answer returned, citing which section(s) it used
```

The LLM never touches ChromaDB directly. Retrieval happens in Python code;
the LLM only sees the final assembled prompt.

## Dependencies

`httpx` and `fastapi` are already dependencies of this repo; `anthropic` is
already an optional extra (see `CLAUDE.md`). `chromadb`,
`sentence-transformers`, `python-dotenv`, and `beautifulsoup4` are new:

```bash
uv add chromadb sentence-transformers python-dotenv beautifulsoup4
```

- `beautifulsoup4` — parses the raw HTML fetched from each source page so
  the ingestion code can pull out just the regulation text (see "Source
  selection" below), discarding page chrome.
- `chromadb` — the vector store: persists each chunk's embedding and
  performs the similarity search that is the "R" (retrieval) in RAG.
- `sentence-transformers` — turns text into embeddings locally (no API
  call); used identically at ingest time (embedding chunks) and query time
  (embedding the user's question).
- `python-dotenv` — see the `.env` loading note below.

`python-dotenv` is required because `rag/ingest.py` is normally run directly
(`python rag/ingest.py`), which does **not** automatically load `.env` the
way `uv run fastapi dev` does. Both `rag/ingest.py` and `rag/tpr_rag.py`
call `load_dotenv()` explicitly (see code below) so `TPR_RAG_DATA_DIR` and
`ANTHROPIC_API_KEY` resolve identically no matter which entry point is
used — otherwise the two scripts could silently disagree on where the
index lives.

No Docker/Podman required for this piece — single Python process, no
multi-service stack, and it doesn't need to join the existing
`docker-compose.yml` stack. ChromaDB runs embedded (`PersistentClient`), not
as a separate server.

**Operational rule:** don't run `rag/ingest.py` while the FastAPI app is
serving traffic against the same index. `tpr_rag.py` opens the ChromaDB
client once at import time and holds it as a module-level singleton, so a
running app won't necessarily see newly-ingested data — and ingestion
writes could contend with an in-flight query against the same on-disk
files. Always: run ingestion, *then* (re)start the app. (A hot-reload
mechanism that avoids the restart is possible but not worth the added
complexity for this project's scale — see Verification steps.)

## Environment variables

```bash
ANTHROPIC_API_KEY=sk-ant-...
TPR_RAG_DATA_DIR=/path/to/chroma_data   # optional, see below
```
`ANTHROPIC_API_KEY` follows the same `.env` + gitignore pattern as other
projects — never commit the key.

`TPR_RAG_DATA_DIR` controls where ChromaDB persists its index. It's
intentionally kept **outside the repo/source tree** so the data survives a
`git clean`, a repo re-clone, or deleting `rag/` entirely, and so it never
risks getting committed. Default (when unset): `~/.tpr-rag/chroma_data` — a
fixed location under the user's home directory, independent of the repo
path or whatever directory a script happens to be run from.

---

## Part 1: Source documents

Primary sources (public domain — U.S. federal regulations, no copyright
restriction on reproducing full text):

- **Treas. Reg. §1.263(a)-1** — general capitalization rule, de minimis
  safe harbor — `https://www.law.cornell.edu/cfr/text/26/1.263(a)-1`
- **Treas. Reg. §1.263(a)-3** — the core BAR test: definitions (d),
  betterments (j), restorations (k), adaptations (l), small-taxpayer safe
  harbor (h) — `https://www.law.cornell.edu/cfr/text/26/1.263(a)-3`.
  (Routine maintenance safe harbor, originally expected at a distinct
  top-level `(i)`, is **not** ingested as its own subsection — see
  "Confirmed HTML structure" below for why.)
- **IRS FAQ** (plain-English cross-reference, useful for casually-phrased
  questions) —
  `https://www.irs.gov/businesses/small-businesses-self-employed/tangible-property-final-regulations`

Start with just these three. Do not attempt to ingest the full corpus of
Rev. Procs and the Federal Register preamble in the first pass — that's a
later expansion, not v1.

### Source selection: why Cornell LII, not eCFR.gov

The official U.S. government CFR mirror, **eCFR.gov**, was considered as a
source for the two regulation-text documents instead of Cornell LII, since
it's the authoritative primary source rather than a third-party mirror.
It was rejected: eCFR.gov (which shares infrastructure with
federalregister.gov) actively blocks plain scripted HTTP requests — a
`curl`/`httpx` GET against it returns a "Federal Register :: Request
Access" bot-check page, not the regulation text, confirmed by directly
fetching `https://www.ecfr.gov/current/title-26/part-1/section-1.263(a)-3`.
Since `fetch_regulation_text()` uses a plain `httpx.get()` with no headless
browser or JS challenge-solving, eCFR.gov is not fetchable by this
ingestion pipeline. Cornell LII was directly confirmed fetchable (a plain
`httpx`/`curl` GET against `1.263(a)-3` returns real content, ~954 KB) and
requires no such workaround, so it remains the source for both regulation
texts.

### Confirmed HTML structure (Cornell LII)

Fetched and inspected directly (see Source selection above) rather than
guessed. Cornell LII marks each top-level lettered subsection as:

```html
<p class="psection-1">
  <span class="enumxml" id="j">(j)</span>
  <span class="et03">Capitalization of betterments</span>—(1) ...
</p>
```

i.e. a `<p class="psection-1">` whose first child is `<span class="enumxml"
id="{letter}">({letter})</span>`, immediately followed by a `<span
class="et03">` containing the subsection's short title (used as the
`topic` metadata field), followed by sibling `<p>` tags (e.g.
`class="psection-2"` for nested `(j)(1)`, `(j)(2)`, ...) making up the
subsection's body, until the next top-level marker.

**Confirmed also on `1.263(a)-1`** — same `psection-1`/`enumxml`/`et03`
pattern holds, with one wrinkle: the `et03` title span can contain nested
markup (e.g. a `definedterm` link), so extraction must use `.get_text()`,
not assume the title is plain text.

**Identifying genuine top-level markers is subtle — two traps.** The `id`
attribute is not reliably unique: nested roman-numeral list items deeper in
the page (e.g. a 2-item list under `(h)(5)` labeled "(i)"/"(ii)") share the
`psection-1` class and even collide on literal `id` with a genuine
top-level letter (`1.263(a)-3` has two elements with `id="i"`), so `id`
can't be trusted. A stateful "next expected letter `a, b, c, ...`" filter
*also* fails — an approach we tried and had to abandon: the bogus nested
`(i)` sits right after `(h)`, so its label happens to equal the next
expected letter `i` and gets accepted, which then **truncated the `(h)`
chunk at that false boundary** (a real bug — `(h)` came out as a 462-char
stub); and once the genuine top-level `(i)` is absent, a strict sequence
stalls waiting for `i` and drops `(j)` onward.

What actually works (`_top_level_markers`) requires **both**: a single
alphabetic label (`(a)`..`(r)`, which rejects the two-char `(ii)` items)
**and** a non-empty `<span class="et03">` title sibling (which rejects the
empty-titled nested `(i)`/`(ii)` list items). Verified on both regulation
pages: every genuine subsection has a non-empty `et03` title; every bogus
nested marker fails one of the two checks. No `id`, no stateful sequencing.

**`(i)` has no distinct top-level heading, so routine-maintenance content
now falls inside the `(h)` chunk region.** Confirmed by direct inspection:
this page has no top-level `(i)` heading paragraph the way `(h)`/`(j)`/
`(k)`/`(l)` do — the routine-maintenance safe harbor exists but nested with
no stable top-level anchor, and the only `id="i"` element is the unrelated
list item under `(h)(5)` (even Cornell's own `href="#i"` links resolve to
it). `(i)` is therefore excluded from the target subsection set (`1.263(a)-3`
targets `{d, h, j, k, l}`). Because `(h)` is now correctly bounded by the
next genuine letter `(j)`, its region spans and captures the
routine-maintenance text — so that content **is** retrievable, but its CFR
citations read `1.263(a)-3(h)` (the small-taxpayer safe harbor label) since
it has no separable anchor. The IRS FAQ's dedicated "Safe harbor for
routine maintenance" entry provides a cleaner alternate citation for it.

The IRS FAQ page uses a completely different structure — a single
`<article>` container (confirmed to be the only one on the page, cleanly
separating real content from surrounding nav chrome) with each question
as an `<h2>`/`<h3>`/`<h4>` heading followed by one or more `<p>` answer
paragraphs until the next heading. It needs its own chunking function
(`chunk_irs_faq`), not `chunk_by_subsection`.

## Part 2: Ingestion script (`rag/ingest.py`)

Fetch each source page, extract the regulation text, and split it into
chunks **by regulation subsection** (e.g. `(d)`, `(j)`, `(k)`, `(l)`, `(h)`,
`(i)`) — not by fixed character count. Subsection boundaries are natural,
semantically coherent units; arbitrary character-count chunking would cut
across e.g. a betterment definition mid-sentence.

Each chunk gets metadata: `{"source": "1.263(a)-3", "subsection": "j",
"topic": "betterment"}` (or similar — topic is a short human label for
what that subsection covers: betterment / restoration / adaptation /
routine maintenance / small taxpayer safe harbor / de minimis safe harbor /
definitions).

```python
import os
from pathlib import Path

import httpx
import chromadb
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv()  # explicit — don't rely on the runner (fastapi dev, etc.) to load .env

EMBED_MODEL = "all-MiniLM-L6-v2"  # same model used at query time, must match
# Outside the repo by default — survives repo deletion/reclone, never git-tracked.
CHROMA_PATH = Path(os.environ.get("TPR_RAG_DATA_DIR", Path.home() / ".tpr-rag" / "chroma_data"))
COLLECTION_NAME = "tpr_regulations"

def fetch_regulation_text(url: str) -> str:
    resp = httpx.get(url, timeout=15.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text  # will need HTML parsing — see note below

def chunk_by_subsection(html_or_text: str, source_label: str) -> list[dict]:
    """
    Returns a list of {"text": ..., "metadata": {...}} dicts, one per
    regulation subsection. Exact parsing depends on the page's HTML
    structure on Cornell LII — inspect the page structure first (subsection
    markers like "(j)" typically appear as bolded/anchored headers) rather
    than guessing a regex up front.
    """
    raise NotImplementedError  # implement after inspecting actual page HTML

def build_index():
    model = SentenceTransformer(EMBED_MODEL)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # Delete-and-recreate rather than incrementally updating: this corpus is
    # tiny (3 source docs, a couple dozen chunks) so a full rebuild is near-
    # instant, and it keeps re-ingestion simple — no need to reconcile
    # stale/removed chunks from a prior run.
    try:
        client.delete_collection(COLLECTION_NAME)
    except ValueError:
        pass  # collection didn't exist yet (first run)

    collection = client.get_or_create_collection(
        COLLECTION_NAME, metadata={"embed_model": EMBED_MODEL}
    )

    all_chunks = []
    # fetch + chunk each of the 3 source URLs, extend all_chunks

    texts = [c["text"] for c in all_chunks]
    embeddings = model.encode(texts).tolist()

    collection.add(
        documents=texts,
        embeddings=embeddings,
        metadatas=[c["metadata"] for c in all_chunks],
        ids=[f"chunk_{i:04d}" for i in range(len(all_chunks))],
    )
    print(f"Indexed {len(all_chunks)} chunks into ChromaDB")

if __name__ == "__main__":
    build_index()
```

**Important implementation note for Claude Code:** don't guess the HTML
parsing logic for `chunk_by_subsection` blind. Fetch one of the actual URLs
first, inspect the real HTML structure (or fetch and print the raw text to
see how subsection markers appear), and write the extraction logic against
what's actually there. Cornell LII's markup may not be trivially regex-able
on the first try — budget for a couple of iterations here rather than
assuming a one-shot regex will work.

## Part 3: Query + endpoint (`rag/tpr_rag.py`)

```python
import os
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from anthropic import Anthropic

load_dotenv()  # explicit — don't rely on the runner (fastapi dev, etc.) to load .env

EMBED_MODEL = "all-MiniLM-L6-v2"  # must match ingest.py exactly
COLLECTION_NAME = "tpr_regulations"
# Must resolve to the same location ingest.py wrote to.
CHROMA_PATH = Path(os.environ.get("TPR_RAG_DATA_DIR", Path.home() / ".tpr-rag" / "chroma_data"))

_model = SentenceTransformer(EMBED_MODEL)
_client = chromadb.PersistentClient(path=str(CHROMA_PATH))
_collection = _client.get_or_create_collection(COLLECTION_NAME)

_indexed_model = _collection.metadata.get("embed_model") if _collection.metadata else None
if _indexed_model and _indexed_model != EMBED_MODEL:
    raise RuntimeError(
        f"Embedding model mismatch: index was built with {_indexed_model!r}, "
        f"but this code uses {EMBED_MODEL!r}. Re-run ingest.py after aligning "
        "EMBED_MODEL in both files."
    )


def retrieve_relevant_chunks(question: str, k: int = 4) -> list[dict]:
    question_embedding = _model.encode([question]).tolist()
    results = _collection.query(query_embeddings=question_embedding, n_results=k)

    return [
        {"text": doc, "metadata": meta}
        for doc, meta in zip(results["documents"][0], results["metadatas"][0])
    ]


def build_prompt(question: str, chunks: list[dict]) -> str:
    context = "\n\n".join(
        f"[{c['metadata']['source']}({c['metadata']['subsection']}) — {c['metadata']['topic']}]\n{c['text']}"
        for c in chunks
    )
    return f"""You are helping analyze whether a described repair or \
improvement to rental property must be capitalized or can be deducted as \
a current expense, under U.S. tangible property regulations.

Answer using ONLY the regulation excerpts below. Cite the specific \
section and subsection(s) you relied on. If the excerpts don't clearly \
answer the question, say so explicitly rather than guessing.

Regulation excerpts:
{context}

Repair description / question: {question}

Provide:
1. A classification (capitalize / deduct / depends on facts) if the \
excerpts support one
2. Which safe harbor or BAR-test category applies, if any
3. The specific section(s) cited
4. A brief note that this is not tax advice and a CPA should confirm
"""


def answer_repair_question(question: str, k: int = 4) -> dict:
    chunks = retrieve_relevant_chunks(question, k=k)

    if not chunks:
        return {"answer": "No relevant regulation text found for this question.", "sources": []}

    prompt = build_prompt(question, chunks)

    client = Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "answer": msg.content[0].text,
        "sources": [f"{c['metadata']['source']}({c['metadata']['subsection']})" for c in chunks],
    }
```

## Part 4: FastAPI route

```python
from fastapi import APIRouter
from pydantic import BaseModel
from rag.tpr_rag import answer_repair_question

router = APIRouter()

class RepairQuestion(BaseModel):
    description: str

@router.post("/api/v1/repair-tax-impact")
def repair_tax_impact(payload: RepairQuestion):
    return answer_repair_question(payload.description)
```

Example request:
```bash
curl -X POST http://localhost:8000/api/v1/repair-tax-impact \
  -H "Content-Type: application/json" \
  -d '{"description": "Replaced the entire roof on a rental property after storm damage"}'
```

Expected response shape:
```json
{
  "answer": "This is likely a restoration under §1.263(a)-3(k)...",
  "sources": ["1.263(a)-3(k)", "1.263(a)-3(d)"]
}
```

### Comparison endpoint: `POST /api/v1/repair-tax-impact-no-rag`

Added after the RAG vs. no-RAG comparison test (see below) so the
difference is directly demonstrable via the API itself, not just something
shown once in this spec. Takes the same `RepairQuestion` payload, reuses
`llm_providers.complete()` directly (no retrieval, no regulation excerpts,
no "answer using ONLY the excerpts below" instruction) and returns the
same `{"answer", "sources"}` shape for an apples-to-apples diff — `sources`
is always `[]` since nothing was retrieved to cite:

```python
@router.post("/api/v1/repair-tax-impact-no-rag")
def repair_tax_impact_no_rag(payload: RepairQuestion):
    return {"answer": complete(payload.description), "sources": []}
```

## Implementation findings (post-build)

The code in Parts 2/3 above was the original design; a few things changed
once actually built and run against live data:

- **LLM call abstracted, not a direct `Anthropic()` call.** `rag/tpr_rag.py`
  doesn't call Anthropic directly as shown above — it calls `complete()`
  from a new shared `llm_providers.py` module (extracted from
  `metrics_analysis.py`'s existing Groq/Anthropic provider-dispatch
  mechanism), so the RAG endpoint is provider-configurable via
  `LLM_PROVIDER` (default `groq`) exactly like the metrics-analysis
  feature, rather than duplicating a second hardcoded LLM integration.
- **`client.delete_collection()` raises `chromadb.errors.NotFoundError`,
  not `ValueError`**, when the collection doesn't exist yet (confirmed by
  running it — first `ingest.py` run crashed on this). Fixed by catching
  the correct exception type.
- **Some CFR subsections are far larger than a free-tier LLM's rate limit
  allows.** `1.263(a)-3(k)` alone is ~53,000 characters (~13,300 tokens) —
  assembling even one such chunk into a prompt exceeded Groq's free-tier
  12,000 TPM (tokens/minute) cap and the request failed outright
  (`groq.APIStatusError: 413 ... rate_limit_exceeded`, confirmed by
  actually hitting the endpoint). First mitigated in `build_prompt` by
  capping each chunk to `MAX_CHUNK_CHARS = 1200` before assembling the
  prompt (kept as a cheap safety net); the deeper fix is sub-chunking,
  below. ChromaDB still stores, and `sources` still cites, the full text —
  only what's sent to the LLM is capped.

- **One chunk per subsection is far too large to embed — sub-chunking.**
  The original "one chunk per subsection" design produced chunks of
  40k–53k chars for `(h)`/`(j)`/`(k)`. But the embedding model
  (`all-MiniLM-L6-v2`) only reads ~256 tokens (~1000 chars) of input, so
  each of those chunks' embeddings only reflected the subsection's *opening*
  — deep content (nested examples, the routine-maintenance safe harbor
  buried in `(h)`) was invisible to retrieval. Fixed by splitting each
  subsection's body into paragraph-aligned pieces under `SUBCHUNK_CHARS =
  1000` (`_pack` / `_split_long` in `ingest.py`): pack paragraphs up to the
  budget, and split any single paragraph that alone exceeds it at sentence
  boundaries (hard-slicing only a lone over-long sentence). The same packing
  is applied to the IRS FAQ path (its Q&A entries also exceeded the window),
  with continuation pieces prefixed by the question / subsection header so
  each sub-chunk stays self-describing when retrieved in isolation. Result:
  the corpus went from 40 chunks (max 53k chars) to ~300 chunks (max ~1.1k
  chars, **0 over the embedding window**), routine-maintenance content
  became retrievable, and answers now cite precise nested paragraphs
  (e.g. `(k)(1)(vi)`, `(k)(2)`) instead of just each subsection's opening.
  Because several sub-chunks can now share one subsection, `sources` is
  deduped in `answer_repair_question` (preserving retrieval order), and the
  default retrieval `k` was raised `4 → 6` for coverage (still well within
  the rate limit at ~1k chars/chunk).

## Test results: RAG vs. no-RAG comparison

Run against the live endpoint to confirm retrieval-augmentation actually
changes the answer, not just adds citations cosmetically. Question asked
identically in both cases: *"I replaced a window that was broken. can I
write it off this year"*

**Plain LLM call, no RAG** (raw question sent directly to Groq, no
retrieved regulation context, no prompt constraints):
> Whether you can write off the cost of replacing a broken window depends
> on your filing status (homeowner/landlord/business), the type of
> property, and the reason for replacement. Homeowners: casualty loss
> deduction (Schedule A) if due to accident/storm and loss exceeds 10% of
> AGI; normal wear and tear is non-deductible maintenance. Landlords: claim
> as a rental expense (Schedule E). Business owners: business expense
> (Schedule C), possibly depreciated. Keep receipts/records. Consult a tax
> professional.
>
> **Sources cited: none.**

**RAG-augmented** (same question, through
`POST /api/v1/repair-tax-impact`):
> 1. Classification: Depends on facts. 2. Safe harbor or BAR-test
> category: Possibly the routine maintenance safe harbor, but more
> information is needed. 3. Specific section(s) cited: `1.263(a)-3(k)`,
> `IRS FAQ(safe-harbor-for-routine-maintenance)`. 4. This is not tax
> advice, a CPA should confirm. The replacement of a broken window may be
> subject to capitalization as an improvement under `1.263(a)-3(k)` if it
> restores the unit of property or returns it to its ordinarily efficient
> operating condition. However, if the amount meets the criteria for the
> routine maintenance safe harbor, it may be deductible.
>
> **Sources cited:** `1.263(a)-3(k)`,
> `IRS FAQ(safe-harbor-for-routine-maintenance)`, and two more IRS FAQ
> entries.

**Finding:** the plain LLM answer isn't wrong, but it answers a broader,
generic "how does home repair tax deduction work" question — casualty
loss, Schedule A/C/E — using general training-data knowledge, with zero
citations. The RAG answer stays anchored to the actual tangible property
regulations framework this question is specifically about (the BAR test,
routine maintenance safe harbor), with citations traceable to real
ingested source text. Separately confirmed: with an **empty** ChromaDB
index (no ingestion run), the same question returns `"No relevant
regulation text found for this question."` with empty `sources` and never
calls the LLM at all — the honest-failure path works as designed, distinct
from both of the above.

## Verification steps

1. Run `python rag/ingest.py` — confirm it prints a nonzero chunk count and
   `~/.tpr-rag/chroma_data/` (or `$TPR_RAG_DATA_DIR` if set) is created with
   files in it — not anywhere inside the repo
2. (Re)start the FastAPI app **after** ingestion completes — don't run
   ingestion while the app is already serving requests (see the
   Operational rule under Dependencies). Then hit
   `/api/v1/repair-tax-impact` with a few test questions:
   - A clear restoration case (e.g. "replaced entire roof after storm damage")
   - A clear routine maintenance case (e.g. "repainted interior walls")
   - A borderline/ambiguous case (e.g. "replaced 3 of 10 rooftop HVAC units")
   - A question with no good match in the small initial corpus (confirm it
     doesn't hallucinate a confident answer — check the "say so explicitly"
     instruction is actually being followed)
3. Confirm `sources` in the response actually correspond to real subsections
   ingested, not fabricated section numbers
4. Confirm the "not tax advice, consult a CPA" note appears in the answer

## Explicitly out of scope for this pass

- No fine-tuning or custom embedding model — use the off-the-shelf
  `all-MiniLM-L6-v2`
- No expansion beyond the 3 initial source documents in this pass — add
  Rev. Procs / Federal Register preamble in a later iteration once the
  core pipeline is proven
- No conversational memory / multi-turn follow-up — single question, single
  answer per request
- No UI beyond FastAPI's `/docs` — same "browser is Swagger" rule as other
  projects
- No production-grade error handling for malformed ChromaDB state — fine
  to let errors surface as 500s for this pass

## Important framing note

This is a portfolio/learning demonstration of the RAG pattern, not a
production tax-advice tool. The output should always include the CPA
disclaimer, and the answer should be honest about not being definitive when
the retrieved excerpts don't clearly resolve the question — this matters
both for correctness and for how defensible this project is to describe in
an interview (grounded, honest-about-limits RAG, not a tool making
confident tax calls).
