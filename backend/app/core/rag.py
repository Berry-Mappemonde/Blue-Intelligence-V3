"""
rag_core.py — Local semantic RAG & smart chunking (shared PoE / Projects).

- Split long PDFs/reports into ~500-character blocks.
- Select relevant blocks by cosine similarity before the LLM call.
- Semantic source monitoring (skip re-extraction if content is nearly identical).
- Candidate re-ranking (geocoding) by semantic similarity.

Backend: sentence-transformers (all-MiniLM-L6-v2) if available,
else TF-IDF fallback (scikit-learn) — same API, no hard dependency.
"""
import re

_st_model = None
_st_failed = False
_ce_model = None
_ce_failed = False


def _get_st():
    global _st_model, _st_failed
    if _st_model is not None or _st_failed:
        return _st_model
    try:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    except Exception:
        _st_failed = True
    return _st_model


def _get_ce():
    """ms-marco Cross-Encoder for re-ranking (spec) — lazily loaded."""
    global _ce_model, _ce_failed
    if _ce_model is not None or _ce_failed:
        return _ce_model
    try:
        from sentence_transformers import CrossEncoder
        _ce_model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    except Exception:
        _ce_failed = True
    return _ce_model


def embedding_backend() -> str:
    return "sentence-transformers" if _get_st() is not None else "tfidf"


def chunk_text(text: str, size: int = 500, overlap: int = 60) -> list[str]:
    """Smart chunking: ~size-char blocks aligned on sentence ends."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind(". ", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + size // 2, end)
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end].strip())
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _cosine_scores(query: str, docs: list[str]) -> list[float]:
    model = _get_st()
    if model is not None:
        emb = model.encode([query] + docs, normalize_embeddings=True)
        return (emb[1:] @ emb[0]).tolist()
    # Repli TF-IDF
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    vec = TfidfVectorizer(ngram_range=(1, 2), max_features=20000).fit([query] + docs)
    m = vec.transform([query] + docs)
    return cosine_similarity(m[0:1], m[1:]).flatten().tolist()


def top_chunks(query: str, text: str, k: int = 8, size: int = 500) -> list[str]:
    chunks = chunk_text(text, size=size)
    if len(chunks) <= k:
        return chunks
    try:
        scores = _cosine_scores(query, chunks)
    except Exception:
        return chunks[:k]
    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:k]
    return [chunks[i] for i in sorted(ranked)]  # original order preserved


def select_context(query: str, text: str, max_chars: int = 8000, size: int = 500) -> str:
    """Condensed context for the LLM: relevant chunks only."""
    if len(text or "") <= max_chars:
        return text or ""
    k = max(2, max_chars // size)
    return "\n…\n".join(top_chunks(query, text, k=k, size=size))[:max_chars]


_LIST_CHUNK_RE = re.compile(
    r"port of entry|ports of entry|puertos habilitados|puerto de entrada|"
    r"ports? d['’ ]?entr[eé]e|portos de entrada|designated port|"
    r"harbour|harbor|capitan[ií]a|latitud|longitud|gazette|decreto|décret|"
    r"customs|douane|aduana|\b1[.)]\s+\w+|\b\d+\.\-\s+\w+",
    re.I,
)


def list_like_score(chunk: str) -> float:
    """1.0 if the chunk looks like a port list / decree."""
    if not chunk:
        return 0.0
    hits = len(_LIST_CHUNK_RE.findall(chunk))
    digits = len(re.findall(r"\d+\.\d+", chunk))
    return min(1.0, hits * 0.18 + min(digits, 6) * 0.06)


def select_list_context(query: str, text: str, max_chars: int = 20000,
                        size: int = 500) -> str:
    """Like select_context, but list-like chunks go ahead of
    semantic similarity alone (a long decree often hides the list
    at the end)."""
    if len(text or "") <= max_chars:
        return text or ""
    chunks = chunk_text(text, size=size)
    if not chunks:
        return (text or "")[:max_chars]
    k = max(4, max_chars // size)
    try:
        sem = _cosine_scores(query, chunks)
    except Exception:
        sem = [0.0] * len(chunks)
    ranked = sorted(
        range(len(chunks)),
        key=lambda i: (list_like_score(chunks[i]) * 1.4 + (sem[i] if i < len(sem) else 0.0)),
        reverse=True,
    )[:k]
    return "\n…\n".join(chunks[i] for i in sorted(ranked))[:max_chars]


def rerank_candidates(query: str, candidates: list[str]) -> list[tuple[int, float]]:
    """Return [(index, score)] sorted by decreasing relevance.
    ms-marco Cross-Encoder if available, else bi-encoder/TF-IDF cosine."""
    if not candidates:
        return []
    if len(candidates) == 1:
        return [(0, 1.0)]
    ce = _get_ce()
    if ce is not None:
        try:
            scores = ce.predict([(query, c) for c in candidates])
            return sorted(((i, float(s)) for i, s in enumerate(scores)), key=lambda x: x[1], reverse=True)
        except Exception:
            pass
    try:
        scores = _cosine_scores(query, candidates)
    except Exception:
        return [(i, 0.0) for i in range(len(candidates))]
    return sorted(((i, float(s)) for i, s in enumerate(scores)), key=lambda x: x[1], reverse=True)


def semantic_similarity(a: str, b: str) -> float:
    """Document-to-document cosine similarity (source monitoring)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        return max(0.0, min(1.0, _cosine_scores(a[:5000], [b[:5000]])[0]))
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a[:3000], b[:3000]).ratio()


def content_changed(old_text: str, new_text: str, threshold: float | None = None) -> bool:
    """Semantic monitoring: True only if the content actually changed
    (avoids an LLM re-extraction for a minor HTML change)."""
    if not old_text or not new_text:
        return True
    if threshold is None:
        from app.core.run_rules import get_rule
        threshold = float(get_rule("shared.content_changed_sim", 0.95))
    return semantic_similarity(old_text, new_text) < threshold
