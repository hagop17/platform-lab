# eCFR Sources Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace live Cornell LII HTML scraping in `rag/ingest.py` with a committed snapshot of authoritative eCFR versioner-API XML, so the RAG index builds offline, hermetically, from reviewable source text.

**Architecture:** Split ingest at the network boundary. A new human-run `rag/fetch_sources.py` is the only module allowed to touch the network — it pulls four CFR sections as XML plus the IRS FAQ HTML into `docs/tpr-sources/`, with a `_manifest.json` integrity record, all committed to git (the `uv.lock` analogy). `rag/ingest.py` becomes offline: it parses those committed files with stdlib `xml.etree.ElementTree`, chunks them, and embeds into ChromaDB. The chunk dict shape is unchanged, so `rag/tpr_rag.py` and `rag/router.py` need no edits.

**Tech Stack:** Python 3.12, `uv`, `httpx` (fetch only), stdlib `xml.etree.ElementTree` (CFR XML), BeautifulSoup (IRS FAQ HTML only), `sentence-transformers` + `chromadb` (index build), pytest.

**Source spec:** [docs/superpowers/specs/2026-07-31-ecfr-sources-design.md](../specs/2026-07-31-ecfr-sources-design.md)

## Global Constraints

- **Branch:** `rag/ecfr-sources`. No pushing — this container has read-only GitHub access; pushes happen from the host.
- **Commit messages get NO `Co-Authored-By` trailer** and stay short (one subject line, body only when it earns its place).
- **Package manager is `uv`.** Never `pip` or `poetry`. Run things as `uv run ...`.
- **No new dependencies.** The CFR parser uses stdlib `xml.etree.ElementTree`. BeautifulSoup stays, used only for the IRS FAQ.
- **Tests must never touch the network** or invoke real LLM/ChromaDB/embedding calls. Fixtures are the committed files in `docs/tpr-sources/`.
- **Test conventions:** `monkeypatch` (pytest builtin), never `unittest.mock`. `@pytest.mark.parametrize` with explicit `id=` when cases share one call-and-assert shape and differ only in data; separate `test_*` functions when the behavior under test genuinely differs.
- **Before every commit, all of these must pass:** `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest`. Pre-commit runs them anyway; run them yourself first so the hook doesn't rewrite files under you.
- **Ruff:** line-length 100, `select = ["E", "F", "I", "S"]`. `S` is flake8-bandit — `assert` is allowed in `tests/**` only, and any `subprocess`/`hashlib` usage must not trip a rule. `hashlib.sha256` is fine (S324 only flags md5/sha1).
- **The chunk contract is frozen:** `{"text": str, "metadata": {"source": str, "subsection": str, "topic": str, "part": int}}`. Do not add, rename, or drop metadata keys — `rag/tpr_rag.py` reads all four.
- **`_pack`, `_split_long`, `SUBCHUNK_CHARS`, `EMBED_MODEL`, `COLLECTION_NAME`, `CHROMA_PATH` are unchanged.** Reuse them as-is.
- **Execution environment: the sandboxed devcontainer.** Two consequences bind every task:
  - **There is no Docker daemon in the container** (no socket is mounted, by design — the
    container's job is confining an autonomous agent, and image builds belong in CI). Any step
    needing `docker` is a **host handoff**: write the change, report it unverified with the exact
    commands, and let the human run it. Do **not** try to install or reach a daemon.
  - **The network is default-deny.** `www.ecfr.gov` and `www.irs.gov` are allowlisted (for
    `rag/fetch_sources.py` only); `pypi.org` and `api.groq.com` are allowlisted. Anything else
    fails at the network layer, not the application layer — surface it as a blocker rather than
    working around it.

## Pre-flight facts (verified live on 2026-07-31, do not re-derive)

These were confirmed by hitting the real API while writing this plan. They are stated here so the implementer does not have to guess:

| Fact | Value |
|---|---|
| Title 26 `latest_issue_date` | `2026-07-24` |
| Title 26 `up_to_date_as_of` | `2026-07-30` |
| `titles.json` shape | `{"titles": [{"number": 26, "latest_issue_date": ..., "up_to_date_as_of": ...}, ...], "meta": {...}}` |
| Section endpoint | `GET /api/versioner/v1/full/{issue_date}/title-26.xml?part=1&section={section}` → `200 text/xml` |
| Response root element | `<DIV8 N="1.263(a)-3" TYPE="SECTION" hierarchy_metadata="...">` — the root *is* the DIV8, not a wrapper |
| Direct children of DIV8 | `HEAD` (1), `P`, `EXAMPLE`, `CITA` (1, last) |
| Child counts | `1.263(a)-1`: 63 P / 17 EXAMPLE · `1.263(a)-2`: 42 P / 26 EXAMPLE · `1.263(a)-3`: 141 P / 117 EXAMPLE · `1.162-4`: 5 P / 0 EXAMPLE |
| Subsections the boundary rule accepts | `1.263(a)-1`: (a)–(h) · `1.263(a)-2`: (a)–(j) · `1.263(a)-3`: (a)–(r) · `1.162-4`: (a)–(c) |
| `1.263(a)-3` subsection `(i)` topic | **"Safe harbor for routine maintenance on property"** — the rule correctly rejects the six nested `(i)` paragraphs |
| Approximate chunk yield (CFR only, `_pack` at 1000 chars) | 49 + 47 + 318 + 3 = **417 chunks** |
| IRS FAQ | `200 text/html`, ~154 KB, exactly one `<article>` element — `chunk_irs_faq` works on it unmodified |

**Two corrections to the spec, already validated — implement these, not the spec's version:**

1. **The italic-title check must be structural, not a regex on flattened text.** The reliable test is: the element's *own* `.text` (the string before its first child) strips to exactly `"(x)"`, **and** its first child element is `<I>`. Matching a regex against `"".join(elem.itertext())` cannot distinguish `(i) <I>Routine maintenance for buildings</I>` from a nested paragraph whose italic appears later.

2. **`"".join(elem.itertext())` is WRONG for `<EXAMPLE>`.** It concatenates `<HED>` and `<PSPACE>` with no separator, producing `"Example. Railroad rolling stockX is a railroad that..."` — the last word of the heading fuses to the first word of the body. `EXAMPLE` must be flattened by joining each child's flattened text with a space. `<P>` still uses `itertext()` directly (its mixed content, e.g. `<P>(a) <I>Overview.</I> This section…`, flattens correctly).

3. **`<CITA>` must be skipped.** It is the trailing authority line (`[T.D. 9636, 78 FR 57718, ...]`) and would otherwise be swallowed into the final subsection's body.

---

## Task 1: Fetch phase — `rag/fetch_sources.py` and the committed snapshot

Produces the source-of-truth files every later task depends on. This is the only task that uses the network.

**Files:**
- Create: `rag/fetch_sources.py`
- Create (by running it): `docs/tpr-sources/_manifest.json`, `docs/tpr-sources/1.263(a)-1.xml`, `docs/tpr-sources/1.263(a)-2.xml`, `docs/tpr-sources/1.263(a)-3.xml`, `docs/tpr-sources/1.162-4.xml`, `docs/tpr-sources/irs-faq.html`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `rag/fetch_sources.py::SOURCES_DIR: Path` — absolute path to `docs/tpr-sources/`, resolved from `__file__` so it works from any cwd.
  - `rag/fetch_sources.py::MANIFEST_PATH: Path`
  - `rag/fetch_sources.py::CFR_SECTIONS: dict[str, dict[str, str]]` — keyed by section label (`"1.263(a)-3"`), each value having `title` and `covers`.
  - `rag/fetch_sources.py::IRS_FAQ_URL: str`, `IRS_FAQ_FILENAME: str = "irs-faq.html"`
  - `rag/fetch_sources.py::FetchError(RuntimeError)`
  - `rag/fetch_sources.py::main() -> None`
  - Manifest JSON schema (Task 1 Step 1 shows it in full) — `tests/test_manifest.py` and the docs depend on the exact key names `files`, `bytes`, `sha256`.

- [ ] **Step 1: Write `rag/fetch_sources.py`**

Create the file with exactly this content:

```python
"""Fetch phase for the tangible-property RAG corpus.

The ONLY module in this repo permitted to reach the network for RAG data.
A human runs it occasionally; it rewrites docs/tpr-sources/ and the manifest,
which are committed to git. rag/ingest.py then builds the index offline from
those committed files. The governing analogy is uv.lock: `uv lock` is a
deliberate human act, the build runs `uv sync --frozen` against the result.

Usage:
    uv run python -m rag.fetch_sources
"""

import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx

API_BASE = "https://www.ecfr.gov/api/versioner/v1"
TITLE = 26
PART = "1"

# docs/tpr-sources/ lives in the repo (unlike the ChromaDB index, which is
# derived and stays outside it) — these files ARE the reviewable source text.
SOURCES_DIR = Path(__file__).resolve().parent.parent / "docs" / "tpr-sources"
MANIFEST_PATH = SOURCES_DIR / "_manifest.json"

# Whole sections, no subsection filtering. 1.168(i)-8 and 1.168(a)-1 are
# deliberately excluded: retrieval returns a fixed k=6, so depreciation and
# partial-disposition text would compete with repair-vs-capitalize text for
# those slots. More corpus is not monotonically better.
CFR_SECTIONS = {
    "1.263(a)-1": {
        "title": "Capital expenditures; in general",
        "covers": "General rule for capital expenditures and the de minimis safe harbor",
    },
    "1.263(a)-2": {
        "title": "Amounts paid to acquire or produce tangible property",
        "covers": "Acquisition/production costs — supports separate-asset questions",
    },
    "1.263(a)-3": {
        "title": "Amounts paid to improve tangible property",
        "covers": "Core betterment/adaptation/restoration test, unit of property, "
        "routine maintenance safe harbor",
    },
    "1.162-4": {
        "title": "Repairs",
        "covers": "The deduction side — counterpart to capitalization",
    },
}

IRS_FAQ_URL = (
    "https://www.irs.gov/businesses/small-businesses-self-employed/"
    "tangible-property-final-regulations"
)
IRS_FAQ_FILENAME = "irs-faq.html"

TIMEOUT = 60.0
# The versioner API serves plain clients fine, but the ecfr.gov *website* does
# not — send a normal UA so a future redirect to a bot-checked host is at least
# no worse off, and so irs.gov doesn't 403 us.
HEADERS = {"User-Agent": "Mozilla/5.0 (platform-lab rag/fetch_sources.py)"}

# If a CDN interstitial ever replaces a real response, it arrives as HTTP 200
# with plausible headers. Fail on the body, not just the status line.
BOT_CHECK_MARKERS = (
    b"Just a moment",
    b"cf-browser-verification",
    b"challenge-platform",
    b"Enable JavaScript and cookies to continue",
    b"captcha",
)


class FetchError(RuntimeError):
    """A source could not be retrieved, or came back looking wrong.

    Raised before anything is written to disk: a partial refresh is strictly
    worse than keeping the last-good snapshot intact.
    """


def _check(resp: httpx.Response, *, expect_content_type: str, must_contain: bytes) -> bytes:
    """Validate one response and return its raw bytes.

    Raw bytes, deliberately — not a parse-and-reserialize — so the recorded
    sha256 describes exactly what the API returned.
    """
    if resp.status_code != 200:
        raise FetchError(f"{resp.request.url}: expected HTTP 200, got {resp.status_code}")

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if content_type != expect_content_type:
        raise FetchError(
            f"{resp.request.url}: expected content-type {expect_content_type!r}, "
            f"got {content_type!r} — likely an error or interstitial page"
        )

    body = resp.content
    for marker in BOT_CHECK_MARKERS:
        if marker.lower() in body.lower():
            raise FetchError(
                f"{resp.request.url}: response body contains bot-check marker "
                f"{marker!r} — refusing to save an interstitial page as source text"
            )

    if must_contain not in body:
        raise FetchError(
            f"{resp.request.url}: response body is missing expected marker "
            f"{must_contain!r} — the endpoint's format may have changed"
        )

    return body


def fetch_title_dates(client: httpx.Client) -> tuple[str, str]:
    """Return (latest_issue_date, up_to_date_as_of) for Title 26.

    The issue date must be discovered, not guessed: the /full/ endpoint 404s on
    any date that isn't an actual issue date.
    """
    resp = client.get(f"{API_BASE}/titles.json")
    _check(resp, expect_content_type="application/json", must_contain=b"latest_issue_date")
    titles = resp.json()["titles"]
    for entry in titles:
        if entry["number"] == TITLE:
            return entry["latest_issue_date"], entry["up_to_date_as_of"]
    raise FetchError(f"Title {TITLE} not present in {API_BASE}/titles.json")


def section_url(issue_date: str, section: str) -> str:
    """The request URL, recorded in the manifest so a human can re-fetch by hand.

    Section labels contain parentheses ("1.263(a)-3"), so quote them.
    """
    return (
        f"{API_BASE}/full/{issue_date}/title-{TITLE}.xml"
        f"?part={PART}&section={quote(section, safe='')}"
    )


def web_view_url(section: str) -> str:
    return f"https://www.ecfr.gov/current/title-{TITLE}/section-{section}"


def fetch_section(client: httpx.Client, issue_date: str, section: str) -> bytes:
    resp = client.get(
        f"{API_BASE}/full/{issue_date}/title-{TITLE}.xml",
        params={"part": PART, "section": section},
    )
    return _check(resp, expect_content_type="text/xml", must_contain=b'TYPE="SECTION"')


def fetch_irs_faq(client: httpx.Client) -> bytes:
    resp = client.get(IRS_FAQ_URL)
    return _check(resp, expect_content_type="text/html", must_contain=b"<article")


def main() -> None:
    pulled_on = date.today().isoformat()

    # Fetch everything into memory and validate it all BEFORE touching disk.
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        issue_date, up_to_date_as_of = fetch_title_dates(client)
        print(f"Title {TITLE} latest_issue_date={issue_date} up_to_date_as_of={up_to_date_as_of}")

        payloads: dict[str, bytes] = {}
        for section in CFR_SECTIONS:
            payloads[f"{section}.xml"] = fetch_section(client, issue_date, section)
            print(f"  fetched {section} ({len(payloads[f'{section}.xml']):,} bytes)")

        payloads[IRS_FAQ_FILENAME] = fetch_irs_faq(client)
        print(f"  fetched IRS FAQ ({len(payloads[IRS_FAQ_FILENAME]):,} bytes)")

    files: dict[str, dict] = {}
    for section, meta in CFR_SECTIONS.items():
        filename = f"{section}.xml"
        files[filename] = {
            "source": "eCFR",
            "citation": f"26 CFR {section}",
            "title": meta["title"],
            "covers": meta["covers"],
            "url": section_url(issue_date, section),
            "web_view": web_view_url(section),
            "bytes": len(payloads[filename]),
            "sha256": hashlib.sha256(payloads[filename]).hexdigest(),
        }
    files[IRS_FAQ_FILENAME] = {
        "source": "IRS",
        "citation": "IRS, Tangible Property Final Regulations (FAQ)",
        "title": "Tangible Property Final Regulations",
        "covers": "Plain-language election mechanics and procedures",
        "url": IRS_FAQ_URL,
        "web_view": IRS_FAQ_URL,
        "bytes": len(payloads[IRS_FAQ_FILENAME]),
        "sha256": hashlib.sha256(payloads[IRS_FAQ_FILENAME]).hexdigest(),
        "provenance_note": (
            "Weaker provenance than the XML: this page is unversioned. It has no "
            "issue date and no API, so 'pulled_on' is the only thing pinning it in "
            "time and its content can change silently between pulls."
        ),
    }

    manifest = {
        "description": (
            "Committed source text for the tangible-property RAG index. These files "
            "are BUILD INPUTS, not reference data: rag/ingest.py parses them to "
            "produce the ChromaDB index that ships in the Docker image and answers "
            "user queries. A refresh therefore propagates to retrieval results with "
            "no human in the loop."
        ),
        "integrity_note": (
            "Refreshing is not free. A re-pull can shift chunk boundaries, drop a "
            "subsection if eCFR restructures its markup, or — if a fetch silently "
            "returns an error page — yield an empty index that still answers HTTP 200 "
            "with no sources. Always review `git diff docs/tpr-sources/`, rebuild the "
            "index, and run the tests before committing a refresh."
        ),
        "verify_command": "uv run pytest tests/test_manifest.py",
        "generated_by": "uv run python -m rag.fetch_sources",
        "pulled_on": pulled_on,
        "ecfr": {
            "source": "eCFR — Office of the Federal Register / GPO",
            "api_base": API_BASE,
            "endpoint_pattern": "/full/{issue_date}/title-26.xml?part=1&section={section}",
            "ecfr_issue_date": issue_date,
            "ecfr_up_to_date_as_of": up_to_date_as_of,
        },
        "files": files,
        "reimport_notes": [
            "The issue date must be discovered from /titles.json, not guessed — the "
            "/full/ endpoint returns HTTP 404 for any date that is not a real issue date.",
            "The versioner API (ecfr.gov/api/versioner/v1/...) serves plain scripted "
            "clients. The ecfr.gov WEBSITE (ecfr.gov/current/...) returns a bot-check "
            "page. An earlier evaluation tested the website and wrongly concluded eCFR "
            "was unscriptable.",
            "Response bytes are written verbatim, never parsed and reserialized, so each "
            "sha256 describes exactly what the API returned.",
            "The fetch is atomic: every source is retrieved and validated in memory "
            "before any file is written, so a failure leaves the last-good snapshot intact.",
            "1.168(i)-8 (partial dispositions) and 1.168(a)-1 (MACRS) are excluded on "
            "purpose. Retrieval returns a fixed k=6 chunks; off-topic material would "
            "compete for those slots.",
        ],
    }

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for filename, body in payloads.items():
        (SOURCES_DIR / filename).write_bytes(body)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    print(f"Wrote {len(payloads)} source files + manifest to {SOURCES_DIR}")


if __name__ == "__main__":
    main()
```

Note on `section_url`: it exists only to record a human-reproducible URL string in the manifest; the actual request uses `params=` so httpx handles the encoding of `1.263(a)-3`.

- [ ] **Step 2: Run the fetch**

Run: `uv run python -m rag.fetch_sources`

Expected output (dates may be newer than the plan's):

```
Title 26 latest_issue_date=2026-07-24 up_to_date_as_of=2026-07-30
  fetched 1.263(a)-1 (...bytes)
  fetched 1.263(a)-2 (...bytes)
  fetched 1.263(a)-3 (245,744 bytes)
  fetched 1.162-4 (1,958 bytes)
  fetched IRS FAQ (~153,804 bytes)
Wrote 5 source files + manifest to /app/docs/tpr-sources
```

If this fails with `FetchError`, do **not** work around it by loosening `_check`. Report the failure — a bad snapshot is worse than no snapshot.

- [ ] **Step 3: Sanity-check the snapshot by hand**

Run:

```bash
ls -la docs/tpr-sources/
uv run python -c "
import xml.etree.ElementTree as ET
for s in ['1.263(a)-1','1.263(a)-2','1.263(a)-3','1.162-4']:
    r = ET.parse(f'docs/tpr-sources/{s}.xml').getroot()
    kids = {}
    for k in r: kids[k.tag] = kids.get(k.tag, 0) + 1
    print(s, r.tag, r.get('TYPE'), kids)
"
```

Expected: each root is `DIV8 SECTION`; child tag counts approximately match the pre-flight table (`1.263(a)-3` → `{'HEAD': 1, 'P': 141, 'EXAMPLE': 117, 'CITA': 1}`). Exact counts may drift if the regulation was amended; the shape must not.

- [ ] **Step 4: Write the manifest integrity test**

Create `tests/test_manifest.py`:

```python
"""The committed source snapshot must match its manifest.

Cheap (milliseconds, no network) and runs under pre-commit and CI, so a
truncated file or a hand-edited XML can't reach the index unnoticed.
"""

import hashlib
import json

import pytest

from rag.fetch_sources import MANIFEST_PATH, SOURCES_DIR

_MANIFEST = json.loads(MANIFEST_PATH.read_text())
_FILES = _MANIFEST["files"]


@pytest.mark.parametrize("filename", sorted(_FILES), ids=sorted(_FILES))
def test_source_file_matches_manifest(filename: str):
    recorded = _FILES[filename]
    body = (SOURCES_DIR / filename).read_bytes()

    assert len(body) == recorded["bytes"]
    assert hashlib.sha256(body).hexdigest() == recorded["sha256"]


def test_manifest_covers_every_committed_source_file():
    """Guards the other direction: a file added to the directory without being
    re-manifested would otherwise be silently ingested with no provenance."""
    on_disk = {p.name for p in SOURCES_DIR.iterdir() if p.name != MANIFEST_PATH.name}

    assert on_disk == set(_FILES)


def test_manifest_records_the_ecfr_issue_date():
    assert _MANIFEST["ecfr"]["ecfr_issue_date"]
    assert _MANIFEST["pulled_on"]


def test_irs_faq_is_labelled_as_weaker_provenance():
    """The FAQ has no issue date and no API. That's stated plainly, not glossed."""
    faq = _FILES["irs-faq.html"]

    assert faq["source"] == "IRS"
    assert "unversioned" in faq["provenance_note"]
```

- [ ] **Step 5: Run the manifest test**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: PASS — 8 tests (5 parametrized + 3).

- [ ] **Step 6: Lint, format, typecheck**

Run: `uv run ruff check . && uv run ruff format . && uv run pyright`
Expected: all clean. If ruff reformats `fetch_sources.py`, that's fine — keep the reformatted version.

- [ ] **Step 7: Commit**

```bash
git add rag/fetch_sources.py tests/test_manifest.py docs/tpr-sources/
git commit -m "Add eCFR fetch phase and committed source snapshot"
```

---

## Task 2: The XML chunker

TDD against the committed XML from Task 1. Nothing else changes yet — `build_index()` still runs the old Cornell path until Task 3, so the suite stays green throughout.

**Files:**
- Modify: `rag/ingest.py` (add new functions; delete nothing yet)
- Test: `tests/test_ingest.py` (create)

**Interfaces:**
- Consumes: `docs/tpr-sources/*.xml` from Task 1; `_pack(paragraphs: list[str], limit: int = SUBCHUNK_CHARS) -> list[str]` and `SUBCHUNK_CHARS = 1000` from the existing `rag/ingest.py`.
- Produces, in `rag/ingest.py`:
  - `_flatten(elem: Element) -> str`
  - `_subsection_start(elem: Element, expected_letter: str) -> tuple[str, str] | None` — returns `(letter, topic)` or `None`.
  - `chunk_cfr_xml(xml_bytes: bytes, source_label: str) -> list[dict]` — the Task 3 replacement for `chunk_cfr_page`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest.py`:

```python
"""Chunker tests, hermetic by construction.

The committed eCFR XML in docs/tpr-sources/ doubles as the fixture corpus —
that is the whole point of committing it. No network, no ChromaDB, no
embedding model: chromadb and sentence_transformers are imported lazily
inside build_index(), so importing rag.ingest here is cheap and needs none of
the cold-HF-cache skip guard that tests/test_tpr_rag.py carries.
"""

import string

import pytest

from rag.ingest import SUBCHUNK_CHARS, chunk_cfr_xml
from rag.fetch_sources import SOURCES_DIR


def _chunks(section: str) -> list[dict]:
    return chunk_cfr_xml((SOURCES_DIR / f"{section}.xml").read_bytes(), section)


def _subsections(chunks: list[dict]) -> list[str]:
    """Distinct subsection letters, in first-appearance order."""
    seen: list[str] = []
    for chunk in chunks:
        letter = chunk["metadata"]["subsection"]
        if letter not in seen:
            seen.append(letter)
    return seen


# Verified against the real 2026-07-24 issue of each section. If eCFR amends a
# section these may legitimately move — but they must never SHRINK silently,
# which is the failure mode this suite exists to catch.
@pytest.mark.parametrize(
    ("section", "last_letter"),
    [
        ("1.263(a)-1", "h"),
        ("1.263(a)-2", "j"),
        ("1.263(a)-3", "r"),
        ("1.162-4", "c"),
    ],
    ids=["1.263(a)-1", "1.263(a)-2", "1.263(a)-3", "1.162-4"],
)
def test_parser_finds_each_top_level_subsection_exactly_once(section: str, last_letter: str):
    letters = _subsections(_chunks(section))
    expected = list(string.ascii_lowercase[: string.ascii_lowercase.index(last_letter) + 1])

    assert letters == expected


def test_routine_maintenance_safe_harbor_is_recovered_as_subsection_i():
    """The regression test for the boundary rule.

    (i) is ambiguous: it is both subsection (i) and the roman numeral *one* in
    nested markers like (e)(2)(i). Sequence alone accepts the wrong one — the
    first (i) after (h) is a nested paragraph opening "2 percent of the
    unadjusted basis". Requiring an italic topic title as well is what resolves
    it. This subsection was missing entirely from the old Cornell-based index.
    """
    topics = {c["metadata"]["subsection"]: c["metadata"]["topic"] for c in _chunks("1.263(a)-3")}

    assert topics["i"] == "Safe harbor for routine maintenance on property"
    assert "2 percent of the unadjusted basis" not in topics["i"]


def test_nested_i_and_stray_v_paragraphs_stay_inside_their_parent_body():
    """Nested markers must not open a new subsection, and must not be dropped:
    they belong to whichever subsection is currently open."""
    chunks = _chunks("1.263(a)-3")

    assert "v" not in _subsections(chunks)

    h_body = " ".join(c["text"] for c in chunks if c["metadata"]["subsection"] == "h")
    assert "2 percent of the unadjusted basis" in h_body


def test_topic_is_taken_from_the_italic_title_without_trailing_period():
    topics = {c["metadata"]["subsection"]: c["metadata"]["topic"] for c in _chunks("1.263(a)-3")}

    assert topics["a"] == "Overview"
    assert topics["e"] == "Determining the unit of property"


def test_example_blocks_are_attached_to_the_open_subsection_and_word_separated():
    """<EXAMPLE> wraps <HED> + <PSPACE>. Joining with itertext() alone fuses
    them ("rolling stockX is a railroad"), so each child is flattened
    separately and joined with a space."""
    body = " ".join(c["text"] for c in _chunks("1.263(a)-3"))

    assert "Example. Railroad rolling stock X is a railroad" in body
    assert "rolling stockX" not in body


def test_authority_citation_is_not_ingested():
    """The trailing <CITA> line is publication metadata, not regulation text."""
    body = " ".join(c["text"] for c in _chunks("1.263(a)-3"))

    assert "78 FR 57718" not in body


@pytest.mark.parametrize(
    "section",
    ["1.263(a)-1", "1.263(a)-2", "1.263(a)-3", "1.162-4"],
    ids=["1.263(a)-1", "1.263(a)-2", "1.263(a)-3", "1.162-4"],
)
def test_every_source_yields_a_non_empty_chunk_list(section: str):
    """The guard against a silently empty index. tpr_rag.py calls
    get_or_create_collection, which happily creates an EMPTY collection rather
    than raising, so an empty ingest surfaces as a cheerful HTTP 200 with no
    sources rather than as an error."""
    assert _chunks(section)


@pytest.mark.parametrize(
    "section",
    ["1.263(a)-1", "1.263(a)-2", "1.263(a)-3", "1.162-4"],
    ids=["1.263(a)-1", "1.263(a)-2", "1.263(a)-3", "1.162-4"],
)
def test_no_chunk_materially_exceeds_the_embedding_window(section: str):
    """all-MiniLM-L6-v2 reads ~256 tokens (~1000 chars). A chunk far past that
    would embed only its opening. The continuation prefix is the only allowed
    overhead."""
    for chunk in _chunks(section):
        assert len(chunk["text"]) <= SUBCHUNK_CHARS + 200


def test_continuation_pieces_carry_a_self_describing_header_prefix():
    """A retrieved sub-chunk is read in isolation, so pieces after the first
    must say which subsection they came from."""
    parts = [c for c in _chunks("1.263(a)-3") if c["metadata"]["subsection"] == "k"]

    assert parts[0]["metadata"]["part"] == 0
    assert not parts[0]["text"].startswith("[")
    assert parts[1]["metadata"]["part"] == 1
    assert parts[1]["text"].startswith("[(k) Restorations (continued)]\n")


def test_chunk_metadata_shape_is_unchanged():
    """rag/tpr_rag.py reads all four keys; changing this shape breaks retrieval
    citations without breaking any import."""
    chunk = _chunks("1.162-4")[0]

    assert set(chunk) == {"text", "metadata"}
    assert set(chunk["metadata"]) == {"source", "subsection", "topic", "part"}
    assert chunk["metadata"]["source"] == "1.162-4"
```

One value in this file is not yet verified: `topics["k"]` is asserted as `Restorations` in the continuation-prefix test. Confirm it in Step 3 and correct the literal if the real italic title differs (e.g. `Capitalization of restorations`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: collection error — `ImportError: cannot import name 'chunk_cfr_xml' from 'rag.ingest'`.

- [ ] **Step 3: Confirm the two unverified literals**

Run:

```bash
uv run python -c "
import re, string, xml.etree.ElementTree as ET
d = ET.parse('docs/tpr-sources/1.263(a)-3.xml').getroot()
i = 0
for el in d:
    if el.tag != 'P': continue
    kids = list(el)
    if not kids or kids[0].tag != 'I': continue
    if (el.text or '').strip() != f'({string.ascii_lowercase[i]})': continue
    print(string.ascii_lowercase[i], '|', ''.join(kids[0].itertext()).strip())
    i += 1
"
```

Expected: 18 lines, `a`–`r`. Read off the real titles for `a`, `e`, `i`, and `k`, and fix any literal in `tests/test_ingest.py` that doesn't match (topics are asserted with the trailing period stripped).

- [ ] **Step 4: Implement the chunker**

In `rag/ingest.py`, add `import string` and `import xml.etree.ElementTree as ET` to the imports (ruff's `I` rule will sort them; stdlib block, alphabetical: `import os`, `import re`, `import string`, `import xml.etree.ElementTree as ET`, then `from pathlib import Path`).

Add these functions immediately **after** `_pack` and **before** `chunk_irs_faq`:

```python
def _flatten(elem: ET.Element) -> str:
    """Collapse an element's mixed content to one whitespace-normalized string.

    <P>(a) <I>Overview.</I> This section…  ->  "(a) Overview. This section…"
    and the italic-digit form (<I>1</I>) -> "(1)".

    <EXAMPLE> is the exception: it wraps <HED> + <PSPACE> as separate children
    with no whitespace between them, so a flat itertext() fuses the heading's
    last word to the body's first ("...rolling stockX is a railroad..."). Join
    its children individually instead.
    """
    if elem.tag == "EXAMPLE":
        return " ".join(part for part in (_flatten(child) for child in elem) if part)
    return " ".join("".join(elem.itertext()).split())


# A top-level subsection opens with the marker as the paragraph's own leading
# text — "(a)", "(b)", ... — with nothing before the italic title element.
_TOP_LEVEL_MARKER = re.compile(r"^\(([a-z])\)$")

# The eCFR section XML is structurally FLAT: 1.263(a)-3 is 141 sibling <P>
# elements under one <DIV8>, with subsection hierarchy encoded in paragraph
# *text*, not in nesting. So boundaries have to be detected, not traversed.
# Only <P> can open a subsection; <EXAMPLE> is body content (117 of them in
# 1.263(a)-3 — worked repair-vs-capitalize scenarios, valuable to retrieve).
# <HEAD> is the section title and <CITA> the trailing authority line; both are
# publication metadata, not regulation text.
_CONTENT_TAGS = ("P", "EXAMPLE")


def _subsection_start(elem: ET.Element, expected_letter: str) -> tuple[str, str] | None:
    """Return (letter, topic) if this element opens `expected_letter`, else None.

    "(i)" is ambiguous — it is both subsection (i), the routine maintenance
    safe harbor, and the roman numeral *one* in nested markers like (e)(2)(i).
    Measured against the real 1.263(a)-3, neither available signal is
    sufficient alone:

      - Sequence alone fails: the first "(i)" after "(h)" is a nested paragraph
        opening "2 percent of the unadjusted basis", not the subsection.
      - Italic title alone fails: "(v) Leased building" and "(i) Routine
        maintenance for buildings" both carry italic titles but are nested.

    So require BOTH: the marker is the next expected letter in sequence, AND it
    is immediately followed by an italic topic title. Run to completion this
    accepts exactly (a)-(r) in 1.263(a)-3, once each, and rejects all six
    nested "(i)" paragraphs and all three stray "(v)" paragraphs.

    The italic check is structural, not textual: `elem.text` is the string
    *before* the first child, so requiring it to be exactly "(x)" and the first
    child to be <I> pins the title to the position right after the marker. A
    regex over the flattened text would also match a paragraph whose italics
    appear somewhere in the middle.
    """
    if elem.tag != "P":
        return None

    children = list(elem)
    if not children or children[0].tag != "I":
        return None

    match = _TOP_LEVEL_MARKER.match((elem.text or "").strip())
    if match is None or match.group(1) != expected_letter:
        return None

    # Trailing period is a typesetting convention ("Overview."); strip it so
    # `topic` reads the same as the Cornell-era metadata did.
    topic = " ".join("".join(children[0].itertext()).split()).rstrip(".")
    return match.group(1), topic


def chunk_cfr_xml(xml_bytes: bytes, source_label: str) -> list[dict]:
    """Chunk one eCFR section XML file into embedding-window-sized pieces.

    Walks the <DIV8 TYPE="SECTION"> children in document order, opening a new
    subsection at each accepted boundary and accumulating everything else —
    including <EXAMPLE> blocks — into whichever subsection is currently open.
    Each body then goes through _pack for embedding-window sizing.
    """
    root = ET.fromstring(xml_bytes)  # noqa: S314 - committed, sha256-pinned local file
    section = root if root.tag == "DIV8" else root.find(".//DIV8[@TYPE='SECTION']")
    if section is None:
        raise ValueError(
            f"{source_label}: no <DIV8 TYPE='SECTION'> element found. The eCFR "
            "response format has changed, or this file is not a section export."
        )

    subsections: list[dict] = []
    current: dict | None = None
    for elem in section:
        if elem.tag not in _CONTENT_TAGS:
            continue

        expected = (
            string.ascii_lowercase[len(subsections)] if len(subsections) < 26 else None
        )
        start = _subsection_start(elem, expected) if expected else None
        if start is not None:
            letter, topic = start
            current = {"letter": letter, "topic": topic, "body": [_flatten(elem)]}
            subsections.append(current)
            continue

        if current is not None:  # text before (a), if any, has no home — skip it
            current["body"].append(_flatten(elem))

    if not subsections:
        # Fail loudly rather than contribute 0 chunks: an empty index still
        # answers HTTP 200 (get_or_create_collection creates rather than raises),
        # so silence here would surface as confidently sourceless answers.
        raise ValueError(
            f"{source_label}: found 0 top-level subsections. The boundary rule no "
            "longer matches this document's markup — inspect the XML before "
            "rebuilding the index."
        )

    chunks = []
    for sub in subsections:
        header = f"({sub['letter']}) {sub['topic']}".strip()
        for part_idx, sub_text in enumerate(_pack(sub["body"])):
            # Prefix continuation pieces with the subsection header so each
            # sub-chunk is self-describing when retrieved in isolation (the
            # first piece already opens with the header text).
            text = sub_text if part_idx == 0 else f"[{header} (continued)]\n{sub_text}"
            chunks.append(
                {
                    "text": text,
                    "metadata": {
                        "source": source_label,
                        "subsection": sub["letter"],
                        "topic": sub["topic"],
                        "part": part_idx,
                    },
                }
            )

    return chunks
```

The `# noqa: S314` is required: ruff's flake8-bandit rules flag `xml.etree` as vulnerable to malicious XML. These files are committed, sha256-pinned, and government-issued — not attacker-controlled. Keep the inline justification so the suppression is auditable.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: PASS, all tests.

If `test_parser_finds_each_top_level_subsection_exactly_once` fails on a section other than `1.263(a)-3`, that is **Risk 2 from the spec materializing** — the boundary rule was verified against `1.263(a)-3` in detail. Do not weaken the rule to make the test pass. Print the section's real markers using the Step 3 command, find out which convention differs, and report it before changing anything.

- [ ] **Step 6: Lint, format, typecheck, full suite**

Run: `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest`
Expected: all clean. The full suite still passes because `build_index()` hasn't changed yet.

- [ ] **Step 7: Commit**

```bash
git add rag/ingest.py tests/test_ingest.py
git commit -m "Add eCFR XML chunker with sequence+italic boundary rule"
```

---

## Task 3: Rewire `build_index()` offline and delete the Cornell path

**Files:**
- Modify: `rag/ingest.py` (imports, `SOURCES`, delete `fetch_page` / `_top_level_markers` / `chunk_cfr_page`, rewrite `build_index`, adapt `chunk_irs_faq`'s caller)
- Test: `tests/test_ingest.py` (add the IRS FAQ case)

**Interfaces:**
- Consumes: `chunk_cfr_xml` (Task 2); `SOURCES_DIR`, `CFR_SECTIONS`, `IRS_FAQ_FILENAME` (Task 1).
- Produces: `rag/ingest.py::build_index() -> None`, now fully offline. `chunk_irs_faq(html: str, source_label: str = "IRS FAQ") -> list[dict]` keeps its signature; only its caller changes.

- [ ] **Step 1: Add the failing IRS FAQ test**

Append to `tests/test_ingest.py`:

```python
def test_irs_faq_chunks_from_the_committed_html_file():
    """chunk_irs_faq's BeautifulSoup logic is unchanged — it just reads the
    committed file now instead of an httpx response body."""
    from rag.fetch_sources import IRS_FAQ_FILENAME
    from rag.ingest import chunk_irs_faq

    chunks = chunk_irs_faq((SOURCES_DIR / IRS_FAQ_FILENAME).read_text(encoding="utf-8"))

    assert chunks
    assert all(c["metadata"]["source"] == "IRS FAQ" for c in chunks)
    assert all(len(c["text"]) <= SUBCHUNK_CHARS + 200 for c in chunks)


def test_ingest_does_not_import_chromadb_or_the_embedder_at_module_level():
    """chromadb and sentence_transformers must be imported lazily inside
    build_index(). That's what lets this file import rag.ingest without the
    cold-HF-cache skip guard that tests/test_tpr_rag.py needs — keep it that way.

    Asserted against rag.ingest's own namespace, deliberately, NOT against
    sys.modules: tests/test_tpr_rag.py imports rag.tpr_rag, which loads
    SentenceTransformer (and therefore torch) at import time. A sys.modules
    assertion would pass or fail purely on test collection order — green by
    accident in a default run, red the moment someone runs the two files in
    the other order.
    """
    import rag.ingest

    assert not hasattr(rag.ingest, "SentenceTransformer")
    assert not hasattr(rag.ingest, "chromadb")
    assert not hasattr(rag.ingest, "NotFoundError")
    assert not hasattr(rag.ingest, "httpx")  # the network moved to fetch_sources.py
```

- [ ] **Step 2: Run to verify the lazy-import test fails**

Run: `uv run pytest tests/test_ingest.py::test_importing_ingest_does_not_load_torch_or_chromadb -v`
Expected: FAIL — `assert 'torch' not in sys.modules`, because `rag/ingest.py` still imports `sentence_transformers` at module level.

(`test_irs_faq_chunks_from_the_committed_html_file` should already pass — `chunk_irs_faq` is unchanged and the committed HTML is a valid input. That's the point: it's a characterization test protecting a behavior we're deliberately preserving.)

- [ ] **Step 3: Move the heavy imports inside `build_index()`**

In `rag/ingest.py`, delete these three module-level imports:

```python
import chromadb
from chromadb.errors import NotFoundError
from sentence_transformers import SentenceTransformer
```

and add them as the first lines inside `build_index()`:

```python
def build_index():
    # Imported lazily, matching the pattern llm_providers.py uses for provider
    # SDKs: it keeps tests/test_ingest.py able to import the chunking functions
    # without pulling in torch, so — unlike tests/test_tpr_rag.py — it needs no
    # module-level skip guard for a cold Hugging Face cache.
    import chromadb
    from chromadb.errors import NotFoundError
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBED_MODEL)
    ...
```

- [ ] **Step 4: Delete the Cornell scraping path**

From `rag/ingest.py` delete, in full:

- `import httpx` (no longer used anywhere in this module — the network moved to `fetch_sources.py`)
- the `SOURCES` dict and the `IRS_FAQ_URL` constant, including the comment block above `SOURCES` about eCFR blocking scripted requests (that claim is wrong; it described the website, not the versioner API)
- `fetch_page`
- `_top_level_markers` (docstring included)
- `chunk_cfr_page`

Keep: `EMBED_MODEL`, `COLLECTION_NAME`, `CHROMA_PATH`, `SUBCHUNK_CHARS`, `_SENTENCE_BOUNDARY`, `_split_long`, `_pack`, the Task 2 functions, `chunk_irs_faq`.

Add near the top, after the `CHROMA_PATH` definition:

```python
from rag.fetch_sources import CFR_SECTIONS, IRS_FAQ_FILENAME, SOURCES_DIR
```

(place it with the other imports at the top of the file; ruff's `I` rule puts first-party imports in their own block after third-party).

Note `from bs4 import BeautifulSoup` stays — `chunk_irs_faq` still uses it.

- [ ] **Step 5: Rewrite the `build_index()` source loop**

Replace the body between `collection = client.get_or_create_collection(...)` and the `if not all_chunks:` guard with:

```python
    all_chunks = []
    for source_label in CFR_SECTIONS:
        xml_bytes = (SOURCES_DIR / f"{source_label}.xml").read_bytes()
        found = chunk_cfr_xml(xml_bytes, source_label)
        subs = sorted({c["metadata"]["subsection"] for c in found})
        print(f"{source_label}: {len(found)} chunks across subsections {subs}")
        all_chunks.extend(found)

    faq_html = (SOURCES_DIR / IRS_FAQ_FILENAME).read_text(encoding="utf-8")
    faq_chunks = chunk_irs_faq(faq_html)
    print(f"IRS FAQ: {len(faq_chunks)} chunks")
    all_chunks.extend(faq_chunks)
```

and update the `RuntimeError` message in the existing empty-index guard to match the new reality:

```python
    if not all_chunks:
        raise RuntimeError(
            "Ingestion produced 0 chunks across all sources — parsing likely "
            "broke. The sources are committed under docs/tpr-sources/, so this "
            "means the chunker regressed, not that a fetch failed. Fix the "
            "chunker before re-running; refusing to build an empty index."
        )
```

Also update the module docstring/comment at the top of `build_index` that says the corpus is "3 source docs, a couple dozen chunks" — it is now 5 source files and roughly 500 chunks. A full rebuild is still near-instant, so the delete-and-recreate rationale stands; just correct the numbers.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: PASS, including `test_importing_ingest_does_not_load_torch_or_chromadb`.

- [ ] **Step 7: Build the real index and verify it end to end**

Run:

```bash
uv run python -m rag.ingest
```

Expected, approximately (exact counts depend on the current issue of each section):

```
1.263(a)-1: ~49 chunks across subsections ['a','b','c','d','e','f','g','h']
1.263(a)-2: ~47 chunks across subsections ['a',...,'j']
1.263(a)-3: ~318 chunks across subsections ['a',...,'r']
1.162-4: ~3 chunks across subsections ['a','b','c']
IRS FAQ: N chunks
Indexed ~500 chunks into ChromaDB at /home/.../.tpr-rag/chroma_data
```

The spec predicted 500–700 total. If the CFR portion lands far outside ~417, or any section's subsection list is short, stop and investigate before proceeding.

Then spot-check that retrieval actually improved — the routine maintenance safe harbor `(i)` was absent from the old index entirely:

```bash
uv run python -c "
from rag.tpr_rag import retrieve_relevant_chunks
for c in retrieve_relevant_chunks('Do I have to capitalize routine HVAC maintenance I perform every few years?'):
    print(c['metadata']['source'], c['metadata']['subsection'], '|', c['metadata']['topic'])
"
```

Expected: `1.263(a)-3 i | Safe harbor for routine maintenance on property` appears among the six results. Record the actual output — Task 5 needs it to re-verify the README example.

- [ ] **Step 8: Lint, format, typecheck, full suite**

Run: `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest`
Expected: all clean. `tests/test_tpr_rag.py` should now run rather than skip (the HF cache is warm after Step 7).

- [ ] **Step 9: Commit**

```bash
git add rag/ingest.py tests/test_ingest.py
git commit -m "Build RAG index from committed eCFR XML instead of scraping"
```

---

## Task 4: Build integration — Dockerfile and docker-compose

Bakes the index into the image and removes the host bind mount that would otherwise shadow it.

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `rag/ingest.py` from Task 3, `docs/tpr-sources/` from Task 1.
- Produces: an image whose `/home/appuser/.tpr-rag/chroma_data` is populated at build time; no runtime dependency on the host or the network for RAG data.

- [ ] **Step 1: Add the source copy and ingest step to the Dockerfile**

In `Dockerfile`, immediately **after** the embedding-model bake (`RUN uv run python -c "from sentence_transformers import ..."`) and **before** `EXPOSE`, add:

```dockerfile
# Build the RAG index into the image. Placed after the model bake because
# ingest needs the embedder, and after the code copy because it imports rag/.
# The source text is committed (docs/tpr-sources/, sha256-pinned by its
# manifest), so this step is fully offline — no scraping at build time and no
# network dependency at container start. TPR_RAG_DATA_DIR must match what
# docker-compose.yml sets at runtime, or the app would open an empty collection.
ENV TPR_RAG_DATA_DIR=/home/appuser/.tpr-rag/chroma_data
COPY --chown=appuser:appuser docs/tpr-sources/ ./docs/tpr-sources/
RUN uv run python -m rag.ingest
```

Setting `ENV TPR_RAG_DATA_DIR` in the image (rather than relying on compose alone) is what guarantees the build-time write and the runtime read land in the same place; compose still sets the same value explicitly, harmlessly.

- [ ] **Step 2: Remove the bind mount from docker-compose.yml**

In `docker-compose.yml`, delete the entire `volumes:` block from the `app` service — both the comment and the `${HOME}/.tpr-rag/chroma_data:/home/appuser/.tpr-rag/chroma_data` line. The index now lives in the image; mounting the host directory over it would shadow the baked index with whatever (possibly empty, possibly stale) copy the host has.

Leave `TPR_RAG_DATA_DIR=/home/appuser/.tpr-rag/chroma_data` in `environment:` — it must keep matching the Dockerfile.

- [ ] **Step 3: Update the stale uid-1000 comment in the Dockerfile**

The comment above `RUN useradd --create-home --uid 1000 ...` currently justifies the fixed UID by bind-mount ownership ("so it lines up with the host user that typically owns the bind-mounted RAG data (see docker-compose.yml)"). That mount is gone. Replace the second half with:

```dockerfile
# Non-root: a container compromise (e.g. an RCE in a dependency) then only
# has this user's privileges, not root's. Fixed UID 1000 — the common
# first-non-root-user convention — so the image behaves predictably if
# anything is ever bind-mounted into it.
```

- [ ] **Step 4: Static self-check (what you CAN verify in-container)**

There is no Docker daemon here — do not attempt `docker build`. Verify what's checkable
statically instead, and be honest in the report about what that does and doesn't prove:

```bash
# The two TPR_RAG_DATA_DIR values must be byte-identical, or the image writes the
# index to one path and the app reads another — an empty collection, HTTP 200, no sources.
grep -n 'TPR_RAG_DATA_DIR' Dockerfile docker-compose.yml
# The app service must have no volumes: block left.
grep -n -A3 'volumes:' docker-compose.yml
# The ingest RUN must come after the model bake and after `COPY rag/`.
grep -n -E 'COPY|RUN|ENV|EXPOSE|CMD' Dockerfile
```

Expected: identical `/home/appuser/.tpr-rag/chroma_data` on both sides; the only `volumes:`
remaining belongs to the `prometheus` service; `RUN uv run python -m rag.ingest` appears after
both `COPY ... rag/` and the `SentenceTransformer` bake line.

Also confirm compose still parses — this needs no daemon:

```bash
docker compose config >/dev/null && echo "compose file valid"
```

If the `docker` CLI is absent entirely, say so in the report and skip this sub-step; the greps
above are the substantive check.

- [ ] **Step 5: Hand the build verification to the human**

Report status `DONE_WITH_CONCERNS` with `build unverified — no Docker daemon in the devcontainer`,
and put these commands in the report verbatim for the human to run on the host:

```bash
docker compose build app
```

Expected: the `uv run python -m rag.ingest` layer prints the same per-section chunk counts seen
in Task 3 Step 7 and ends with `Indexed ~500 chunks`.

```bash
docker compose run --rm --no-deps app uv run python -c "
from rag.tpr_rag import retrieve_relevant_chunks
chunks = retrieve_relevant_chunks('Is replacing an entire roof a restoration?')
print(len(chunks))
for c in chunks: print(c['metadata']['source'], c['metadata']['subsection'])
"
```

Expected: `6` and six real citations. A `0` means the baked index isn't being found — the
`TPR_RAG_DATA_DIR` values are out of sync, or the compose `volumes:` removal was incomplete.

Task 4 is the plan's most integration-heavy change and its failure modes (path mismatch, layer
ordering, shadowed index) are invisible in a diff and obvious in a build. So this verification is
required — but it is **deferred, not blocking**.

**Controller: do not stall the run waiting for it.** Park the verification in the ledger and
continue to Task 5:

```
Task 4: parked — docker build unverified (no daemon in devcontainer) — ruling: code changes
        reviewed and committed; human runs `docker compose build app` + the retrieval check
        before merge. Blocks merge, not Task 5.
```

Task 5 depends on Task 4's *code* changes (which are complete), not on its build. Point the final
whole-branch review at this parked entry so it surfaces as an outstanding pre-merge item rather
than being silently dropped.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "Bake RAG index into the image, drop the chroma bind mount"
```

---

## Task 5: Documentation

**Files:**
- Modify: `docs/tpr_rag_spec.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the real retrieval output recorded in Task 3 Step 7 and Task 4 Step 4.
- Produces: no code interface.

- [ ] **Step 1: Read the current spec before editing**

Run: `uv run python -c "print(open('docs/tpr_rag_spec.md').read())"` — or just read the file. Locate three things: the claim that eCFR blocks scripted requests, the "Confirmed HTML structure" section describing Cornell's `span.et03` / `p.psection-1` DOM, and any passage about the index living outside the repo and being bind-mounted.

- [ ] **Step 2: Update `docs/tpr_rag_spec.md`**

Make these four changes:

1. **Correct the eCFR claim.** Replace the assertion that eCFR blocks scripted requests with: the *website* (`ecfr.gov/current/...`) serves a bot-check page, but the *versioner API* (`ecfr.gov/api/versioner/v1/...`) serves structured XML to plain HTTP clients. State plainly that the original evaluation tested the wrong endpoint. Don't delete the old finding — the correction is more useful with the mistake visible.

2. **Replace the Cornell HTML-structure section** with the eCFR XML structure: root `<DIV8 N="..." TYPE="SECTION">`, `<HEAD>` section title, structurally flat body (141 sibling `<P>` in `1.263(a)-3`, hierarchy encoded in paragraph text not nesting), `<EXAMPLE>` = `<HED>` + `<PSPACE>` siblings, trailing `<CITA>` authority line. Include the `<EXAMPLE>` flattening trap (`itertext()` fuses `<HED>` to `<PSPACE>`) and the `<CITA>` exclusion — both are exactly the kind of gotcha this doc exists to record.

3. **Document the `(i)` boundary rule** with the verified trace table from the design spec (Section 6), and state the outcome: 18 subsections `(a)`–`(r)` accepted once each, six nested `(i)` and three stray `(v)` rejected, and that `(i)` — the routine maintenance safe harbor, absent from the old Cornell index — is now recovered. Note the rule was validated against all four sections, not just `1.263(a)-3`.

4. **Record the sourcing rationale and the new build shape:** authoritative government API over a third-party mirror; source text committed under `docs/tpr-sources/` with a sha256 manifest (the `uv.lock` analogy); index built at image-build time and no longer bind-mounted. Delete or rewrite the passage explaining the "run ingest on the host to avoid root-owned files" workaround — that gotcha is eliminated, not relocated.

- [ ] **Step 3: Update `CLAUDE.md`**

Four edits:

- Under **Toolchain & commands**, add: `` **Refresh RAG source text (network, human-run):** `uv run python -m rag.fetch_sources` — rewrites `docs/tpr-sources/` + manifest; review the diff, re-run ingest, run tests, then commit. ``
- Under **Architecture**, rewrite the `rag/` bullet: `rag/fetch_sources.py` is the only network-touching module and writes the committed snapshot; `rag/ingest.py` parses that snapshot offline (stdlib `xml.etree.ElementTree` for CFR XML, BeautifulSoup for the IRS FAQ) into ChromaDB.
- Under **Environment variables**, rewrite the `TPR_RAG_DATA_DIR` entry: still defaults to `~/.tpr-rag/chroma_data` for local dev; in Docker the index is **built into the image** at that path and the host bind mount has been removed.
- Under **Gotchas**, replace the bind-mount/uid-1000 guidance with two new entries: (a) `docs/tpr-sources/` files are build inputs, not reference data — a refresh changes retrieval results with no human in the loop, and `tests/test_manifest.py` is what keeps them honest; (b) `chunk_cfr_xml`'s boundary rule needs *both* letter sequence and an italic title — weakening either one silently drops or truncates subsections.

Also update the **Testing conventions** section: note that `rag/ingest.py`'s heavy imports are now lazy, so `tests/test_ingest.py` imports it directly and needs no skip guard — the guard in `tests/test_tpr_rag.py` is still required because `rag/tpr_rag.py` still loads the model at import time.

- [ ] **Step 4: Update `README.md`**

Three edits:

- The "What's here" table row for RAG: change "Scrapes + chunks CFR/IRS pages into ChromaDB" to reflect committed-source ingest, e.g. "Chunks committed eCFR XML + IRS FAQ into ChromaDB, retrieves, builds a grounded prompt".
- The paragraph below the table describing the chunker: it currently sells "parses real-world government HTML (with documented traps around non-unique DOM ids and false subsection boundaries)". Rewrite around the current, stronger story: authoritative eCFR XML committed to the repo as a reviewable, sha256-pinned snapshot, parsed by a boundary rule that resolves the `(i)` ambiguity, with the committed files doubling as test fixtures so the chunker has real tests for the first time.
- The `> **Note:**` block after the RAG example: Docker users no longer pre-run ingest at all — the index ships in the image. Keep a one-line note that local non-Docker dev still runs `uv run python -m rag.ingest` once (now offline and fast), and mention `uv run python -m rag.fetch_sources` as the occasional refresh step.

- [ ] **Step 5: Correct the README's `sources` array from real retrieval**

The README shows a concrete `sources` array (`["1.263(a)-3(k)", "1.263(a)-3(j)", "1.263(a)-1(f)"]`).
The index roughly doubled and gained new subsections, so it is probably stale. Regenerate the
retrieval half in-container — this needs no Docker and no LLM call:

```bash
uv run python -c "
from rag.tpr_rag import answer_repair_question, retrieve_relevant_chunks
chunks = retrieve_relevant_chunks('I replaced the entire roof on a rental property.')
sources = []
for c in chunks:
    s = f\"{c['metadata']['source']}({c['metadata']['subsection']})\"
    if s not in sources: sources.append(s)
print(sources)
"
```

This reproduces exactly the dedupe-preserving-order logic `answer_repair_question` uses, so its
output *is* the `sources` array the endpoint would return. Update the README's JSON block with it.

Leave the `answer` prose string as-is for now and flag it in your report — regenerating it needs a
live LLM call against a running container, which is the human's step below.

- [ ] **Step 6: Hand the `answer` prose regeneration to the human**

Report that the README's `answer` string is **unverified against the new index**, and include this
for the human to run on the host after Task 4's build:

```bash
docker compose up -d --build
sleep 15
curl -s localhost:8000/api/v1/repair-tax-impact \
  -H 'content-type: application/json' \
  -d '{"description": "I replaced the entire roof on a rental property."}' | jq
```

The human pastes back the real response; the controller updates the README's `answer` string
(truncated the same way the current example is). Retrieval shifting is Risk 1 in the spec — the
README example must be re-verified rather than assumed still accurate.

**Deferred, not blocking** — same as Task 4. Park it in the ledger and finish the plan:

```
Task 5: parked — README `answer` prose unverified against the new index — ruling: `sources`
        array regenerated from real retrieval; prose needs one live endpoint call by the human
        before merge.
```

- [ ] **Step 7: Verify docs are consistent with the code**

Run:

```bash
grep -rn "psection-1\|et03\|law.cornell.edu\|blocks plain scripted\|bind-mount\|bind mount" \
  README.md CLAUDE.md docs/*.md Dockerfile docker-compose.yml
```

Expected: no hits describing current behavior. Hits inside a clearly-labelled historical passage in `docs/tpr_rag_spec.md` (the "we originally tested the wrong endpoint" correction) are fine and intended.

- [ ] **Step 8: Full verification and commit**

Run: `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest`
Expected: all clean.

```bash
git add README.md CLAUDE.md docs/tpr_rag_spec.md
git commit -m "Document the eCFR sourcing migration"
```

---

## Human verification owed before merge (deferred from Tasks 4 and 5)

The devcontainer has no Docker daemon, so two verifications cannot run in-loop. They are parked
in the ledger, not skipped — the branch is **not mergeable** until both pass. Run on the host:

```bash
# 1. Task 4 — the image builds offline and the baked index is reachable
docker compose build app
docker compose run --rm --no-deps app uv run python -c "
from rag.tpr_rag import retrieve_relevant_chunks
chunks = retrieve_relevant_chunks('Is replacing an entire roof a restoration?')
print(len(chunks))
for c in chunks: print(c['metadata']['source'], c['metadata']['subsection'])
"
# expect: ~500 chunks indexed during build; then 6 real citations. A 0 means the
# baked index isn't being found — TPR_RAG_DATA_DIR mismatch or an incomplete
# volumes: removal.

# 2. Task 5 — the README's example answer still reflects reality
docker compose up -d --build && sleep 15
curl -s localhost:8000/api/v1/repair-tax-impact \
  -H 'content-type: application/json' \
  -d '{"description": "I replaced the entire roof on a rental property."}' | jq
```

## Done criteria

- [ ] `docs/tpr-sources/` holds four XML files, one HTML file, and `_manifest.json`, all committed.
- [ ] `uv run pytest` passes with no network access — `tests/test_ingest.py` and `tests/test_manifest.py` both run (neither skips).
- [ ] `rag/ingest.py` contains no `httpx` import and no Cornell CSS selectors.
- [ ] `1.263(a)-3(i)` "Safe harbor for routine maintenance on property" is present in the index.
- [ ] `docker compose build app` succeeds with no network access to eCFR or irs.gov, and the built image answers retrieval queries with six real citations.
- [ ] `docker-compose.yml` has no `volumes:` block under `app`.
- [ ] README's RAG example output reflects a real response from the rebuilt index.

## Out of scope (do not do these)

- AWS infrastructure — ECR, VPC, EC2, CI image push. Separate branch and spec.
- Changing `EMBED_MODEL`, `SUBCHUNK_CHARS`, `_pack`, or the chunk-size strategy.
- Adding `1.168(i)-8` or `1.168(a)-1` to the corpus.
- Editing `rag/tpr_rag.py` or `rag/router.py` — the preserved metadata contract is what makes that unnecessary. If you find yourself needing to change them, the contract broke; stop and report it.
- `git push` — no push credentials exist in this container by design.
