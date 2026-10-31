"""Code fingerprint of PoE runs — no network, no persisted secret."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import claude  # noqa: E402
from app.services import poe_pipeline as poe  # noqa: E402
from app.services.run_fingerprint import (  # noqa: E402
    build_code_fingerprint, git_state, merge_run_params, resume_params,
)


class TestGitState:
    def test_this_repo_has_sha(self):
        sha, dirty = git_state()
        assert sha and len(sha) == 40
        assert dirty in (True, False)


class TestFingerprint:
    def test_no_secrets_and_features(self, monkeypatch):
        monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("BI_TF_BASE", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-secret")
        monkeypatch.setenv("SERPER_API_KEY", "sp-secret")
        monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888/")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
        fp = build_code_fingerprint(
            {"anthropic_api_key": "sk-ant-secret", "claude_budget_usd": 12},
            zone_timeout_s=900,
        )
        blob = str(fp)
        assert "sk-or-secret" not in blob
        assert "tf-secret" not in blob
        assert "sp-secret" not in blob
        assert "sk-ant-secret" not in blob
        assert "nvapi-secret" not in blob
        assert fp["nvidia_configured"] is True
        assert fp["nvidia_model"] == "openai/gpt-oss-20b"
        assert fp["nvidia_judge_chain"][0] == "nvidia/nemotron-3-ultra-550b-a55b"
        assert fp["nvidia_judge_chain"][1] == "openai/gpt-oss-20b"
        assert "deepseek-ai/deepseek-v4-flash-0731" in fp["nvidia_judge_chain"]
        assert "deepseek-ai/deepseek-v4-pro-0813" not in fp["nvidia_judge_chain"]
        assert fp["nvidia_page_chain"] == [
            "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "openai/gpt-oss-20b",
            "meta/muse-glimmer-30b",
        ]
        assert fp["features"]["nvidia_adapter"] is True
        assert fp["openrouter_configured"] is True
        assert fp["tinyfish_configured"] is True
        assert fp["serper_configured"] is True
        assert fp["searxng_url"] == "http://127.0.0.1:8888"
        assert fp["claude_enabled"] is True
        assert fp["claude_budget_usd"] == 12
        assert fp["claude_model"] == claude.CLAUDE_MODEL
        assert fp["catalog_skip"] is True
        assert fp["features"]["p0_pdf_isolated"] is True
        assert fp["features"]["p0_geocode_cache"] is True
        assert fp["features"]["claude_adapter"] is True
        assert fp["features"]["haiku_source_coords"] is True
        assert fp["features"]["geocode_name_veto"] is True
        assert fp["features"]["haiku_geocode_tiebreak"] is True
        assert fp["features"]["geocode_name"] is True
        assert fp["features"]["llm_geocode_port"] is True
        assert fp["features"]["seed_union"] is True
        assert fp["features"]["seed_verify"] is True
        assert fp["features"]["seed_osm"] is True
        assert fp["features"]["seed_enrich"] is True
        assert fp["features"]["seed_mine_sources"] is True
        assert fp["features"]["seed_mine_sources"] is True
        assert fp["features"]["tinyfish_poe_agent"] is True
        assert fp["features"]["claude_sonnet_judge"] is True
        assert fp["features"]["seed_database"] is True
        assert fp["features"]["seed_search_query"] is True
        assert fp["features"]["wpi_counterlist"] is True
        assert fp["git_sha"]

    def test_claude_off_without_budget(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_BUDGET_USD", raising=False)
        fp = build_code_fingerprint({}, zone_timeout_s=900)
        assert fp["claude_enabled"] is False
        assert fp["claude_budget_usd"] == 0


class TestMergeResume:
    def test_merge_nests_code(self):
        params = merge_run_params({"variant": "tinyfish", "force": True},
                                  {"git_sha": "abc"})
        assert params["variant"] == "tinyfish"
        assert params["code"]["git_sha"] == "abc"

    def test_resume_keeps_origin_sha(self):
        existing = {"variant": "v2", "code": {"git_sha": "aaa"}}
        out = resume_params(existing, {"git_sha": "bbb"})
        assert out["code"]["git_sha"] == "aaa"
        assert out["resume_git_sha"] == "bbb"
        assert out["resumed"] is True

    def test_resume_same_sha_no_extra(self):
        existing = {"variant": "v2", "code": {"git_sha": "aaa"}}
        out = resume_params(existing, {"git_sha": "aaa"})
        assert "resume_git_sha" not in out


class TestExtractionEnginePreserved:
    def test_claude_tag_survives_compare(self, monkeypatch):
        async def fake_llm(context, zone, settings=None, log=None):
            return [{"name": "Port Alpha", "city": None, "note": None,
                     "extraction_engine": "claude"}]

        import app.core.ml as ml
        monkeypatch.setattr(poe, "extract_ports", fake_llm)
        monkeypatch.setattr(ml, "extract_entities", lambda text: [])
        ports = asyncio.run(poe.extract_ports_llm("ctx", {"name": "Testland"},
                                                  lambda m: None))
        assert ports[0]["extraction_engine"] == "claude"


@pytest.fixture
def fingerprint_run_db():
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _seed():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        tdb = client["bi_test_run_fingerprint"]
        await client.drop_database("bi_test_run_fingerprint")
        await tdb.eez_zones.insert_one({
            "mrgid": 42, "name": "Testland", "geoname": "Testland",
            "iso2": "TL",
        })
        return tdb

    loop = asyncio.new_event_loop()
    tdb = loop.run_until_complete(_seed())
    yield tdb, loop
    loop.run_until_complete(tdb.client.drop_database("bi_test_run_fingerprint"))
    loop.close()


class TestExecuteRunWritesCode:
    def test_params_code_on_new_run(self, fingerprint_run_db, monkeypatch):
        from app.core.tasks import TaskState
        from app.services import poe_runs

        async def fake_zone(db, mrgid, logger=None, force=False, run=None):
            await db.poe_run_ports.insert_one({
                "run_id": run.run_id, "mrgid": mrgid, "name": "Alpha",
            })
            await db.poe_run_zones.insert_one({
                "run_id": run.run_id, "mrgid": mrgid, "status": "ia",
                "poe_count": 1,
            })
            return {"status": "ia", "poe_count": 1}

        monkeypatch.setattr(poe_runs.poe, "generate_zone_poe", fake_zone)
        tdb, loop = fingerprint_run_db
        state = TaskState()
        summary = loop.run_until_complete(poe_runs.execute_run(
            tdb, state, "r-fp", label="canary", limit=0, only_zones=[42],
            concurrency=1, variant="tinyfish"))
        doc = loop.run_until_complete(tdb.poe_runs.find_one({"_id": "r-fp"}))
        assert doc["params"]["variant"] == "tinyfish"
        assert doc["params"]["code"]["git_sha"]
        assert "features" in doc["params"]["code"]
        assert "sk-" not in str(doc["params"])
        assert summary["git_sha"] == doc["params"]["code"]["git_sha"]
