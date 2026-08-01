# eCFR Sources Migration — Design

**Date:** 2026-07-31
**Branch:** `rag/ecfr-sources`
**Status:** Approved, ready for implementation planning

## 1. Problem

`rag/ingest.py` builds the RAG index by scraping Cornell LII HTML — a third-party mirror of the
Code of Federal Regulations — plus an IRS FAQ page, at ingest time.

The CFR is published directly by the government through the **eCFR versioner API**, which serves
structured XML to plain HTTP clients. Sourcing from the authoritative API is the better approach:
the text arrives with an issue date, the markup is structural rather than presentational, and no
third party sits between the regulation and the index.

A comment in `ingest.py` states eCFR "blocks plain scripted requests." That is true of the
**website** (`ecfr.gov/current/...`), which serves a bot-check page. It is **not** true of the
**versioner API** (`ecfr.gov/api/versioner/v1/...`). The original evaluation tested the wrong
endpoint.

Two further problems follow from scraping at ingest time:

1. **Nothing is reproducible or reviewable.** The index is derived from pages that can change
   silently. There is no record of what text produced it, and no way to review what changed
   between ingests.
2. **The chunker is untestable and lossy.** It depends on live network access, so it has no
   tests. Its dependence on Cornell's CSS classes also forced the exclusion of subsection `(i)`
   — the routine maintenance safe harbor — because that page gives `(i)` no distinct top-level
   anchor.

## 2. Approach

Split ingest at the network boundary and commit the source text.

```
PHASE 1 — fetch (network, human-run, occasional)
  rag/fetch_sources.py
    eCFR versioner API ──┐
    irs.gov FAQ page   ──┤
                         ▼
              docs/tpr-sources/          ← committed to git
                _manifest.json
                1.263(a)-1.xml
                1.263(a)-2.xml
                1.263(a)-3.xml
                1.162-4.xml
                irs-faq.html
                         │
PHASE 2 — build index (offline, every build and test)
                         ▼
  rag/ingest.py  → parse → chunk → embed → ChromaDB (derived, never committed)
```

The governing analogy is `uv.lock`: `uv lock` is run deliberately by a human; the build runs
`uv sync --frozen` against the committed result. `docs/tpr-sources/` is the lockfile for
regulation text and `_manifest.json` is its integrity record.

### Properties this buys

- **Hermetic builds.** Docker and CI never reach the network for RAG data.
- **A testable chunker.** The committed XML doubles as test fixtures, so `test_ingest.py`
  becomes possible for the first time.
- **Reviewable refreshes.** A re-import is a pull request showing exactly which regulation
  text changed.

### Contract preserved

The chunk shape is unchanged:

```python
{"text": str, "metadata": {"source", "subsection", "topic", "part"}}
```

Because the metadata shape is preserved, **`rag/tpr_rag.py` and `rag/router.py` require no
edits**, and `_pack` is reused as-is. The blast radius is `ingest.py`, a new
`fetch_sources.py`, new tests, the Dockerfile, `docker-compose.yml`, and docs.

## 3. Scope of sources

Whole sections are ingested. Subsection filtering is removed.

| Source | Included | Rationale |
|---|---|---|
| `1.263(a)-1` Capital expenditures; in general | Yes, full | General rule and de minimis safe harbor |
| `1.263(a)-2` Amounts paid to acquire or produce property | Yes, full | Supports separate-asset questions ("I bought a new appliance") |
| `1.263(a)-3` Amounts paid to improve tangible property | Yes, full | Core BAR test; recovers `(e)`, `(i)`, `(n)` |
| `1.162-4` Repairs | Yes, full | The deduction side, counterpart to capitalization (~2 KB) |
| IRS FAQ page | Yes, whole page | Plain-language election mechanics and procedures |
| `1.168(i)-8` Partial dispositions | **No** | Out of scope for repair-vs-capitalize |
| `1.168(a)-1` MACRS | **No** | Depreciation, not improvement analysis |

The two `1.168` sections are excluded deliberately. Retrieval returns a fixed `k=6` chunks,
so off-topic material competes with relevant material for those slots. More content is not
monotonically better.

Expected index growth: ~300 chunks today to roughly 500–700.

## 4. Source snapshot and manifest

`docs/tpr-sources/_manifest.json` records where each file came from and how to verify it,
carrying:

- `source`, `api_base`, `endpoint_pattern`; per-file `url` and `web_view`
- `pulled_on`, `ecfr_issue_date`, `ecfr_up_to_date_as_of`
- per-file `bytes`, `sha256`, `citation`, `title`, `covers`
- `integrity_note` and `verify_command`
- `reimport_notes` capturing the operational gotchas

### Two things the manifest must state explicitly

1. **These files are build inputs, not reference data.** `ingest.py` parses them to produce the
   index that ships in the image and answers user queries, so a refresh propagates automatically
   to retrieval results with no human in the loop. A snapshot folder that nothing imports would
   be safe to refresh freely; this one is not, and the description must say so rather than imply
   otherwise. Concretely, a re-pull can change chunk boundaries, drop a subsection if eCFR
   restructures its markup, or — if a fetch silently returns an error page — yield an empty index
   that still returns HTTP 200 with no sources.

2. **The IRS FAQ is labelled as weaker provenance.** Its entry carries `source: "IRS"`, its own
   `pulled_on`, and an explicit note that the page is unversioned — no issue date, no API.
   This is stated plainly rather than glossed over.

### Re-import workflow

```
uv run python -m rag.fetch_sources    # network; rewrites XML/HTML + manifest
git diff docs/tpr-sources/            # review what changed in the law
uv run python -m rag.ingest           # rebuild index
uv run pytest                         # integrity + chunker assertions
git commit                            # PR with a readable diff
```

Because refreshing can change behavior, the workflow includes an explicit verification step:
rebuild the index, confirm chunk counts are within expected range, and spot-check that a known
question still retrieves the expected subsections.

## 5. Fetch phase — `rag/fetch_sources.py`

The only module permitted to touch the network.

1. `GET /api/versioner/v1/titles.json`; read `latest_issue_date` for Title 26. Arbitrary dates
   return HTTP 404, so the date must be discovered, not guessed.
2. For each section: `GET /api/versioner/v1/full/{issue_date}/title-26.xml?part=1&section={section}`,
   writing response bytes straight to `docs/tpr-sources/{section}.xml`. Use a raw byte write, not
   a parse-and-reserialize, so the sha256 describes exactly what the API returned.
3. `GET` the IRS FAQ page; write `docs/tpr-sources/irs-faq.html`.
4. Compute sha256 per file; write `_manifest.json`.

**Failure handling: loud and atomic.** Any non-200 response, unexpected content type, or a body
containing a bot-check page aborts the entire run without writing partial files or a
half-updated manifest. Leaving the last-good snapshot intact is strictly better than a partial
replacement.

## 6. Index build phase — the XML chunker

**Parser: stdlib `xml.etree.ElementTree`.** No new dependency. The document is flat enough that
XPath buys nothing. Mixed content flattens correctly via `"".join(elem.itertext())`:
`<P>(a) <I>Overview.</I> This section…` becomes `"(a) Overview. This section…"`, and the italic-digit
form `(<I>1</I>)` flattens to `(1)`. BeautifulSoup remains, used only for the IRS FAQ's HTML.

### Observed structure of eCFR section XML

- One `<DIV8 N="1.263(a)-3" TYPE="SECTION">` wrapper; `<HEAD>` carries the section title.
- Content is **structurally flat** — 165 sibling `<P>` elements in `1.263(a)-3`. Subsection
  hierarchy is encoded in paragraph *text*, not in nesting.
- `<EXAMPLE>` blocks (117 in `1.263(a)-3`), each `<HED>` + `<PSPACE>`, are siblings of `<P>`.
  These are worked repair-vs-capitalize examples and are valuable retrieval content.
- Top-level subsections are conventionally written as `(x) <I>Topic.</I>`.

### Algorithm

1. Locate `<DIV8 TYPE="SECTION">`; read `<HEAD>` for the title.
2. Walk direct children in document order (`<P>`, `<EXAMPLE>`).
3. Flatten each element to text; identify top-level subsection boundaries (rule below).
4. Accumulate each subsection's body until the next boundary. Attach `<EXAMPLE>` blocks to
   whichever subsection is currently open.
5. Pass each body through the existing `_pack` for embedding-window sizing, preserving the
   `[(x) Topic (continued)]` prefix convention on continuation pieces.

### Boundary detection — the `(i)` problem

`(i)` is ambiguous: it is both subsection `(i)` (routine maintenance safe harbor) and the roman
numeral *one* used in nested markers such as `(e)(2)(i)`. This ambiguity is what forced `(i)`
out of the Cornell-based index.

Measured against the real `1.263(a)-3` XML, **neither signal alone is sufficient**:

- *Sequence alone fails.* The first `(i)` after `(h)` is a nested paragraph beginning
  "2 percent of the unadjusted basis," not the subsection.
- *Italic title alone fails.* `(v) Leased building` and `(i) Routine maintenance for buildings`
  both carry italic titles but are nested.

**Rule: accept a paragraph as a top-level boundary only if its leading marker is the next
expected letter in sequence AND it is followed by an italic topic title.**

Verified trace against the real document:

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

Run to completion this accepts exactly 18 subsections, `(a)` through `(r)`, each once, and
rejects all six nested `(i)` paragraphs and all three stray `(v)` paragraphs.

`topic` is extracted from the first `<I>` immediately following the marker, matching the role
`span.et03` played in the Cornell parser so metadata stays consistent.

### Removed code

`SOURCES[...]["subsections"]`, `_top_level_markers`, and `chunk_cfr_page`'s CSS-class heuristics
are all deleted. The new parser is a net reduction in complexity.

### IRS FAQ

`chunk_irs_faq` keeps its BeautifulSoup logic verbatim. It reads the committed HTML file instead
of an `httpx` response.

## 7. Lazy imports

`chromadb`, `chromadb.errors.NotFoundError`, and `sentence_transformers.SentenceTransformer` are
currently imported at module level in `ingest.py` but used only inside `build_index()`. Move them
inside that function.

This follows the pattern `CLAUDE.md` documents for `llm_providers.py` ("adapters import their SDK
lazily inside the function, not at module top level"). The payoff is that `test_ingest.py` can
import the chunking functions without loading torch, so — unlike `tests/test_tpr_rag.py` — it needs
no module-level skip guard for a cold Hugging Face cache.

## 8. Tests

### `tests/test_ingest.py` (new)

Hermetic, using the committed XML as fixtures:

- The parser finds exactly subsections `(a)`–`(r)` in `1.263(a)-3`, once each.
- `(i)` resolves to "Safe harbor for routine maintenance on property", **not** "2 percent of the
  unadjusted basis". This is the regression test for the boundary rule.
- Stray `(v)` and nested `(i)` paragraphs remain inside their parent's body.
- `topic` extraction, `EXAMPLE` attachment, `_pack` size limits, continuation-prefix format.
- Every source yields a non-empty chunk list — the guard against a silently empty index, which
  matters because `get_or_create_collection` in `tpr_rag.py` creates an empty collection rather
  than raising when the index is missing.

Per repo conventions: `@pytest.mark.parametrize` with explicit `id=` for the repeated per-section
shape; `monkeypatch` rather than `unittest.mock`.

### `tests/test_manifest.py` (new)

Recompute each file's sha256 and compare against `_manifest.json`. Runs in milliseconds with no
network, executing automatically under pre-commit and CI.

## 9. Build integration

### Dockerfile

Added after the embedding-model bake, since ingest needs the embedder:

```dockerfile
COPY --chown=appuser:appuser docs/tpr-sources/ ./docs/tpr-sources/
RUN uv run python -m rag.ingest
```

### docker-compose.yml

The bind mount of `${HOME}/.tpr-rag/chroma_data` over `/home/appuser/.tpr-rag/chroma_data` is
**removed**. With the index baked into the image, that mount would shadow it with the host's copy.

Removing it also retires the documented workaround about running ingest on the host to avoid
root-owned files, and the uid-1000 alignment rationale that existed to support it. A gotcha is
eliminated rather than relocated.

### Local development (non-Docker)

`uv run python -m rag.ingest` is still run once, but is now offline and fast — reading five
committed files instead of scraping three pages.

## 10. Documentation changes

| File | Change |
|---|---|
| `docs/tpr_rag_spec.md` | Correct the "eCFR blocks scripted requests" claim; replace the Cornell HTML-structure section with the XML structure and the `(i)` boundary rule; record the sourcing rationale (authoritative API over scraped mirror) |
| `CLAUDE.md` | Add the fetch command; note the index is built at image-build time; drop the bind-mount guidance |
| `README.md` | Simplify the RAG note — Docker users no longer pre-run ingest; re-verify the example output since retrieval will shift |

## 11. Risks

1. **Retrieval results will change.** Chunk counts roughly double and new subsections enter the
   index. The README's example output must be re-verified rather than assumed still accurate.
2. **The boundary rule is verified against `1.263(a)-3` only.** The other three sections may use
   slightly different conventions; `(o) Treatment of capital expenditures.` already shows a
   trailing-period title style rather than em-dash continuation. Running the parser against all
   four XML fixtures is the first implementation step that will confirm or break the rule.
3. **eCFR markup could change on a future refresh.** Mitigated by the parser asserting an
   expected subsection sequence and failing loudly rather than silently dropping content.
4. **The IRS FAQ remains unversioned.** It has no issue date and no API, so its provenance is
   inherently weaker than the XML. Labelled honestly in the manifest rather than papered over.

## 12. Out of scope

- **AWS infrastructure.** ECR, VPC, EC2, and CI image push are a separate sub-project on their
  own branch and spec. This work unblocks the clean version of it by making the image build
  hermetic, but ships independently.
- **Changing the embedding model or chunk-size strategy.** `_pack` and `EMBED_MODEL` are unchanged.
- **`1.168(i)-8` and `1.168(a)-1`.** Excluded per Section 3.
