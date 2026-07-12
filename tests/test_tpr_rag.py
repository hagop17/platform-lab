from pathlib import Path

import pytest

# rag/tpr_rag.py loads a SentenceTransformer model and opens a ChromaDB
# client at *import time*. If the embedding model isn't already cached
# locally, importing it would try to download from the Hugging Face Hub —
# which violates this suite's "no network calls" rule. Skip rather than
# fail on a machine/CI runner with a cold cache; run rag/ingest.py once (or
# otherwise warm the HF cache) to enable this test there.
from huggingface_hub import constants as hf_constants

_EMBED_MODEL_CACHE_DIR = (
    Path(hf_constants.HF_HUB_CACHE) / "models--sentence-transformers--all-MiniLM-L6-v2"
)
if not _EMBED_MODEL_CACHE_DIR.exists():
    pytest.skip(
        f"sentence-transformers model not cached at {_EMBED_MODEL_CACHE_DIR} — "
        "importing rag.tpr_rag would hit the network to download it. "
        "Run rag/ingest.py once to warm the cache and enable this test.",
        allow_module_level=True,
    )

from rag.tpr_rag import answer_repair_question, build_prompt  # noqa: E402


def _chunk(source: str, subsection: str, topic: str, text: str) -> dict:
    return {"text": text, "metadata": {"source": source, "subsection": subsection, "topic": topic}}


def test_build_prompt_includes_question_and_untruncated_short_chunk():
    chunks = [_chunk("1.263(a)-3", "h", "Betterments", "short excerpt text")]

    prompt = build_prompt("Is a new roof a betterment?", chunks)

    assert "Is a new roof a betterment?" in prompt
    assert "[1.263(a)-3(h) — Betterments]\nshort excerpt text" in prompt
    assert "..." not in prompt


def test_build_prompt_truncates_long_chunk():
    long_text = "x" * 2000
    chunks = [_chunk("1.263(a)-3", "h", "Betterments", long_text)]

    prompt = build_prompt("question", chunks)

    assert ("x" * 1200) + "..." in prompt
    assert long_text not in prompt


def test_answer_repair_question_dedupes_sources_preserving_order(monkeypatch):
    chunks = [
        _chunk("1.263(a)-3", "h", "Betterments", "part 1"),
        _chunk("1.263(a)-1", "f", "De minimis safe harbor", "part 2"),
        _chunk("1.263(a)-3", "h", "Betterments", "part 3 (continued)"),
    ]
    monkeypatch.setattr("rag.tpr_rag.retrieve_relevant_chunks", lambda question, k=6: chunks)
    monkeypatch.setattr("rag.tpr_rag.complete", lambda prompt: "fake answer")

    result = answer_repair_question("question")

    assert result["answer"] == "fake answer"
    assert result["sources"] == ["1.263(a)-3(h)", "1.263(a)-1(f)"]


def test_answer_repair_question_no_chunks_found(monkeypatch):
    monkeypatch.setattr("rag.tpr_rag.retrieve_relevant_chunks", lambda question, k=6: [])
    monkeypatch.setattr(
        "rag.tpr_rag.complete",
        lambda prompt: pytest.fail("complete() should not be called when no chunks are retrieved"),
    )

    result = answer_repair_question("question")

    assert result == {
        "answer": "No relevant regulation text found for this question.",
        "sources": [],
    }
