"""Chunker tests, hermetic by construction.

The committed eCFR XML in docs/tpr-sources/ doubles as the fixture corpus —
that is the whole point of committing it. No network, no ChromaDB, no
embedding model: chromadb and sentence_transformers are imported lazily
inside build_index(), so importing rag.ingest here is cheap and needs none of
the cold-HF-cache skip guard that tests/test_tpr_rag.py carries.
"""

import string

import pytest

from rag.fetch_sources import SOURCES_DIR
from rag.ingest import SUBCHUNK_CHARS, chunk_cfr_xml


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
    assert parts[1]["text"].startswith("[(k) Capitalization of restorations (continued)]\n")


def test_chunk_metadata_shape_is_unchanged():
    """rag/tpr_rag.py reads all four keys; changing this shape breaks retrieval
    citations without breaking any import."""
    chunk = _chunks("1.162-4")[0]

    assert set(chunk) == {"text", "metadata"}
    assert set(chunk["metadata"]) == {"source", "subsection", "topic", "part"}
    assert chunk["metadata"]["source"] == "1.162-4"
