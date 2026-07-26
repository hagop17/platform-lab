import os
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from opentelemetry import trace
from sentence_transformers import SentenceTransformer

from llm_providers import complete

load_dotenv()  # explicit — don't rely on the runner (fastapi dev, etc.) to load .env

tracer = trace.get_tracer(__name__)

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


def retrieve_relevant_chunks(question: str, k: int = 6) -> list[dict]:
    # Embedding and the vector search are both in-process, so no httpx span
    # covers them — split into two spans to tell "the model is slow" apart from
    # "the ChromaDB query is slow".
    with tracer.start_as_current_span("rag.embed") as span:
        span.set_attribute("rag.embed_model", EMBED_MODEL)
        question_embedding = _model.encode([question]).tolist()

    with tracer.start_as_current_span("rag.vector_query") as span:
        span.set_attribute("rag.k", k)
        span.set_attribute("rag.collection", COLLECTION_NAME)
        results = _collection.query(query_embeddings=question_embedding, n_results=k)

        if not results["documents"] or not results["documents"][0] or not results["metadatas"]:
            span.set_attribute("rag.chunks_returned", 0)
            return []

        chunks = [
            {"text": doc, "metadata": meta}
            for doc, meta in zip(results["documents"][0], results["metadatas"][0])
        ]
        span.set_attribute("rag.chunks_returned", len(chunks))
        return chunks


# Some CFR subsections (e.g. 1.263(a)-3(k), 53k+ chars) are far larger than
# what fits in a free-tier LLM rate limit once you assemble k of them into
# one prompt — Groq's TPM cap was hit at ~13.4k tokens from a single large
# chunk. Cap what actually goes into the prompt; ChromaDB still stores (and
# `sources` still cites) the full, untruncated text.
MAX_CHUNK_CHARS = 1200


def build_prompt(question: str, chunks: list[dict]) -> str:
    context = "\n\n".join(
        f"[{c['metadata']['source']}({c['metadata']['subsection']}) — {c['metadata']['topic']}]\n"
        f"{c['text'][:MAX_CHUNK_CHARS]}{'...' if len(c['text']) > MAX_CHUNK_CHARS else ''}"
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


def answer_repair_question(question: str, k: int = 6) -> dict:
    span = trace.get_current_span()
    span.set_attribute("rag.grounded", True)

    chunks = retrieve_relevant_chunks(question, k=k)

    if not chunks:
        span.set_attribute("rag.no_context", True)
        return {"answer": "No relevant regulation text found for this question.", "sources": []}

    prompt = build_prompt(question, chunks)
    span.set_attribute("rag.prompt_chars", len(prompt))

    # Sub-chunking means several retrieved chunks can share one subsection —
    # dedupe the citations while preserving retrieval order.
    sources: list[str] = []
    for c in chunks:
        src = f"{c['metadata']['source']}({c['metadata']['subsection']})"
        if src not in sources:
            sources.append(src)

    return {"answer": complete(prompt), "sources": sources}
