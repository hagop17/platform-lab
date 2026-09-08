"""Latency benchmark for the RAG retrieval path.

Produces the numbers quoted in docs/guides/rag-embeddings-primer.md, so that
table is reproducible rather than something a reader has to take on trust.
Results are hardware-specific — the point is the *ratio* between retrieval
and generation, not the absolute milliseconds.

By default it measures the local, offline stages only: model load, query
embedding, document embedding, and the ChromaDB search. No network, no API
key, no cost.

`--llm` additionally times the generation half, which is the bottleneck the
retrieval numbers are meant to be compared against. That path is opt-in
because it makes real, paid requests to whichever provider LLM_PROVIDER
selects, so the default run stays free and offline.

Requires a built index (`uv run python -m rag.ingest`).

Usage:
    uv run python -m rag.benchmark          # offline stages only
    uv run python -m rag.benchmark --llm    # also time the LLM (paid)
"""

import statistics
import sys
import time
from pathlib import Path

QUESTION = "I replaced 3 of 10 rooftop HVAC units on my rental building"
RUNS = 30
LLM_RUNS = 3  # each one is a paid request, so far fewer than the local stages


def _time_ms(fn, runs: int = RUNS) -> tuple[float, float, float]:
    """Return (median, min, max) milliseconds over `runs` calls, after a warm-up.

    The warm-up matters: the first call through either the model or the
    ChromaDB client pays one-off initialisation that would otherwise land in
    the sample and inflate the median.
    """
    fn()
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples), min(samples), max(samples)


def _time_llm(prompt: str) -> tuple[float, float, float]:
    """Return (median, min, max) seconds over LLM_RUNS real completions.

    No warm-up call here, unlike _time_ms: every request costs money, and
    there is no local cache for the first one to prime anyway.
    """
    # Imported here so the default offline run never touches the provider SDK.
    # tpr_rag loads the model and the collection at import time and calls
    # load_dotenv(), which is what puts the API key in the environment.
    from rag.tpr_rag import complete

    samples = []
    for _ in range(LLM_RUNS):
        start = time.perf_counter()
        complete(prompt)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples), min(samples), max(samples)


def main(include_llm: bool = False):
    # Imported inside main() for the same reason ingest.py does it: keep the
    # module importable (and lintable) without pulling in torch.
    from rag.fetch_sources import CFR_SECTIONS, IRS_FAQ_FILENAME, SOURCES_DIR
    from rag.ingest import (
        CHROMA_PATH,
        COLLECTION_NAME,
        EMBED_MODEL,
        chunk_cfr_xml,
        chunk_irs_faq,
    )

    # Import and construction are timed separately because they are wildly
    # asymmetric: pulling in torch dominates, and constructing the model once
    # the library is loaded is nearly free. Reporting only the second would
    # understate what a process actually pays at startup.
    start = time.perf_counter()
    from sentence_transformers import SentenceTransformer

    import_s = time.perf_counter() - start

    start = time.perf_counter()
    model = SentenceTransformer(EMBED_MODEL)
    load_s = time.perf_counter() - start
    print(
        f"model load                 {import_s + load_s:6.1f} s    "
        f"(import {import_s:.1f} s + construct {load_s:.1f} s)"
    )

    median, low, high = _time_ms(lambda: model.encode([QUESTION]))
    print(f"query embedding            {median:6.1f} ms   ({low:.1f}-{high:.1f})")

    chunks = []
    for section in CFR_SECTIONS:
        xml_bytes = (SOURCES_DIR / f"{section}.xml").read_bytes()
        chunks += chunk_cfr_xml(xml_bytes, section)
    faq_html = (SOURCES_DIR / IRS_FAQ_FILENAME).read_text(encoding="utf-8")
    chunks += chunk_irs_faq(faq_html)
    texts = [c["text"] for c in chunks]

    start = time.perf_counter()
    model.encode(texts)
    batch_s = time.perf_counter() - start
    print(
        f"document embedding         {batch_s:6.1f} s    "
        f"({len(texts)} chunks, {batch_s / len(texts) * 1000:.0f} ms/chunk)"
    )

    import chromadb

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    collection = client.get_collection(COLLECTION_NAME)
    embedding = model.encode([QUESTION]).tolist()
    median, low, high = _time_ms(lambda: collection.query(query_embeddings=embedding, n_results=6))
    print(f"ANN search, k=6            {median:6.2f} ms   ({low:.2f}-{high:.2f})")

    if include_llm:
        from rag.tpr_rag import build_prompt, retrieve_relevant_chunks

        prompt = build_prompt(QUESTION, retrieve_relevant_chunks(QUESTION))
        median, low, high = _time_llm(prompt)
        print(
            f"LLM call                   {median:6.2f} s    "
            f"({low:.2f}-{high:.2f}, {LLM_RUNS} runs, {len(prompt)} char prompt)"
        )

    print(f"\nindexed chunks: {collection.count()}   path: {Path(CHROMA_PATH)}")
    if not include_llm:
        print("LLM call not measured; pass --llm to time it (makes paid requests).")


if __name__ == "__main__":
    main(include_llm="--llm" in sys.argv)
