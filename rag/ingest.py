import os
import re
import string
import xml.etree.ElementTree as ET
from pathlib import Path

import chromadb
import httpx
from bs4 import BeautifulSoup
from chromadb.errors import NotFoundError
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv()  # explicit — don't rely on the runner (fastapi dev, etc.) to load .env

EMBED_MODEL = "all-MiniLM-L6-v2"  # same model used at query time, must match
COLLECTION_NAME = "tpr_regulations"
# Outside the repo by default — survives repo deletion/reclone, never git-tracked.
CHROMA_PATH = Path(os.environ.get("TPR_RAG_DATA_DIR", Path.home() / ".tpr-rag" / "chroma_data"))

# Confirmed by directly fetching each URL and inspecting the real HTML (see
# docs/tpr_rag_spec.md "Confirmed HTML structure" / "Source selection" sections).
# eCFR.gov was tried first and rejected: it blocks plain scripted requests
# (returns a bot-check page), whereas Cornell LII responds with real content.
SOURCES = {
    "1.263(a)-1": {
        "url": "https://www.law.cornell.edu/cfr/text/26/1.263(a)-1",
        "subsections": {"a", "f"},  # (a) general rule, (f) de minimis safe harbor
    },
    "1.263(a)-3": {
        "url": "https://www.law.cornell.edu/cfr/text/26/1.263(a)-3",
        # (i) intentionally excluded: confirmed by direct inspection that
        # this page has no distinct top-level "(i)" heading paragraph the
        # way (h)/(j)/(k)/(l) have. The routine-maintenance safe-harbor
        # text is nested three levels deep inside (h)'s body with no
        # stable top-level anchor; the only id="i" element on the whole
        # page is an unrelated nested list item under (h)(5) (even
        # Cornell's own "#i" cross-reference links resolve to it). See
        # docs/tpr_rag_spec.md "Confirmed HTML structure" for the full trace.
        "subsections": {"d", "h", "j", "k", "l"},
    },
}
IRS_FAQ_URL = "https://www.irs.gov/businesses/small-businesses-self-employed/tangible-property-final-regulations"


def fetch_page(url: str) -> str:
    resp = httpx.get(
        url,
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    return resp.text


def _top_level_markers(soup: BeautifulSoup) -> list:
    """
    Cornell LII marks each top-level lettered subsection as:
        <p class="psection-1">
          <span class="enumxml" id="j">(j)</span>
          <span class="et03">Capitalization of betterments</span>—(1) ...
        </p>

    Two traps make `p.psection-1 > span.enumxml` alone unreliable:

    1. The `id` attribute is not unique — nested roman-numeral list items
       deeper in the page (e.g. a 2-item list under (h)(5) labeled
       "(i)"/"(ii)") reuse the `psection-1` class and even collide on the
       literal `id` of a real top-level letter (1.263(a)-3 has two elements
       with id="i"). So `id` can't be trusted.
    2. A stateful "next expected letter a, b, c, ..." filter doesn't work
       either: the bogus nested "(i)" sits right after "(h)" and its label
       happens to equal the next expected letter "i", so it gets accepted —
       which then truncates the (h) chunk at that false boundary. And once
       the genuine top-level (i) is absent, a strict sequence stalls waiting
       for "i" and drops (j) onward.

    What actually distinguishes genuine top-level subsections is *both*:
      - a single alphabetic label ("(a)".."(r)"), which rejects the
        two-char "(ii)" items, and
      - a non-empty `<span class="et03">` title sibling, which rejects the
        empty-titled nested "(i)"/"(ii)" list items.
    Verified against both 1.263(a)-1 and 1.263(a)-3: every genuine
    subsection has a non-empty et03 title; every bogus nested marker fails
    one of these two checks.
    """
    top_level = []
    for marker in soup.select("p.psection-1 > span.enumxml"):
        label = marker.get_text(strip=True).strip("()")
        if len(label) != 1 or not label.isalpha():
            continue
        if marker.parent is None:
            continue
        title_span = marker.parent.select_one("span.et03")
        if title_span is None or not title_span.get_text(strip=True):
            continue
        top_level.append(marker)
    return top_level


# Whole subsections are far too large to embed as one chunk — (h), (j), (k)
# on 1.263(a)-3 are each 40k–53k chars, but all-MiniLM-L6-v2 only reads
# ~256 tokens (~1000 chars) of input, so a single-chunk-per-subsection
# embedding would only reflect each subsection's opening. Pack each source's
# text into pieces under this budget so every chunk's embedding actually
# represents its own content.
SUBCHUNK_CHARS = 1000

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.;])\s+")


def _split_long(text: str, limit: int) -> list[str]:
    """Split one over-long paragraph into <=limit pieces, preferring sentence
    boundaries and hard-slicing only a single sentence that alone exceeds the
    limit (rare)."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for sent in _SENTENCE_BOUNDARY.split(text):
        if len(sent) > limit:  # a single sentence bigger than the budget
            if buf:
                pieces.append(" ".join(buf))
                buf, buf_len = [], 0
            pieces.extend(sent[j : j + limit] for j in range(0, len(sent), limit))
            continue
        if buf and buf_len + len(sent) > limit:
            pieces.append(" ".join(buf))
            buf, buf_len = [], 0
        buf.append(sent)
        buf_len += len(sent)
    if buf:
        pieces.append(" ".join(buf))
    return pieces


def _pack(paragraphs: list[str], limit: int = SUBCHUNK_CHARS) -> list[str]:
    """Pack per-paragraph texts into sub-chunks each <=limit chars, splitting
    any individual paragraph that alone exceeds the limit. Guarantees no
    output piece is materially larger than the embedding window."""
    units: list[str] = []
    for para in paragraphs:
        if para:
            units.extend(_split_long(para, limit))

    sub_chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for unit in units:
        if buf and buf_len + len(unit) > limit:
            sub_chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        buf.append(unit)
        buf_len += len(unit)
    if buf:
        sub_chunks.append("\n".join(buf))
    return sub_chunks


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

        expected = string.ascii_lowercase[len(subsections)] if len(subsections) < 26 else None
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


def chunk_cfr_page(html: str, source_label: str, wanted_subsections: set[str]) -> list[dict]:
    """
    For each wanted top-level subsection, collect its header paragraph plus
    all sibling <p> tags (e.g. class="psection-2" for nested (j)(1), (j)(2),
    ...) up to the next top-level marker, then split that body into
    embedding-window-sized sub-chunks (see _pack). The
    et03 title span can contain nested markup (e.g. a definedterm link), so
    title extraction uses get_text(), not raw string matching.
    """
    soup = BeautifulSoup(html, "html.parser")
    markers = _top_level_markers(soup)

    chunks = []
    for i, marker in enumerate(markers):
        letter = marker.get_text(strip=True).strip("()")
        if letter not in wanted_subsections:
            continue

        parent_p = marker.parent
        title_span = parent_p.select_one("span.et03")
        topic = title_span.get_text(" ", strip=True) if title_span else ""

        next_boundary = markers[i + 1].parent if i + 1 < len(markers) else None

        body_parts = [parent_p.get_text(" ", strip=True)]
        sib = parent_p.find_next_sibling()
        while sib is not None and sib is not next_boundary:
            body_parts.append(sib.get_text(" ", strip=True))
            sib = sib.find_next_sibling()

        sub_chunks = _pack(body_parts)
        header = f"({letter}) {topic}".strip()
        for part_idx, sub_text in enumerate(sub_chunks):
            # Prefix continuation pieces with the subsection header so each
            # sub-chunk is self-describing when embedded/retrieved in isolation
            # (the first piece already opens with the header text).
            text = sub_text if part_idx == 0 else f"[{header} (continued)]\n{sub_text}"
            chunks.append(
                {
                    "text": text,
                    "metadata": {
                        "source": source_label,
                        "subsection": letter,
                        "topic": topic,
                        "part": part_idx,
                    },
                }
            )

    return chunks


def chunk_irs_faq(html: str, source_label: str = "IRS FAQ") -> list[dict]:
    """
    The IRS FAQ page's real content lives inside a single <article> element
    (confirmed directly — the page has exactly one <article> tag, separate
    from the surrounding nav chrome which uses its own h2/h3 tags outside
    that container). Each question is an <h2>/<h3>/<h4> heading followed by
    one or more <p> answer paragraphs, until the next heading.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("article")
    if article is None:
        return []

    headings = article.find_all(["h2", "h3", "h4"])

    chunks = []
    for i, heading in enumerate(headings):
        question = heading.get_text(" ", strip=True)
        if not question:
            continue

        body_parts = []
        sib = heading.find_next_sibling()
        while sib is not None and sib.name not in ("h2", "h3", "h4"):
            if sib.name == "p":
                body_parts.append(sib.get_text(" ", strip=True))
            sib = sib.find_next_sibling()

        if not any(body_parts):
            # heading with no answer body — likely nav/section chrome, not a real FAQ entry
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
        # Prepend the question so it's packed together with (and never split
        # away from) the start of its answer, then keep continuation pieces
        # tied to the question for standalone retrieval.
        sub_chunks = _pack([question, *body_parts])
        for part_idx, sub_text in enumerate(sub_chunks):
            text = sub_text if part_idx == 0 else f"[{question} (continued)]\n{sub_text}"
            chunks.append(
                {
                    "text": text,
                    "metadata": {
                        "source": source_label,
                        "subsection": slug,
                        "topic": question,
                        "part": part_idx,
                    },
                }
            )

    return chunks


def build_index():
    model = SentenceTransformer(EMBED_MODEL)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # Delete-and-recreate rather than incrementally updating: this corpus is
    # tiny (3 source docs, a couple dozen chunks) so a full rebuild is near-
    # instant, and it keeps re-ingestion simple — no need to reconcile
    # stale/removed chunks from a prior run.
    try:
        client.delete_collection(COLLECTION_NAME)
    except NotFoundError:
        pass  # collection didn't exist yet (first run)

    collection = client.get_or_create_collection(
        COLLECTION_NAME, metadata={"embed_model": EMBED_MODEL}
    )

    all_chunks = []
    for source_label, source in SOURCES.items():
        html = fetch_page(source["url"])
        found = chunk_cfr_page(html, source_label, source["subsections"])
        subs = sorted({c["metadata"]["subsection"] for c in found})
        print(f"{source_label}: {len(found)} chunks across subsections {subs}")
        all_chunks.extend(found)

    faq_chunks = chunk_irs_faq(fetch_page(IRS_FAQ_URL))
    print(f"IRS FAQ: {len(faq_chunks)} chunks")
    all_chunks.extend(faq_chunks)

    if not all_chunks:
        raise RuntimeError(
            "Ingestion produced 0 chunks across all sources — parsing likely "
            "broke (e.g. a source page's HTML structure changed). Refusing "
            "to build an empty index; fix the chunker before re-running."
        )

    texts = [c["text"] for c in all_chunks]
    embeddings = model.encode(texts).tolist()

    collection.add(
        documents=texts,
        embeddings=embeddings,
        metadatas=[c["metadata"] for c in all_chunks],
        ids=[f"chunk_{i:04d}" for i in range(len(all_chunks))],
    )
    print(f"Indexed {len(all_chunks)} chunks into ChromaDB at {CHROMA_PATH}")


if __name__ == "__main__":
    build_index()
