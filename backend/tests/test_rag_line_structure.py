"""RAG chunks keep line breaks, and marina / dock lists score as lists (P0-3 round 2)."""
from app.core import rag
from app.core.rag import chunk_text, list_like_score, select_list_context

PROSE = "The ministry was established in 1992 and reformed several times. "
MARINAS = [
    "Abaco Beach Resort Marina", "Bimini Big Game Club Dock", "Cat Cay Yacht Club Wharf",
    "Government Dock, Spanish Wells", "Harbour Island Public Pier", "Nassau Prince George Quay",
    "Treasure Cay Marina", "Rock Sound Anchorage", "Walker's Cay Marina", "Port Lucaya Marina",
]


def test_chunks_keep_one_list_entry_per_line():
    text = "Ports of entry:\n" + "\n".join(f"  {m}\t" for m in MARINAS) + "\n\n\n"
    [chunk] = chunk_text(text, size=2000)
    assert chunk.split("\n") == ["Ports of entry:"] + MARINAS
    assert "  " not in chunk and "\t" not in chunk


def test_long_list_is_cut_on_line_breaks():
    text = "\n".join(MARINAS * 6)
    chunks = chunk_text(text, size=200, overlap=0)
    assert len(chunks) > 1
    for c in chunks:  # every cut lands between two entries, never inside a name
        assert all(line in MARINAS for line in c.split("\n")), c


def test_prose_without_line_breaks_still_cuts_on_sentences():
    chunks = chunk_text(PROSE * 30, size=200, overlap=0)
    assert all(c.endswith(".") for c in chunks[:-1])


def test_marina_and_dock_terms_count_as_list_signal():
    assert list_like_score(PROSE) == 0.0
    for term in ("marina", "Marinas", "dock", "wharf", "wharves", "quay", "pier", "anchorage",
                 "harbour", "harbor"):
        assert list_like_score(f"The {term} here.") > 0.0, term
    # Word boundaries: no credit for "docket", "Pierre" or "piercing".
    assert list_like_score("The docket was filed by Pierre, piercing the record.") == 0.0
    assert list_like_score("\n".join(MARINAS)) >= 0.9


def test_marina_list_reaches_the_reader_with_its_lines(monkeypatch):
    monkeypatch.setattr(rag, "_cosine_scores", lambda q, docs: [0.0] * len(docs))
    listing = "\n".join(MARINAS)
    text = PROSE * 80 + "\n" + listing + "\n" + PROSE * 80
    ctx = select_list_context("ports of entry", text, max_chars=800, size=200)
    assert "Treasure Cay Marina\nRock Sound Anchorage" in ctx
