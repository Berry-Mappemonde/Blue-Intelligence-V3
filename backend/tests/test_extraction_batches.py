"""P0-3: long source text is read in batches instead of one truncated call (stubbed LLM, offline)."""
import asyncio
import re

import pytest

from app.core import llm, tokenfactory
from app.core.run_rules import bind_rules, catalog_default, reset_rules, snapshot_for_run
from app.services import poe_pipeline as poe

ZONE = {"name": "Testland", "sovereign": "Testland", "mrgid": 990001}
PROSE = "General information about travel formalities, visas and fees for visitors. "
NAME_RE = re.compile(r"\b(?:Port [A-Z][a-z]+|[A-Z][a-z]+ Port)\b")


def _source(url: str, body: str) -> str:
    return f"[SOURCE: {url}]\n{body}"


def _prose(chars: int) -> str:
    return (PROSE * (chars // len(PROSE) + 1))[:chars]


@pytest.fixture
def reader(monkeypatch):
    """Stub primary reader: returns the port names present in what it actually sends."""
    sent: list[str] = []

    async def fake_extract(context, zone, settings=None, log=None):
        text = llm.reader_context(context)
        sent.append(text)
        return [{"name": n, "city": None, "note": None, "extraction_engine": "nightshift"}
                for n in dict.fromkeys(NAME_RE.findall(text))]

    import app.core.ml as ml
    monkeypatch.setattr(poe, "extract_ports", fake_extract)
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    tokenfactory.reset_ledger()
    yield sent
    tokenfactory.reset_ledger()


def _core(context, catalog_text=None, logs=None):
    log = logs.append if logs is not None else (lambda m: None)
    return asyncio.run(poe._extract_ports_core(context, ZONE, log, catalog_text=catalog_text))


def test_ports_beyond_the_window_are_now_extracted(reader):
    raw = _source("https://gov.example/decree",
                  _prose(45000) + "\nDesignated ports of entry: Port Alpha, Port Beta.\n" + _prose(3000))
    context, window, branch = poe.extraction_context(raw, [], "ports of entry")
    assert branch == "rag" and raw.index("Port Alpha") > llm.LLM_CONTEXT_CHARS

    # One truncated call (the old path) never reads the decree's tail.
    names_old = {p["name"] for p in _core(raw)}
    assert "Port Alpha" not in names_old and len(reader) == 1

    reader.clear()
    names = {p["name"] for p in _core(context, catalog_text=raw)}
    assert {"Port Alpha", "Port Beta"} <= names
    assert len(reader) > 1
    assert all(len(s) <= llm.LLM_CONTEXT_CHARS for s in reader)
    assert all(s.startswith("[SOURCE: https://gov.example/decree]\n") for s in reader)
    assert llm.llm_context_chars.get() == llm.LLM_CONTEXT_CHARS


def test_duplicates_across_batches_merge(reader):
    raw = "\n\n".join([
        _source("https://gov.example/a", _prose(8000) + " Port Kappa and Port Lima. " + _prose(8000)),
        _source("https://gov.example/b", _prose(8000) + " Kappa Port, Port Mike. " + _prose(8000)),
    ])
    ports = _core(raw[:llm.LLM_CONTEXT_CHARS], catalog_text=raw)
    assert len(reader) == 2  # two sources of ~16k cannot share one 20k batch
    names = [p["name"] for p in ports]
    assert sorted(names) == ["Port Kappa", "Port Lima", "Port Mike"]  # "Kappa Port" merged


def test_name_on_a_cut_is_read_whole_and_merged(reader):
    body = _prose(19900) + " Port Oscar " + _prose(19900)
    raw = _source("https://gov.example/long", body)
    batches = poe.extraction_batches(raw, 20000, 1500)
    assert len(batches) >= 2 and all(len(b) <= 20000 for b in batches)
    ports = _core(raw[:20000], catalog_text=raw)
    assert [p["name"] for p in ports].count("Port Oscar") == 1


def test_small_input_makes_exactly_one_call_as_before(reader):
    raw = _source("https://gov.example/small", "Ports of entry: Port Papa, Port Quebec.")
    ports = _core(raw, catalog_text=raw)
    assert len(reader) == 1 and reader[0] == raw
    assert {p["name"] for p in ports} == {"Port Papa", "Port Quebec"}


def test_catalog_window_keeps_one_call_when_it_already_fits(reader):
    raw = _source("https://gov.example/catalog", _prose(30000) + " Port Romeo.")

    async def run():
        token = llm.llm_context_chars.set(poe.CATALOG_CONTEXT_CHARS)
        try:
            return await poe._extract_ports_core(raw, ZONE, lambda m: None, catalog_text=raw)
        finally:
            llm.llm_context_chars.reset(token)

    ports = asyncio.run(run())
    assert len(reader) == 1 and "Port Romeo" in {p["name"] for p in ports}


def test_batches_cover_every_char_and_keep_source_order():
    raw = "\n\n".join([
        _source("https://gov.example/1", "Port Sierra. " + _prose(3000)),
        _source("https://gov.example/2", _prose(50000)),
        "[SYNTHÈSE DE RECHERCHE (à recouper)]\nPort Tango.",
    ])
    batches = poe.extraction_batches(raw, 20000, 1000)
    assert all(len(b) <= 20000 for b in batches)
    assert batches[0].startswith("[SOURCE: https://gov.example/1]")
    assert "Port Tango" in batches[-1]
    joined = "".join(batches)
    body = raw.split("[SOURCE: https://gov.example/2]\n", 1)[1].split("\n\n[SYN")[0]
    for i in range(0, len(body) - 200, 200):  # every slice of the long source is sent
        assert body[i:i + 200] in joined
    assert sum(b.startswith("[SOURCE: https://gov.example/2]\n") for b in batches) >= 3


def test_run_cap_limits_calls_and_keeps_list_like_batches(reader):
    raw = "\n\n".join(
        [_source(f"https://gov.example/p{i}", _prose(15000)) for i in range(3)]
        + [_source("https://gov.example/list",
                   "Designated ports of entry (customs): Port Uniform, Port Victor. " + _prose(12000))])
    snap = snapshot_for_run(mode="formalities", overrides={"formalities.extract_max_llm_calls": 2})
    rules_token, run_token = bind_rules(snap), tokenfactory.bind_run("run-p03-cap")
    logs = []
    try:
        ports = _core(raw[:20000], catalog_text=raw, logs=logs)
        assert len(reader) == 2
        assert {"Port Uniform", "Port Victor"} <= {p["name"] for p in ports}
        assert any("plafond d'appels LLM: 2/4" in m for m in logs)
        assert tokenfactory.calls_taken("run-p03-cap") == 2

        # Cap used up: the next zone sends nothing and falls back without the LLM.
        reader.clear()
        assert _core("Port Whiskey.", catalog_text="Port Whiskey.") == []
        assert reader == []
    finally:
        tokenfactory.reset_run(run_token)
        reset_rules(rules_token)


def test_cap_without_a_run_holds_per_zone(reader):
    snap = snapshot_for_run(mode="formalities", overrides={"formalities.extract_max_llm_calls": 1})
    token = bind_rules(snap)
    try:
        for _ in range(3):  # no run bound: each zone gets its own allowance
            assert {p["name"] for p in _core("Port Xray.", catalog_text="Port Xray.")} == {"Port Xray"}
        assert len(reader) == 3
    finally:
        reset_rules(token)


def test_rule_defaults_match_code_constants():
    assert catalog_default("formalities.extract_batch_chars") == llm.LLM_CONTEXT_CHARS
    assert catalog_default("formalities.extract_batch_overlap_chars") == poe.EXTRACT_BATCH_OVERLAP_CHARS
    assert catalog_default("formalities.extract_batch_concurrency") == poe.EXTRACT_BATCH_CONCURRENCY
    assert catalog_default("formalities.extract_max_llm_calls") == poe.EXTRACT_MAX_LLM_CALLS


def _collect(monkeypatch, body: str, url: str = "https://planning.gov.example/Scoping-Report.pdf"):
    async def fake_cascade(u, min_chars=200, log=None):
        return {"text": body, "html": "", "md5": "m", "level": "N1-pymupdf", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    logs: list[str] = []
    texts, _h, _used, _ex = asyncio.run(poe._collect_texts([{"url": url, "domain": "gov.example"}],
                                                           logs.append))
    return texts, logs


def test_port_named_past_60k_of_a_source_is_extracted(reader, monkeypatch):
    # Bermuda case: the designating sentence sits at offset ~71,457 of one PDF.
    body = _prose(71457) + "\nThe designated port of entry is Port Zulu.\n" + _prose(4000)
    texts, _logs = _collect(monkeypatch, body)
    assert len(texts) == 1 and "Port Zulu" in texts[0]  # no longer cut at 60,000
    raw = "\n\n".join(texts)
    assert raw.index("Port Zulu") > 60000

    context, _window, _branch = poe.extraction_context(raw, [], "ports of entry")
    names = {p["name"] for p in _core(context, catalog_text=raw)}
    assert "Port Zulu" in names
    assert all(len(s) <= llm.LLM_CONTEXT_CHARS for s in reader)


def test_source_safety_ceiling_is_a_rule(monkeypatch):
    assert catalog_default("formalities.source_max_chars") == poe.SOURCE_MAX_CHARS > 60000
    snap = snapshot_for_run(mode="formalities", overrides={"formalities.source_max_chars": 60000})
    token = bind_rules(snap)
    try:
        texts, logs = _collect(monkeypatch, _prose(70000) + " Port Yankee.")
    finally:
        reset_rules(token)
    assert "Port Yankee" not in texts[0]
    assert len(texts[0]) == len("[SOURCE: https://planning.gov.example/Scoping-Report.pdf]\n") + 60000
    assert any("plafond de sécurité 60000" in m for m in logs)


def _names(n: int) -> list[str]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    return [f"Port {'ABCDEFGHIJ'[i // 26 % 10]}{letters[i % 26]}{letters[i // 26 % 26]}" for i in range(n)]


@pytest.fixture
def capped_reader(reader, monkeypatch):
    """The stub reader, with its answer cut at the ports one answer can hold."""
    async def capped(context, zone, settings=None, log=None):
        text = llm.reader_context(context)
        reader.append(text)
        names = list(dict.fromkeys(NAME_RE.findall(text)))[:llm.extract_ports_per_answer()]
        return [{"name": n, "city": None, "note": None} for n in names]

    monkeypatch.setattr(poe, "extract_ports", capped)
    return reader


def test_default_caps_hold_about_30_ports_per_answer():
    assert llm.extract_output_tokens() == 2000 == catalog_default("formalities.extract_output_tokens")
    assert llm.extract_output_tokens(fallback=True) == 2500 == catalog_default(
        "formalities.extract_output_tokens_fallback")
    assert llm.extract_ports_per_answer() == 30


def test_dense_list_is_split_under_the_answer_cap(capped_reader):
    names = _names(90)
    raw = _source("https://customs.example/ports",
                  "Designated ports of entry:\n" + "\n".join(f"- {n}, customs and immigration" for n in names))
    assert len(raw) < llm.LLM_CONTEXT_CHARS  # fits the window: the old path made one call

    old = _core(raw)  # catalog_text=None: one call, answer cut at 30 ports
    assert len(capped_reader) == 1 and len(old) == 30

    capped_reader.clear()
    ports = _core(raw, catalog_text=raw)
    assert {p["name"] for p in ports} == set(names)
    assert len(capped_reader) >= 3
    assert all(poe.estimated_port_mentions(s) <= 30 for s in capped_reader)
    assert all(s.startswith("[SOURCE: https://customs.example/ports]\n") for s in capped_reader)


def test_lower_cap_makes_smaller_batches(capped_reader):
    names = _names(40)
    raw = _source("https://customs.example/ports", "\n".join(f"- {n}, customs and immigration" for n in names))
    assert {p["name"] for p in _core(raw, catalog_text=raw)} == set(names)
    calls_default = len(capped_reader)
    assert calls_default == 2

    capped_reader.clear()
    snap = snapshot_for_run(mode="formalities", overrides={"formalities.extract_output_tokens": 1000})
    token = bind_rules(snap)
    try:
        assert llm.extract_ports_per_answer() == 13
        assert {p["name"] for p in _core(raw, catalog_text=raw)} == set(names)
    finally:
        reset_rules(token)
    assert len(capped_reader) > calls_default


def test_prose_does_not_trigger_extra_splits():
    raw = _source("https://gov.example/law", _prose(18000) + " The port of entry is Port Alpha.")
    assert poe.estimated_port_mentions(raw) <= 2
    assert poe.fit_output_cap([raw], 30, 1500) == [raw]


def test_readers_send_the_configured_caps(monkeypatch):
    from app.core import generator, nvidia
    seen = {}

    async def fake_openrouter(prompt, system, settings, max_tokens, images=None):
        seen["openrouter"] = max_tokens
        return {"ports": []}

    async def fake_nim(system, prompt, settings, **kwargs):
        seen["nvidia"] = kwargs["max_tokens"]
        return {"ports": []}, "nvidia/test-model"

    monkeypatch.setattr(llm, "_json_openrouter", fake_openrouter)
    monkeypatch.setattr(nvidia, "complete_json_nvidia_tracked", fake_nim)

    async def readers():
        await llm.extract_ports_openrouter("Port Alpha.", ZONE, settings={})
        await nvidia.extract_ports_nvidia("Port Alpha.", ZONE, settings={})

    asyncio.run(readers())
    assert seen == {"openrouter": 2500, "nvidia": 2500}
    assert generator._output_tokens("extract") == 2000 and generator._output_tokens("judge") == 2000

    snap = snapshot_for_run(mode="formalities", overrides={"formalities.extract_output_tokens": 4000,
                                                           "formalities.extract_output_tokens_fallback": 6000})
    token = bind_rules(snap)
    try:
        asyncio.run(readers())
        assert seen == {"openrouter": 6000, "nvidia": 6000}
        assert generator._output_tokens("extract") == 4000
        assert generator._output_tokens("judge") == generator.MAX_OUTPUT_TOKENS == 2000
    finally:
        reset_rules(token)
