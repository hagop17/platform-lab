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
(`uv run python -m rag.ingest` — it must be run as a module, not as a bare
script path, since it does `from rag.fetch_sources import ...`), which does
**not** automatically load `.env` the way `uv run fastapi dev` does. Both
`rag/ingest.py` and `rag/tpr_rag.py`
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
GROQ_API_KEY=...                        # required — default LLM_PROVIDER
ANTHROPIC_API_KEY=sk-ant-...            # only if LLM_PROVIDER=anthropic
TPR_RAG_DATA_DIR=/path/to/chroma_data   # optional, see below
```
These follow the same `.env` + gitignore pattern as other projects — never
commit a key. (This section reflects the original design's Anthropic-only
plan; see "Implementation findings" below for why the LLM call ended up
provider-configurable via `llm_providers.py` instead.)

`TPR_RAG_DATA_DIR` controls where ChromaDB persists its index. It's
intentionally kept **outside the repo/source tree** so the data survives a
`git clean`, a repo re-clone, or deleting `rag/` entirely, and so it never
risks getting committed. Default (when unset): `~/.tpr-rag/chroma_data` — a
fixed location under the user's home directory, independent of the repo
path or whatever directory a script happens to be run from.

---

## Part 1: Source documents

Primary sources (public domain — U.S. federal regulations, no copyright
restriction on reproducing full text). As of the eCFR sources migration
(see "Source selection" below), these are pulled from the eCFR versioner
API and the IRS FAQ page by `rag/fetch_sources.py`, committed verbatim
under `docs/tpr-sources/` with a sha256 manifest, and parsed offline by
`rag/ingest.py` — see
`docs/superpowers/specs/2026-07-31-ecfr-sources-design.md` for the full
migration design:

- **Treas. Reg. §1.263(a)-1** — general capitalization rule, de minimis
  safe harbor — `docs/tpr-sources/1.263(a)-1.xml` (eCFR versioner API)
- **Treas. Reg. §1.263(a)-2** — amounts paid to acquire or produce
  tangible property; supports separate-asset questions —
  `docs/tpr-sources/1.263(a)-2.xml`
- **Treas. Reg. §1.263(a)-3** — the core BAR test: definitions (d),
  determining the unit of property (e), the routine maintenance safe
  harbor (i), betterments (j), restorations (k), adaptations (l),
  small-taxpayer safe harbor (h) — `docs/tpr-sources/1.263(a)-3.xml`.
  (`(i)` was excluded from the old Cornell-HTML-based index because that
  page gave it no distinct top-level anchor; the eCFR XML chunker's
  sequence+italic boundary rule recovers it — see "eCFR XML structure and
  the `(i)` boundary rule" below.)
- **Treas. Reg. §1.162-4** — repairs, the deduction-side counterpart to
  capitalization — `docs/tpr-sources/1.162-4.xml`
- **IRS FAQ** (plain-English cross-reference, useful for casually-phrased
  questions) — `docs/tpr-sources/irs-faq.html`

`1.168(i)-8` (partial dispositions) and `1.168(a)-1` (MACRS) are
deliberately excluded: retrieval returns a fixed `k=6` chunks, so
off-topic depreciation material would compete with repair-vs-capitalize
text for those slots.

### Source selection: eCFR versioner API, not Cornell LII

**Original finding (kept here uncorrected, then corrected below — the
mistake stays visible rather than getting quietly deleted):** The official
U.S. government CFR mirror, **eCFR.gov**, was considered as a source for
the two regulation-text documents instead of Cornell LII, since it's the
authoritative primary source rather than a third-party mirror. It was
rejected: eCFR.gov (which shares infrastructure with federalregister.gov)
actively blocks plain scripted HTTP requests — a `curl`/`httpx` GET against
it returns a "Federal Register :: Request Access" bot-check page, not the
regulation text, confirmed by directly fetching
`https://www.ecfr.gov/current/title-26/part-1/section-1.263(a)-3`. Since
`fetch_regulation_text()` uses a plain `httpx.get()` with no headless
browser or JS challenge-solving, eCFR.gov is not fetchable by this
ingestion pipeline. Cornell LII was directly confirmed fetchable (a plain
`httpx`/`curl` GET against `1.263(a)-3` returns real content, ~954 KB) and
requires no such workaround, so it remains the source for both regulation
texts.

**Correction (eCFR sources migration, 2026-07-31): this tested the wrong
endpoint.** The URL fetched above, `https://www.ecfr.gov/current/...`, is
the eCFR **website**. It does serve a bot-check page to plain scripted
clients, exactly as described — that observation was correct. But the
eCFR **versioner API**, `https://www.ecfr.gov/api/versioner/v1/...`, is a
completely different surface: it serves structured XML straight to a
plain `httpx` GET, no headers or JS challenge-solving required (confirmed
directly by `rag/fetch_sources.py`, which now pulls all four CFR sections
through it — see `docs/superpowers/specs/2026-07-31-ecfr-sources-design.md`
§5). The original evaluation never tried the API; it only ever tested the
website, under the mistaken belief that a bot-check on `ecfr.gov/current/`
implied the same for `ecfr.gov/api/`. The authoritative-API-over-
third-party-mirror argument that motivated considering eCFR.gov in the
first place was right all along — only the fetchability test was wrong.

Cornell LII is no longer used as a source. Source text now comes from the
eCFR versioner API (CFR sections) and the IRS FAQ page, fetched by the
one network-touching module `rag/fetch_sources.py` and committed verbatim
under `docs/tpr-sources/`, sha256-pinned by `_manifest.json` — the
`uv.lock` analogy for regulation text: a deliberate, human-run,
reviewable refresh, not a live fetch on every build. `rag/ingest.py`
then builds the index entirely offline from those committed files, and in
Docker the index is built directly into the image at `docker build` time
(`rag/ingest.py` runs during the build against the committed snapshot)
rather than being populated via a host bind mount at container start —
see `Dockerfile` and `docker-compose.yml`, and `CLAUDE.md` → Architecture
and Gotchas for the operational details.

### eCFR XML structure and the `(i)` boundary rule

Fetched and inspected directly (`rag/fetch_sources.py` against the eCFR
versioner API — see "Source selection" above) rather than guessed. Each
section comes back as one self-contained XML document, the root element
**being** the section itself rather than wrapped in something else:

```xml
<DIV8 N="1.263(a)-3" TYPE="SECTION" hierarchy_metadata="...">
  <HEAD>&#167; 1.263(a)-3 Amounts paid to improve tangible property.</HEAD>
  <P>(a) <I>Overview.</I> This section provides rules...</P>
  ...
  <P>(k) <I>Restorations</I>—(1) In general. ...</P>
  <EXAMPLE>
    <HED>Example.</HED>
    <PSPACE>Railroad rolling stock X is a railroad...</PSPACE>
  </EXAMPLE>
  ...
  <CITA>[T.D. 9636, 78 FR 57718, Sept. 19, 2013, as amended by ...]</CITA>
</DIV8>
```

`<HEAD>` carries the section title. The body is **structurally flat**:
`1.263(a)-3` is 141 sibling `<P>` elements (plus 117 `<EXAMPLE>` elements)
directly under `<DIV8>` — subsection hierarchy (`(a)`, `(a)(1)`,
`(a)(1)(i)`, ...) is encoded entirely in each paragraph's *text*, not in
XML nesting. There is no `<P>` inside another `<P>`. Boundaries therefore
have to be *detected* by inspecting paragraph text, not found by walking a
tree — the opposite of the old Cornell HTML, whose nested `psection-2`
paragraphs sat directly under their parent `psection-1`.

**`<EXAMPLE>` flattening trap.** Each `<EXAMPLE>` wraps a `<HED>` (heading,
e.g. `"Example."`) and a `<PSPACE>` (body) as two separate children with no
whitespace between them in the source. Flattening the whole element with a
single `"".join(elem.itertext())` therefore fuses the heading's last word
straight onto the body's first word — `"...rolling stockX is a
railroad..."` instead of `"...rolling stock X is a railroad..."`. The
chunker's `_flatten` (`rag/ingest.py`) special-cases `EXAMPLE`: it
flattens `<HED>` and `<PSPACE>` independently and joins them with an
explicit space. `<P>`'s own mixed content still flattens correctly with
plain `itertext()` (e.g. `<P>(a) <I>Overview.</I> This section…` becomes
`"(a) Overview. This section…"` with no fusion, since real whitespace
already separates the marker, the italic run, and the trailing text).

**`<CITA>` exclusion.** The trailing `<CITA>` element is the authority
citation line (e.g. `[T.D. 9636, 78 FR 57718, Sept. 19, 2013, ...]`) —
publication metadata, not regulation text. It is not in the chunker's
`_CONTENT_TAGS` (only `P` and `EXAMPLE` are), so it is skipped entirely
rather than being swallowed into the final subsection's body as trailing
noise.

**The `(i)` boundary rule.** `(i)` is ambiguous in the source text: it is
both subsection `(i)` — the routine maintenance safe harbor — and the
roman numeral *one* used in nested markers like `(e)(2)(i)`. This same
ambiguity is what forced `(i)` out of the old Cornell-HTML-based index —
Cornell's `id="i"` attribute collided between the genuine top-level
subsection and an unrelated nested list item, so `(i)` had no stable
top-level anchor there and its content fell silently inside the `(h)`
chunk instead.

Measured against the real `1.263(a)-3` XML, **neither available signal is
sufficient alone**:

- *Sequence alone fails.* The first `(i)` after `(h)` is a nested
  paragraph beginning "2 percent of the unadjusted basis," not the
  subsection.
- *Italic title alone fails.* `(v) Leased building` and `(i) Routine
  maintenance for buildings` both carry italic topic titles but are
  nested paragraphs, not subsections.

**Rule: accept a paragraph as a top-level boundary only if its leading
marker is the next expected letter in sequence AND it is immediately
followed by an italic topic title** (`_subsection_start` in
`rag/ingest.py`). The italic check is structural, not textual —
`elem.text` (the string *before* the first child) must strip to exactly
`"(x)"` and the first child must be `<I>` — rather than a regex over
flattened text, which would also match a paragraph whose italics happen
to appear somewhere in the middle.

Verified trace against the real document (from
`docs/superpowers/specs/2026-07-31-ecfr-sources-design.md` §6):

| Marker | Italic title | Expecting | Decision |
|---|---|---|---|
| `(e)` Determining the unit of property | yes | e | accept, expect `f` |
| `(v)` Leased building | yes | f | reject — wrong letter |
| `(f)`…`(h)` | yes | f…h | accept, expect `i` |
| `(i)` 2 percent of the unadjusted basis | **no** | i | reject — no title |
| `(i)` **Safe harbor for routine maintenance on property** | yes | i | **accept**, expect `j` |
| `(i)` Routine maintenance for buildings | yes | j | reject — wrong letter |
| `(i)` Amounts paid for a betterment | no | j | reject |
| `(v)` Amounts paid to return a unit of property | no | j | reject |
| `(j)` Capitalization of betterments | yes | j | accept, expect `k` |

Run to completion, the rule accepts exactly 18 subsections — `(a)` through
`(r)` — each once, and rejects all six nested `(i)` paragraphs and all
three stray `(v)` paragraphs in `1.263(a)-3`. `(i)` — the routine
maintenance safe harbor, entirely absent from the old Cornell-based index
— is now recovered as its own citable subsection, `1.263(a)-3(i)`. The
rule was validated against **all four** ingested sections
(`1.263(a)-1`, `1.263(a)-2`, `1.263(a)-3`, `1.162-4`), not just
`1.263(a)-3`:
`tests/test_ingest.py::test_parser_finds_each_top_level_subsection_exactly_once`
runs it against every fixture and asserts the full expected letter range
for each.

`topic` is extracted from the first `<I>` immediately following the
marker, with the trailing period stripped (`"Overview."` → `"Overview"`),
matching the role `span.et03` played in the old Cornell parser so the
metadata shape stays consistent.

### IRS FAQ structure (unchanged by this migration)

The IRS FAQ page uses a completely different structure — a single
`<article>` container (confirmed to be the only one on the page, cleanly
separating real content from surrounding nav chrome) with each question
as an `<h2>`/`<h3>`/`<h4>` heading followed by one or more `<p>` answer
paragraphs until the next heading. It needs its own chunking function
(`chunk_irs_faq`), not the CFR XML chunker. `rag/fetch_sources.py` now
fetches the page HTML and commits it to `docs/tpr-sources/irs-faq.html`;
`chunk_irs_faq`'s BeautifulSoup logic is otherwise verbatim unchanged —
it just reads the committed file instead of a live response.

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

- **The eCFR migration changed the corpus size and shape.** Note the ~300-chunk
  figure above describes the *Cornell-era* index. Moving to committed eCFR XML
  took it to **467 chunks**: `1.263(a)-1` 49, `1.263(a)-2` 47, `1.263(a)-3` 317,
  `1.162-4` 3, IRS FAQ 51. Two CFR sections are new to the corpus, and
  `1.263(a)-3` now yields all 18 subsections `(a)`–`(r)` rather than the five
  the Cornell parser could anchor — recovering `(i)`, the routine-maintenance
  safe harbor, which had been absent entirely.

- **Finer chunking lets a single subsection dominate `k=6`.** Measured against
  the rebuilt index across four questions: *"I replaced the entire roof on a
  rental property"* returns **1** distinct source (all six chunks land in `(k)`
  "Capitalization of restorations"), while *"$400 appliance, can I deduct it?"*
  returns **6** (spanning three IRS FAQ entries, `1.263(a)-1(a)`, and
  `1.162-4(a)`), *"routine HVAC maintenance"* returns **4** (including the
  recovered `(i)`), and *"repainted and patched drywall"* returns **2**. So
  concentration is question-specific rather than systemic — a whole-roof
  replacement genuinely *is* squarely a `(k)` restoration, and the answer still
  cites precise nested paragraphs (`(k)(1)(vi)`, `(k)(2)`, `(e)(2)(ii)`). Worth
  knowing when choosing README/demo examples: the roof question is a weaker
  showcase post-migration than it was before, because a one-element `sources`
  array understates the grounding. The HVAC question demonstrates more.

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

1. Run `uv run python -m rag.ingest` — confirm it prints a nonzero chunk count and
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
