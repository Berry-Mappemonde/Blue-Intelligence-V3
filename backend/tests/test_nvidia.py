"""NVIDIA NIM adapter — no network."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import llm  # noqa: E402
from app.core import nvidia  # noqa: E402
from app.services import poe_pipeline as poe  # noqa: E402
from app.services import poe_seed_enrich as enr  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# These tests cover the NIM fallback mechanics (410 skip, 429 hop, listing escalation,
# second reader) on the pre-Nemotron chains they were written for. The Nemotron heads
# of the production chains are covered in test_nemotron_suite.py.
LEGACY_CHAINS = {
    "judge": (nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL, nvidia.FLASH_MODEL),
    "extract": (nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL),
    "legal": (nvidia.LEGAL_MODEL, nvidia.SECONDARY_MODEL),
    "json": (nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL),
    "page": (nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL),
    "text": (nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL),
    "review": (nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL),
}


@pytest.fixture(autouse=True)
def legacy_chains(monkeypatch):
    for name in ("NEBIUS_API_KEY", "TF_RUN_BUDGET_USD", "TF_PRICES_JSON", "BI_TF_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(nvidia, "CHAINS", LEGACY_CHAINS)
    # No network: Nightshift (the first hop of llm.ask_json) reports no model answered.
    from unittest.mock import AsyncMock
    from app.core import generator
    monkeypatch.setattr(generator, "generate", AsyncMock(return_value={
        "provider": "sample", "text": "", "parsed": {}, "model": "deterministic-heuristic",
        "fallback_reason": "test: no Nightshift"}))


class TestParseAndLegal:
    def test_strict_json_accepts_object(self):
        assert nvidia.parse_json_strict('{"is_poe": false, "kind": "cargo"}')["kind"] == "cargo"

    def test_strict_json_rejects_thinking_dump(self):
        blob = (
            "Here's a thinking process:\n"
            '1. Output format: {"is_poe": true, "confidence": 0, "reason": "", '
            '"official_name": null, "kind": "pleasure"}\n'
        )
        assert nvidia.parse_json_strict(blob) is None

    def test_strict_json_rejects_prose_then_json(self):
        assert nvidia.parse_json_strict('Sure. {"is_poe": true}') is None

    def test_message_text_ignores_reasoning_only(self):
        assert nvidia._message_text({
            "content": None,
            "reasoning_content": '{"is_poe": true}',
        }) == ""
        assert nvidia._message_text({
            "content": '{"is_poe": false}',
        }) == '{"is_poe": false}'

    def test_json_from_reasoning_content(self):
        assert nvidia._json_from_message({
            "content": None,
            "reasoning_content": 'notes...\n{"is_poe": true, "kind": "pleasure"}',
        })["kind"] == "pleasure"
        assert nvidia._json_from_message({
            "content": '{"is_poe": false}',
            "reasoning_content": '{"is_poe": true}',
        })["is_poe"] is False

    def test_legal_gazette_fr(self):
        text = (
            "Arrêté relatif aux points de passage frontaliers maritimes pour la "
            "plaisance. Sont habilités : Dunkerque, Calais, Saint-Malo, Brest. "
            "Les formalités de police aux frontières s'y accomplissent."
        )
        assert nvidia.looks_like_legal_text(text) is True
        model, engine = nvidia.second_extract_choice(text)
        assert engine == "nvidia-kimi"
        assert model == nvidia.LEGAL_MODEL

    def test_short_or_forum_is_not_legal(self):
        assert nvidia.looks_like_legal_text("Fort Bay is nice") is False
        model, engine = nvidia.second_extract_choice(
            "Fort Bay Harbour is the official port of entry for visiting yachts. " * 4)
        assert model == nvidia.SECONDARY_MODEL
        assert engine == "nvidia-muse"

    def test_engine_label(self):
        assert nvidia.engine_label() == "nvidia-gpt-oss"
        assert nvidia.engine_label("deepseek-ai/deepseek-v4-flash-0731") == "nvidia-deepseek"
        assert nvidia.engine_label("meta/muse-glimmer-30b") == "nvidia-muse"
        assert nvidia.engine_label("moonshotai/kimi-k3") == "nvidia-kimi"
        assert nvidia.engine_label("poolside/laguna-xs-2.1") == "nvidia-laguna"
        assert nvidia.engine_label("openai/gpt-oss-20b") == "nvidia-gpt-oss"
        assert nvidia.engine_label("meta/llama-3.2-11b-vision-instruct") == "nvidia-llama"
        assert nvidia.engine_label("nvidia/nemotron-3-nano-30b-a3b") == "nvidia-nemotron"
        assert nvidia.engine_label("nvidia/nemotron-3.5-lightning-30b-a3b") == "nvidia-nemotron"

    def test_second_extract_skips_overridden_primary(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_MODEL", "other/reader")
        model, engine = nvidia.second_extract_choice(
            "Fort Bay Harbour is the official port of entry for visiting yachts. " * 4)
        assert model == nvidia.PRIMARY_MODEL
        assert engine == "nvidia-gpt-oss"


class TestProvider:
    def test_auto_uses_nvidia_when_key(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        assert nvidia.llm_provider({}) == "nvidia"
        assert nvidia.nvidia_enabled({}) is True

    def test_openrouter_forced(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        assert nvidia.nvidia_enabled({}) is False

    def test_settings_key_beats_empty_env(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "auto")
        assert nvidia.nvidia_enabled({"nvidia_api_key": "nvapi-ui"}) is True

    def test_dead_pro_rewrites_to_gpt_oss(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_MODEL", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL_CHAIN_REVIEW", raising=False)
        assert nvidia.PRO_MODEL not in nvidia.models_for("review")
        assert nvidia.PRO_MODEL not in nvidia.models_for("judge")
        monkeypatch.setenv("NVIDIA_MODEL", nvidia.PRO_MODEL)
        assert nvidia.primary_model() == nvidia.GPT_OSS_MODEL
        assert nvidia.PRO_MODEL not in nvidia.models_for("json")

    def test_eol_alias_only_not_muse(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-flash")
        monkeypatch.setenv("NVIDIA_MODEL_SECONDARY", "meta/muse-glimmer-30b")
        monkeypatch.setenv("NVIDIA_MODEL_LEGAL", "poolside/laguna-xs-2-1")
        assert nvidia.primary_model() == nvidia.FLASH_MODEL
        assert nvidia.secondary_model() == "meta/muse-glimmer-30b"
        assert nvidia.legal_model() == "poolside/laguna-xs-2.1"

    def test_json_object_omitted_for_muse_and_laguna(self):
        muse = nvidia.chat_payload(
            "meta/muse-glimmer-30b", "sys", "user", 32)
        assert "response_format" not in muse
        flash = nvidia.chat_payload(
            "deepseek-ai/deepseek-v4-flash-0731", "sys", "user", 32)
        assert flash["response_format"] == {"type": "json_object"}
        laguna = nvidia.chat_payload(
            "poolside/laguna-xs-2.1", "sys", "user", 32)
        assert "response_format" not in laguna
        gemma = nvidia.chat_payload("google/gemma-4-31b-it", "sys", "user", 32)
        assert "response_format" not in gemma

    def test_judge_chain_order(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_MODEL", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL_SECONDARY", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL_LEGAL", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL_CHAIN_JUDGE", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL_CHAIN_JSON", raising=False)
        chain = nvidia.models_for("judge")
        assert chain == (
            nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL, nvidia.FLASH_MODEL)
        assert nvidia.PRO_MODEL not in chain
        assert nvidia.models_for("extract") == (
            nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL)
        assert nvidia.LEGAL_MODEL not in nvidia.models_for("extract")
        assert nvidia.models_for("legal") == (
            nvidia.LEGAL_MODEL, nvidia.SECONDARY_MODEL)
        assert nvidia.models_for("page") == nvidia.models_for("json") == (
            nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL)
        assert nvidia.models_for("text") == nvidia.models_for("json")
        assert nvidia.models_for("review") == nvidia.models_for("json")

    def test_nvidia_model_prefixes_without_reordering(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_MODEL", "other/reader")
        monkeypatch.delenv("NVIDIA_MODEL_SECONDARY", raising=False)
        monkeypatch.delenv("NVIDIA_MODEL_CHAIN_JUDGE", raising=False)
        assert nvidia.models_for("judge") == (
            "other/reader", nvidia.GPT_OSS_MODEL,
            nvidia.SECONDARY_MODEL, nvidia.FLASH_MODEL)
        assert nvidia.models_for("legal")[0] == nvidia.LEGAL_MODEL

    def test_secondary_env_replaces_muse_slot(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_MODEL", raising=False)
        monkeypatch.setenv(
            "NVIDIA_MODEL_SECONDARY", "meta/llama-3.2-11b-vision-instruct")
        assert nvidia.models_for("json") == (
            nvidia.GPT_OSS_MODEL,
            "meta/llama-3.2-11b-vision-instruct")

    def test_chain_env_overrides_full_list(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_MODEL", raising=False)
        monkeypatch.setenv(
            "NVIDIA_MODEL_CHAIN_JSON",
            "openai/gpt-oss-20b, meta/muse-glimmer-30b")
        assert nvidia.models_for("json") == (
            nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL)
        assert nvidia.models_for("page")[0] == nvidia.PRIMARY_MODEL

    def test_payload_follows_infer_docs(self):
        extras = nvidia.generation_extras("meta/muse-glimmer-30b")
        assert extras["reasoning_effort"] == "low"
        assert extras["chat_template_kwargs"]["reasoning_strength"] == "low"
        muse = nvidia.chat_payload("meta/muse-glimmer-30b", "sys", "user", 32)
        assert muse["temperature"] == 0.95
        assert muse["top_p"] == 1.0
        assert "response_format" not in muse

        kimi = nvidia.chat_payload("moonshotai/kimi-k3", "sys", "user", 32)
        assert kimi["temperature"] == 1.0
        assert kimi["reasoning_effort"] == "low"
        assert "top_p" not in kimi  # not exposed on kimi-k3-infer
        kimi_legal = nvidia.chat_payload(
            "moonshotai/kimi-k3", "sys", "user", 32, role="legal")
        assert kimi_legal["reasoning_effort"] == "high"

        pro = nvidia.chat_payload("deepseek-ai/deepseek-v4-pro-0813", "sys", "user", 32)
        assert pro["model"] == nvidia.GPT_OSS_MODEL
        assert pro["temperature"] == 0.6
        assert pro["top_p"] == 0.7
        assert pro["response_format"] == {"type": "json_object"}

        oss = nvidia.chat_payload("openai/gpt-oss-20b", "sys", "user", 32)
        assert oss["reasoning_effort"] == "low"
        assert oss["temperature"] == 0.6
        assert oss["top_p"] == 0.7
        assert "chat_template_kwargs" not in oss

        flash = nvidia.chat_payload(
            "deepseek-ai/deepseek-v4-flash-0731", "sys", "user", 32)
        assert flash["reasoning_effort"] == "none"
        assert flash["chat_template_kwargs"]["thinking"] is False
        assert flash["chat_template_kwargs"]["reasoning_effort"] == "none"

        laguna = nvidia.chat_payload(
            "poolside/laguna-xs-2.1", "sys", "user", 32)
        assert laguna["temperature"] == 1.0
        assert laguna["top_p"] == 0.95
        assert "chat_template_kwargs" not in laguna
        assert "reasoning_effort" not in laguna
        assert "thinking" not in laguna


class TestJudgeNvidia:
    def test_listing_escalates_to_next_nim(self, monkeypatch):
        models = []

        async def fake_complete(system, user, settings=None, *, model=None, **k):
            models.append(model)
            return {"is_poe": True, "confidence": 40, "kind": "pleasure",
                    "reason": "flash"}

        monkeypatch.setattr(nvidia, "nvidia_enabled", lambda s=None: True)
        monkeypatch.setattr(nvidia, "complete_json_nvidia", fake_complete)

        out = _run(enr._judge_llm(
            {"name": "Nouméa", "seed_sources": ["listing"]},
            {"name": "NC"}, "extrait officiel", {}, lambda m: None))
        assert models == [nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL]
        assert out["judge_engine"] == "nvidia-muse"
        assert out["judge_status"] == "accepted"

    def test_failure_skips_to_next_model(self, monkeypatch):
        models = []

        async def fake_complete(system, user, settings=None, *, model=None, **k):
            models.append(model)
            if model == nvidia.PRIMARY_MODEL:
                raise RuntimeError("nvidia HTTP 410: gone")
            return {"is_poe": True, "confidence": 90, "kind": "pleasure",
                    "reason": "muse"}

        monkeypatch.setattr(nvidia, "nvidia_enabled", lambda s=None: True)
        monkeypatch.setattr(nvidia, "complete_json_nvidia", fake_complete)

        out = _run(enr._judge_llm(
            {"name": "Port Commerce", "seed_sources": ["v1"]},
            {"name": "XX"}, "clearance yachts", {}, lambda m: None))
        assert models[0] == nvidia.PRIMARY_MODEL
        assert models[1] == nvidia.SECONDARY_MODEL
        assert out["judge_engine"] == "nvidia-muse"
        assert out["judge_status"] == "accepted"

    def test_listing_hops_after_overridden_head(self, monkeypatch):
        models = []

        async def fake_complete(system, user, settings=None, *, model=None, **k):
            models.append(model)
            if model == nvidia.PRIMARY_MODEL:
                return {"is_poe": True, "confidence": 90, "kind": "pleasure",
                        "reason": "pro"}
            return {"is_poe": True, "confidence": 40, "kind": "pleasure",
                    "reason": "other"}

        monkeypatch.setenv("NVIDIA_MODEL", "other/reader")
        monkeypatch.setattr(nvidia, "nvidia_enabled", lambda s=None: True)
        monkeypatch.setattr(nvidia, "complete_json_nvidia", fake_complete)

        out = _run(enr._judge_llm(
            {"name": "Nouméa", "seed_sources": ["listing"]},
            {"name": "NC"}, "extrait officiel", {}, lambda m: None))
        assert models == ["other/reader", nvidia.PRIMARY_MODEL]
        assert out["judge_engine"] == "nvidia-gpt-oss"
        assert out["judge_status"] == "accepted"

    def test_cargo_stays_on_primary(self, monkeypatch):
        models = []

        async def fake_complete(system, user, settings=None, *, model=None, **k):
            models.append(model)
            return {"is_poe": True, "confidence": 90, "kind": "cargo",
                    "reason": "terminal"}

        async def boom(*a, **k):
            raise AssertionError("OpenRouter should not run")

        monkeypatch.setattr(nvidia, "nvidia_enabled", lambda s=None: True)
        monkeypatch.setattr(nvidia, "complete_json_nvidia", fake_complete)
        monkeypatch.setattr(llm, "_json_openrouter", boom)

        out = _run(enr._judge_llm(
            {"name": "Port Commerce", "seed_sources": ["v1"]},
            {"name": "XX"}, "terminal conteneur", {}, lambda m: None))
        assert models == [nvidia.PRIMARY_MODEL]
        assert out["judge_status"] == "rejected"
        assert out["judge_engine"] == "nvidia-gpt-oss"


class TestExtractSecondReader:
    def test_gpt_oss_parallel_and_agreement(self, monkeypatch):
        async def fake_primary(context, zone, settings=None, log=None):
            return [{"name": "Fort Bay", "city": None, "note": None,
                     "extraction_engine": "nvidia-deepseek"}]

        async def fake_second(context, zone, settings=None, log=None,
                              model=None, engine="nvidia-gpt-oss", **k):
            assert engine == "nvidia-gpt-oss"
            assert model == nvidia.GPT_OSS_MODEL
            return [{"name": "Fort Bay", "extraction_engine": engine},
                    {"name": "Cinta Oil Terminal", "extraction_engine": engine}]

        import app.core.ml as ml
        monkeypatch.setattr(nvidia, "nvidia_enabled", lambda s=None: True)
        monkeypatch.setattr(nvidia, "second_extract_choice",
                            lambda ctx: (nvidia.GPT_OSS_MODEL, "nvidia-gpt-oss"))
        monkeypatch.setattr(poe, "extract_ports", fake_primary)
        monkeypatch.setattr(nvidia, "extract_ports_nvidia", fake_second)
        monkeypatch.setattr(ml, "extract_entities", lambda text: [])

        ports = _run(poe.extract_ports_llm(
            "ctx", {"name": "Testland"}, lambda m: None, settings={"x": 1}))
        by = {p["name"]: p for p in ports}
        assert by["Fort Bay"]["extraction_engine"] == "nvidia-deepseek"
        assert by["Fort Bay"]["claude_agreement"] is True
        assert by["Cinta Oil Terminal"]["extraction_engine"] == "nvidia-gpt-oss"
        assert "vu par nvidia-gpt-oss" in (by["Cinta Oil Terminal"].get("note") or "")


class TestTrackedFallback:
    def test_json_chain_skips_410_to_muse(self, monkeypatch):
        calls = []

        async def fake_one(key, payload, *, max_tokens, log=None):
            calls.append(payload["model"])
            if payload["model"] == nvidia.GPT_OSS_MODEL:
                raise RuntimeError("nvidia HTTP 410: gone")
            assert payload["model"] == nvidia.SECONDARY_MODEL
            assert "response_format" not in payload
            return {"ok": True}

        monkeypatch.setattr(nvidia, "_complete_one", fake_one)
        monkeypatch.setattr(nvidia, "get_nvidia_key", lambda s=None: "k")
        data, used = _run(nvidia.complete_json_nvidia_tracked(
            "sys", "user", role="json"))
        assert used == nvidia.SECONDARY_MODEL
        assert data == {"ok": True}
        assert calls == [nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL]

    def test_json_chain_reaches_muse_without_json_object(self, monkeypatch):
        calls = []
        gone = {nvidia.GPT_OSS_MODEL}

        async def fake_one(key, payload, *, max_tokens, log=None):
            calls.append(payload["model"])
            if payload["model"] in gone:
                raise RuntimeError("nvidia HTTP 410: gone")
            assert payload["model"] == nvidia.SECONDARY_MODEL
            assert "response_format" not in payload
            return {"ok": True}

        monkeypatch.setattr(nvidia, "_complete_one", fake_one)
        monkeypatch.setattr(nvidia, "get_nvidia_key", lambda s=None: "k")
        data, used = _run(nvidia.complete_json_nvidia_tracked(
            "sys", "user", role="json"))
        assert used == nvidia.SECONDARY_MODEL
        assert data == {"ok": True}
        assert calls == [
            nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL,
        ]

    def test_complete_one_429_does_not_retry_same_model(self, monkeypatch):
        sleeps = []
        posts = []

        async def fake_sleep(s):
            sleeps.append(s)

        class _Resp:
            status_code = 429
            text = "rate limited"

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                posts.append(1)
                return _Resp()

        monkeypatch.setattr(nvidia.httpx, "AsyncClient", _Client)
        monkeypatch.setattr(nvidia.asyncio, "sleep", fake_sleep)
        with pytest.raises(RuntimeError, match="429"):
            _run(nvidia._complete_one(
                "k", {"model": nvidia.PRIMARY_MODEL}, max_tokens=8))
        assert posts == [1]
        assert sleeps == []

    def test_json_chain_429_hops_to_gpt_oss(self, monkeypatch):
        calls = []

        async def fake_one(key, payload, *, max_tokens, log=None):
            calls.append(payload["model"])
            if payload["model"] == nvidia.GPT_OSS_MODEL:
                raise RuntimeError("nvidia HTTP 429: rate limited")
            return {"ok": True}

        monkeypatch.setattr(nvidia, "_complete_one", fake_one)
        monkeypatch.setattr(nvidia, "get_nvidia_key", lambda s=None: "k")
        data, used = _run(nvidia.complete_json_nvidia_tracked(
            "sys", "user", role="json"))
        assert used == nvidia.SECONDARY_MODEL
        assert data == {"ok": True}
        assert calls == [nvidia.GPT_OSS_MODEL, nvidia.SECONDARY_MODEL]


class TestAskJsonCascade:
    def test_nvidia_then_openrouter_then_claude(self, monkeypatch):
        from app.core import claude, llm

        order = []

        async def nv_fail(*a, **k):
            order.append("nvidia")
            raise RuntimeError("nvidia down")

        async def or_fail(*a, **k):
            order.append("openrouter")
            raise RuntimeError("or down")

        async def claude_ok(*a, **k):
            order.append("claude")
            return {"ok": True}

        monkeypatch.setattr(nvidia, "nvidia_enabled", lambda s=None: True)
        monkeypatch.setattr(nvidia, "complete_json_nvidia_tracked", nv_fail)
        monkeypatch.setattr(llm, "get_llm_key", lambda s=None: "sk-or")
        monkeypatch.setattr(llm, "_json_openrouter", or_fail)
        monkeypatch.setattr(claude, "claude_enabled", lambda s=None: True)
        monkeypatch.setattr(claude, "budget_allows_call", lambda s=None: True)
        monkeypatch.setattr(claude, "complete_json_claude", claude_ok)

        data, engine = _run(llm.ask_json_tracked("p"))
        assert data == {"ok": True}
        assert engine == "claude"
        assert order == ["nvidia", "openrouter", "claude"]

    def test_gatekeeper_label_is_nvidia(self, monkeypatch):
        from app.core import llm
        from app.core.judge import YesNo

        async def fake_yes(*a, **k):
            return YesNo(
                accepted=True, url=None, reason="reef",
                engine="nvidia-deepseek",
                raw={"marine": True, "score": 0.9, "reason": "reef"},
            )

        monkeypatch.setattr(llm, "has_llm", lambda s=None: True)
        monkeypatch.setattr("app.core.judge.ask_yes_no", fake_yes)
        import app.core.ml as ml
        monkeypatch.setattr(ml, "predict_relevance", lambda t: None)

        out = _run(llm.gatekeeper_check(
            "Reef restore", "coral reef ocean marine sea coast", {}))
        assert out["engine"] == "NVIDIA Gatekeeper"
        assert out["accepted"] is True


class TestCompleteText:
    def test_text_payload_omits_json_object(self):
        body = nvidia.chat_payload(
            nvidia.SECONDARY_MODEL, "sys", "user", 32, json_object=False, role="text")
        assert "response_format" not in body
        assert body["temperature"] == 0.95


class TestMarinaNvidiaFirst:
    def test_nvidia_hit_skips_openrouter(self, monkeypatch):
        from app.services import marina_enrich as me

        async def urls(*a, **k):
            return ["https://minimes.port.fr"]

        async def pages(u, tinyfish_key=None, logger=None):
            return [{"url": u[0], "title": "Minimes",
                     "text": "The marina welcomes visiting yachts."}]

        async def fake_nv(*a, **k):
            return {"canal_vhf": "9", "places_visiteurs": 320,
                    "tirant_eau_max_metres": 3.5, "score_protection_meteo": None,
                    "services_disponibles": ["eau"], "telephone_capitainerie": "05",
                    "resume_avis": None}

        async def boom_or(*a, **k):
            raise AssertionError("OpenRouter must not run after NVIDIA hit")

        async def boom_tf(*a, **k):
            raise AssertionError("TinyFish must not run after NVIDIA hit")

        monkeypatch.setattr(me, "discover_marina_urls", urls)
        monkeypatch.setattr(me, "fetch_marina_pages", pages)
        monkeypatch.setattr(me, "enrich_via_nvidia", fake_nv)
        monkeypatch.setattr(me, "enrich_via_openrouter", boom_or)
        monkeypatch.setattr(me, "enrich_via_tinyfish", boom_tf)
        out = _run(me.enrich_marina(
            {"name": "Minimes", "lat": 46.1, "lon": -1.1, "tags": {}},
            tinyfish_key="tf", openrouter_key="or", settings={"nvidia_api_key": "nv"}))
        assert out["enrichment_source"] == "nvidia"
        assert out["canal_vhf"] == "9"
