"""v2 reconstruction tests — hygiene (interstitial filter, block detection),
parallel-compare (dual parsing, dual geocoding, LLM∥NER), event
journal, port-by-port diff and run report.

No server or network required. Mongo tests use a dedicated database
(bi_test_poe_v2), destroyed at the end.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import geo  # noqa: E402
from app.core.events import RunRecorder, RUNS_DIR  # noqa: E402


@pytest.fixture(autouse=True)
def _keep_unit_tests_off_nvidia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
from app.core.extract import dual_parse_html, looks_blocked, serp_filter  # noqa: E402
from app.services import poe_pipeline as poe  # noqa: E402
from app.services.poe_diff import diff_ports  # noqa: E402
from app.services.poe_report import report_to_markdown  # noqa: E402


# --- Hygiene: anti-interstitial SERP filter ----------------------------------
class TestSerpFilterInterstitials:
    def test_unblock_and_challenge_urls_rejected(self):
        results = [
            {"url": "https://unblock.federalregister.gov/"},
            {"url": "https://example.gov/cdn-cgi/challenge-platform/x"},
            {"url": "https://geo.captcha-delivery.com/captcha/?initialCid=x"},
            {"url": "https://www.douane.gouv.fr/demarche/ports-entree"},
        ]
        kept = [r["url"] for r in serp_filter(results)]
        assert kept == ["https://www.douane.gouv.fr/demarche/ports-entree"]

    def test_regressions_still_filtered(self):
        results = [
            {"url": "https://www.tripadvisor.com/Attractions-g147-Fiji.html"},
            {"url": "https://www.tiktok.com/@port"},
            {"url": "https://customs.gov.fj/ports-of-entry"},
        ]
        kept = [r["url"] for r in serp_filter(results)]
        assert kept == ["https://customs.gov.fj/ports-of-entry"]

    def test_dictionary_and_tcp_port_sites_rejected(self):
        results = [
            {"url": "https://twominenglish.com/official-meaning/"},
            {"url": "https://www.askdifference.com/official-vs-unofficial/"},
            {"url": "https://www.vocabulary.com/dictionary/official"},
            {"url": "https://www.guiahardware.es/puertos-de-red-mas-conocidos/"},
            {"url": "https://www.stationx.net/common-ports-cheat-sheet/"},
            {"url": "https://www.douane.gouv.fr/demarche/ports-entree"},
        ]
        kept = [r["url"] for r in serp_filter(results)]
        assert kept == ["https://www.douane.gouv.fr/demarche/ports-entree"]


# --- Hygiene: block-page detection -------------------------------------------
class TestLooksBlocked:
    def test_cloudflare_challenge_detected(self):
        text = "Just a moment... Enable JavaScript and cookies to continue"
        assert looks_blocked(text, html="<html>cf-browser-verification</html>",
                             title="Just a moment...") is True

    def test_unblock_interstitial_detected(self):
        text = ("Request unsuccessful. Please complete the security check. "
                "Automated access to this site is forbidden.")
        assert looks_blocked(text) is True

    def test_legit_long_customs_page_not_blocked(self):
        text = ("The designated ports of entry for foreign pleasure craft are "
                "Papeete, Uturoa and Taiohae. " * 60)  # > 1500 chars, aucun marqueur
        assert looks_blocked(text) is False

    def test_long_text_with_marker_not_blocked(self):
        # Long genuine regulatory content that cites “access denied” stays accepted
        text = ("Access to this page has been denied in the past for vessels without "
                "clearance. The full regulation follows. " * 40)
        assert looks_blocked(text) is False


# --- Parallel: compared dual parsing -----------------------------------------
class TestDualParse:
    def test_dual_parse_returns_comparison_signals(self):
        html = ("<html><head><title>Ports</title></head><body><article>"
                + "<p>The official ports of entry are Alpha Harbour and Beta Marina. "
                  "Foreign yachts must clear customs on arrival.</p>" * 12
                + "</article></body></html>")
        res = asyncio.run(dual_parse_html(html))
        assert res["text"]
        assert res["level"] in ("N1-trafilatura", "N2-readability")
        assert res["n1_chars"] >= 0 and res["n2_chars"] > 0
        assert 0.0 <= res["similarity"] <= 1.0


# --- Parallel: dual Nominatim ∥ GeoNames geocoding ---------------------------
class TestGeocodeDual:
    def _run(self, monkeypatch, nomi_rows, geon_rows):
        async def fake_nomi(query, country_code=None, limit=1):
            return nomi_rows

        async def fake_geon(query, country_code=None, limit=1):
            return geon_rows

        monkeypatch.setattr(geo, "_nominatim_rows", fake_nomi)
        monkeypatch.setattr(geo, "_geonames_rows", fake_geon)
        monkeypatch.setenv("GEONAMES_USERNAME", "test")
        monkeypatch.setattr(geo, "_geonames_disabled_reason", None)
        port = {"name": "Port Alpha", "city": None}
        zone = {"name": "Testland", "iso2": "TL", "sovereign": "Testland"}
        return asyncio.run(geo.geocode_port_dual(port, zone))

    def test_agreement_under_2km(self, monkeypatch):
        res = self._run(monkeypatch,
                        [{"lat": "10.0", "lon": "20.0", "display_name": "Port Alpha",
                          "class": "harbour", "type": "harbour"}],
                        [{"lat": "10.001", "lng": "20.001", "name": "Port Alpha",
                          "fcode": "HBR"}])
        assert res["nominatim"] == [10.0, 20.0]
        assert res["geonames"] == [10.001, 20.001]
        assert res["agree"] is True
        assert res["agreement_km"] < 2.0
        assert res["nominatim_meta"]["osm_type"] == "harbour"
        assert res["geonames_meta"]["geonames_fcode"] == "HBR"

    def test_disagreement_flagged(self, monkeypatch):
        res = self._run(monkeypatch,
                        [{"lat": "10.0", "lon": "20.0", "display_name": "Port Alpha"}],
                        [{"lat": "11.0", "lng": "21.0", "name": "Port Alpha"}])
        assert res["agree"] is False
        assert res["agreement_km"] > 100

    def test_single_provider_neutral(self, monkeypatch):
        res = self._run(monkeypatch,
                        [{"lat": "10.0", "lon": "20.0", "display_name": "Port Alpha"}],
                        [])
        assert res["agree"] is None
        assert res["agreement_km"] is None
        assert res["nominatim"] and not res["geonames"]


# --- Parallel: LLM ∥ NER extraction -------------------------------------------
class FakeRec:
    def __init__(self):
        self.events = []

    async def event(self, step, **payload):
        self.events.append({"step": step, **payload})


class TestExtractionCompare:
    def test_agreement_flags_and_ner_only_candidates(self, monkeypatch):
        async def fake_llm(context, zone, settings=None, log=None):
            return [{"name": "Port Alpha", "city": None, "note": None},
                    {"name": "Zeta Marina", "city": None, "note": None}]

        import app.core.ml as ml
        monkeypatch.setattr(poe, "extract_ports", fake_llm)
        monkeypatch.setattr(ml, "extract_entities",
                            lambda text: [{"text": "Port Alpha", "label": "PORT_NAME"},
                                          {"text": "Beta Quay", "label": "PORT_NAME"}])
        rec = FakeRec()
        ports = asyncio.run(poe.extract_ports_llm("ctx", {"name": "Testland"},
                                                  lambda m: None, rec=rec))
        by_name = {p["name"]: p for p in ports}
        assert by_name["Port Alpha"]["extraction_agreement"] is True
        assert by_name["Zeta Marina"]["extraction_agreement"] is False
        ev = next(e for e in rec.events if e["step"] == "extraction_compare")
        assert ev["ner_only"] == ["Beta Quay"]
        assert ev["both"] == ["Port Alpha"]

    def test_empty_ner_is_neutral_not_disagreement(self, monkeypatch):
        async def fake_llm(context, zone, settings=None, log=None):
            return [{"name": "Port Alpha", "city": None, "note": None}]

        import app.core.ml as ml
        monkeypatch.setattr(poe, "extract_ports", fake_llm)
        monkeypatch.setattr(ml, "extract_entities", lambda text: [])  # NER muet
        ports = asyncio.run(poe.extract_ports_llm("ctx", {"name": "Testland"},
                                                  lambda m: None))
        assert ports[0]["extraction_agreement"] is None

    def test_llm_failure_falls_back_to_ner(self, monkeypatch):
        async def broken_llm(context, zone, settings=None, log=None):
            raise RuntimeError("no key")

        import app.core.ml as ml
        import app.core.claude as claude_mod
        monkeypatch.setattr(poe, "extract_ports", broken_llm)
        monkeypatch.setattr(claude_mod, "claude_enabled", lambda s=None: False)
        monkeypatch.setattr(ml, "extract_entities",
                            lambda text: [{"text": "Gamma Wharf", "label": "PORT_NAME"}])
        ports = asyncio.run(poe.extract_ports_llm("ctx", {"name": "Testland"},
                                                  lambda m: None))
        assert len(ports) == 1
        assert ports[0]["name"] == "Gamma Wharf"
        assert ports[0]["extraction_engine"] == "ner"


# --- Event journal (JSONL, no Mongo) -------------------------------------------
class TestRunRecorder:
    def test_jsonl_written_with_sequence(self):
        rec = RunRecorder("testrun-jsonl", db=None)
        path = RUNS_DIR / "testrun-jsonl.jsonl"
        try:
            asyncio.run(rec.event("zone_start", mrgid=1, zone="Z", whitelist=["gov.tl"]))
            asyncio.run(rec.event("fetch", mrgid=1, zone="Z", url="https://x", chars=12))
            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            assert [l["seq"] for l in lines] == [1, 2]
            assert lines[0]["step"] == "zone_start"
            assert lines[0]["payload"]["whitelist"] == ["gov.tl"]
        finally:
            path.unlink(missing_ok=True)

    def test_long_payload_clipped(self):
        rec = RunRecorder("testrun-clip", db=None)
        path = RUNS_DIR / "testrun-clip.jsonl"
        try:
            asyncio.run(rec.event("llm", raw="x" * 10000))
            line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            assert len(line["payload"]["raw"]) < 5000
        finally:
            path.unlink(missing_ok=True)


# --- Port-by-port diff ------------------------------------------------------------
def _port(mrgid, name, lat=None, lon=None, sources=None, zone="TestZone"):
    from app.core.dedup import normalize_name
    return {"mrgid": mrgid, "zone_name": zone, "name": name, "lat": lat, "lon": lon,
            "source_urls": sources or [], "validated": lat is not None,
            "dedup_key": f"{mrgid}:{normalize_name(name)}"}


class TestDiffPorts:
    def test_categories(self):
        v1 = [
            _port(100, "Port Alpha", 10.0, 20.0, ["https://gov.tl/a"]),
            _port(100, "Port Beta", 10.5, 20.5, ["https://gov.tl/b"]),
            _port(100, "Port Gamma", 11.0, 21.0, ["https://gov.tl/c"]),
            _port(100, "Port Delta", 12.0, 22.0, ["https://gov.tl/d"]),
        ]
        v2 = [
            _port(100, "Port Alpha", 10.0, 20.0, ["https://gov.tl/a"]),      # unchanged
            _port(100, "Beta Port", 10.5, 20.5, ["https://gov.tl/b"]),       # renamed
            _port(100, "Port Gamma", 11.1, 21.0, ["https://douane.tl/c"]),   # moved + re-sourced
            _port(100, "Port Epsilon", 13.0, 23.0, ["https://gov.tl/e"]),    # nouveau
        ]
        d = diff_ports(v1, v2, moved_km=2.0)
        s = d["summary"]
        assert s["matched"] == 3
        assert s["unchanged"] == 1
        assert s["renamed"] == 1
        assert s["moved"] == 1
        assert s["resourced"] == 1
        assert s["added"] == 1 and d["added"][0]["name"] == "Port Epsilon"
        assert s["removed"] == 1 and d["removed"][0]["name"] == "Port Delta"
        gamma = next(m for m in d["matched"] if m["v2"]["name"] == "Port Gamma")
        assert set(gamma["flags"]) == {"moved", "resourced"}
        assert gamma["move_km"] > 2.0

    def test_zones_isolated(self):
        # Two homonym ports in two different zones do not match
        v1 = [_port(100, "Port Alpha", 10.0, 20.0)]
        v2 = [_port(200, "Port Alpha", 10.0, 20.0)]
        d = diff_ports(v1, v2)
        assert d["summary"]["matched"] == 0
        assert d["summary"]["added"] == 1 and d["summary"]["removed"] == 1


# --- Run report (dedicated Mongo) + markdown render -------------------------------
@pytest.fixture(scope="module")
def seeded_db():
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _seed():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        tdb = client["bi_test_poe_v2"]
        await client.drop_database("bi_test_poe_v2")
        await tdb.poe_runs.insert_one({
            "_id": "r1", "label": "test", "state": "done",
            "zones_total": 1, "zones_done": 1, "created_at": "2026-08-28T00:00:00Z",
            "started_at": "2026-08-28T00:00:00Z", "finished_at": "2026-08-28T00:10:00Z"})
        await tdb.poe_run_zones.insert_one({
            "run_id": "r1", "mrgid": 100, "name": "TestZone", "status": "ia", "poe_count": 2})
        await tdb.poe_run_ports.insert_many([
            {**_port(100, "Port Alpha", 10.0, 20.0, ["https://gov.tl/a"]), "run_id": "r1",
             "extraction_agreement": True, "geocode_agree": True},
            {**_port(100, "Port Epsilon", 13.0, 23.0, ["https://gov.tl/e"]), "run_id": "r1",
             "extraction_agreement": False, "geocode_agree": None},
        ])
        await tdb.poe_ports.insert_many([
            _port(100, "Port Alpha", 10.0, 20.0, ["https://gov.tl/a"]),
            _port(100, "Port Delta", 12.0, 22.0, ["https://gov.tl/d"]),
        ])
        await tdb.poe_run_events.insert_many([
            {"run_id": "r1", "seq": 1, "step": "search", "mrgid": 100, "zone": "TestZone",
             "payload": {"engine": "searxng", "lang": "en", "n": 5, "results": []}},
            {"run_id": "r1", "seq": 1.1, "step": "search", "mrgid": 100, "zone": "TestZone",
             "payload": {"engine": "tinyfish", "lang": "en", "n": 3, "results": []}},
            {"run_id": "r1", "seq": 1.2, "step": "search_compare", "mrgid": 100, "zone": "TestZone",
             "payload": {"n_a": 5, "n_b": 3, "jaccard_domains": 0.5, "discordant": False}},
            {"run_id": "r1", "seq": 2, "step": "gatekeeper", "mrgid": 100, "zone": "TestZone",
             "payload": {"official": ["gov.tl"], "rejected": ["blog.tl"], "strictly_official": True}},
            {"run_id": "r1", "seq": 3, "step": "fetch", "mrgid": 100, "zone": "TestZone",
             "payload": {"url": "https://gov.tl/a", "level": "N1-trafilatura", "chars": 900,
                         "blocked": False, "render_used": False,
                         "parse": {"n1_chars": 900, "n2_chars": 850, "similarity": 0.9, "agree": True}}},
            {"run_id": "r1", "seq": 4, "step": "extraction_compare", "mrgid": 100, "zone": "TestZone",
             "payload": {"llm_n": 2, "ner_n": 1, "both": ["Port Alpha"],
                         "llm_only": ["Port Epsilon"], "ner_only": []}},
            {"run_id": "r1", "seq": 5, "step": "geocode", "mrgid": 100, "zone": "TestZone",
             "payload": {"port": "Port Alpha", "nominatim": [10.0, 20.0], "geonames": [10.001, 20.001],
                         "agreement_km": 0.15, "agree": True, "chosen": "nominatim",
                         "arbitration": "agree", "geonames_available": True}},
            {"run_id": "r1", "seq": 6, "step": "zone_done", "mrgid": 100, "zone": "TestZone",
             "payload": {"status": "ia", "poe_count": 2, "duration_s": 42.5}},
        ])
        return tdb

    loop = asyncio.new_event_loop()
    tdb = loop.run_until_complete(_seed())
    yield tdb, loop
    loop.run_until_complete(tdb.client.drop_database("bi_test_poe_v2"))
    loop.close()


class TestRunReport:
    def test_report_structure_and_diff(self, seeded_db):
        from app.services.poe_report import build_run_report
        tdb, loop = seeded_db
        rep = loop.run_until_complete(build_run_report(tdb, "r1"))
        assert rep["run"]["run_id"] == "r1"
        assert rep["search"]["by_engine"]["searxng"] == 1
        assert rep["search"]["by_engine"]["tinyfish"] == 1
        assert rep["search"]["by_engine"]["tinyfish_scoped"] == 0
        assert rep["search"]["zones_with_tinyfish_results"] == 1
        assert rep["search"]["zones_search_discordant"] == 0
        assert rep["gatekeeper"]["strictly_official"] == 1
        assert rep["fetch"]["by_level"] == {"N1-trafilatura": 1}
        assert rep["extraction"]["confirmed_llm_and_ner"] == 1
        assert rep["geocode"]["agree"] == 1
        assert rep["ports"]["total"] == 2
        assert rep["zones"]["duration_s"]["n"] == 1
        ds = rep["diff"]["summary"]
        assert ds["matched"] == 1 and ds["added"] == 1 and ds["removed"] == 1

    def test_markdown_rendering(self, seeded_db):
        from app.services.poe_report import build_run_report
        tdb, loop = seeded_db
        rep = loop.run_until_complete(build_run_report(tdb, "r1"))
        md = report_to_markdown(rep)
        assert "# Rapport de run PoE" in md
        assert "Comparaison port par port" in md
        assert "Géocodage double" in md
        assert "TinyFish" in md


# --- Auto discovery (source catalogue + seeds, not a frozen list) ------------
class TestStructuredDiscovery:
    # Real r.jina.ai format (bold), not a test markdown table.
    _MX_JINA = """
### **Puertos habilitados**
#### [1.-](https://www.gob.mx/puertosymarinamercante/acciones-y-programas/puertos-y-terminales#)Bahía Colonet
**Entidad federativa:**Baja California
**Latitud:**30.96571843
**Longitud:**-116.2804389
#### 4.- Ensenada
**Entidad federativa:**Baja California
**Latitud:**31.8522146
**Longitud:**-116.625788
**Tipo de actividad:**Comercial Pesquera Turística
#### 34.- Manzanillo
**Entidad federativa:**Colima
**Latitud:**19.057546
**Longitud:**-104.313762
**Tipo de actividad:**Comercial Turística
"""
    _MX_TABLE = """
#### 4.- Ensenada
| Entidad federativa: | Baja California |
| Latitud: | 31.8522146 |
| Longitud: | -116.625788 |
"""

    def test_catalog_reads_jina_bold_and_markdown_table(self):
        from app.core.extract import extract_structured_ports, looks_like_port_catalog
        ports = extract_structured_ports(self._MX_JINA)
        by = {p["name"]: p for p in ports}
        assert "Bahía Colonet" in by and "Ensenada" in by and "Manzanillo" in by
        assert abs(by["Ensenada"]["lat"] - 31.8522146) < 1e-6
        assert abs(by["Bahía Colonet"]["lat"] - 30.96571843) < 1e-6
        assert by["Ensenada"]["city"] == "Baja California"
        assert by["Ensenada"]["extraction_engine"] == "catalog"
        table = extract_structured_ports(self._MX_TABLE)
        assert table and abs(table[0]["lat"] - 31.8522146) < 1e-6
        long = self._MX_JINA + "\n".join(
            f"#### {i}.- Puerto{i}\n**Latitud:**1.0\n**Longitud:**1.0"
            for i in range(10, 20))
        assert looks_like_port_catalog(long)

    def test_sufficient_catalog_skips_llm(self, monkeypatch):
        called = []

        async def boom(context, zone, settings=None, log=None):
            called.append(1)
            return []

        monkeypatch.setattr(poe, "extract_ports", boom)

        async def _run():
            return await poe.extract_ports_llm(
                "slice trop court", {"name": "Mexico"}, lambda m: None,
                catalog_text=self._MX_JINA)

        ports = asyncio.run(_run())
        assert called == []
        names = {p["name"] for p in ports}
        assert "Ensenada" in names and "Manzanillo" in names

    def test_name_only_catalog_skip_keeps_ports(self, monkeypatch):
        """PPF / kartelë / SIS: skip LLM but keep the names (not 0 ports)."""
        called = []

        async def boom(context, zone, settings=None, log=None):
            called.append(1)
            return []

        monkeypatch.setattr(poe, "extract_ports", boom)
        text = """
        Liste des ports de plaisance éligibles
        Haut de France
        Calais
        Port de plaisance de Calais
        Calais
        PAF
        Haut de France
        Dunkerque
        Dunkerque Marina
        Dunkerque
        PAF
        Normandie
        Dieppe
        Port de plaisance de Dieppe
        Dieppe
        Douane
        Bretagne
        Saint-Malo
        Saint-Malo Plaisance
        Saint Malo
        PAF
        """
        async def _run():
            return await poe.extract_ports_llm(
                "slice trop court", {"name": "France"}, lambda m: None,
                catalog_text=text)

        ports = asyncio.run(_run())
        assert called == []
        names = " ".join(p["name"] for p in ports)
        assert "Calais" in names and "Dunkerque Marina" in names
        assert all(p.get("lat") is None for p in ports)

    def test_catalog_reads_full_text_not_llm_slice(self, monkeypatch):
        async def _no_llm(context, zone, log=None):
            return []
        monkeypatch.setattr(poe, "extract_ports", _no_llm)
        async def _run():
            return await poe.extract_ports_llm(
                self._MX_JINA[:80], {"name": "Mexico"}, lambda m: None,
                catalog_text=self._MX_JINA)
        ports = asyncio.run(_run())
        names = {p["name"] for p in ports}
        assert "Bahía Colonet" in names and "Ensenada" in names

    def test_catalog_per_source_keeps_last_port(self):
        from app.core.extract import extract_structured_ports
        joined = (
            f"[SOURCE: https://gob.mx/page]\n{self._MX_JINA}\n"
            "[SOURCE: https://example.gob.mx/ley.pdf]\n"
            "1.- Disposiciones generales\nTexte de loi sans coordonnées.\n"
        )
        names = {p["name"] for p in extract_structured_ports(joined)}
        assert {"Bahía Colonet", "Ensenada", "Manzanillo"} <= names

    def test_legal_port_of_phrasing(self):
        from app.core.extract import extract_structured_ports
        text = ("No plant material may be imported into Niue except through "
                "the port of Alofi, the Hanan International Airport, or the Post Office.")
        ports = extract_structured_ports(text)
        names = {p["name"] for p in ports}
        assert "Alofi" in names
        assert not any("Entry" in n or "Hanan" in n for n in names)

    def test_legal_phrasing_is_multilingual(self):
        from app.core.extract import extract_structured_ports
        fr = extract_structured_ports("L'entrée s'effectue uniquement par le port de Papeete, ou le port d'Alofi.")
        es = extract_structured_ports("Despacho únicamente por el puerto de Ensenada, no por el puerto de entrada aéreo.")
        names_fr = {p["name"] for p in fr}
        names_es = {p["name"] for p in es}
        assert "Papeete" in names_fr and "Alofi" in names_fr
        assert "Ensenada" in names_es
        assert "entrada" not in {n.lower() for n in names_es}

    def test_english_latitude_numbered_catalog(self):
        from app.core.extract import extract_structured_ports, looks_like_port_catalog
        text = "\n".join(
            f"{i}. Port{i}\nLatitude: -{i}.5\nLongitude: -17{i}.2"
            for i in range(1, 12)
        )
        ports = extract_structured_ports(text)
        assert len(ports) >= 8
        assert ports[0]["name"] == "Port1"
        assert abs(ports[0]["lat"] - (-1.5)) < 1e-6
        assert looks_like_port_catalog(text)

    def test_hints_and_seeds_apply_to_every_eez(self):
        for zone in (
            {"iso2": "FR", "sovereign": "France", "name": "France"},
            {"iso2": "FJ", "sovereign": "Fiji", "name": "Fiji"},
            {"iso2": "JP", "sovereign": "Japan", "name": "Japan"},
            {"iso2": "CL", "sovereign": "Chile", "name": "Chile"},
        ):
            hints = poe.search_hint_queries(zone)
            assert hints, zone
            blob = " ".join(hints)
            assert (
                "customs act" in blob.lower()
                or "legislation" in blob.lower()
                or "plaisance" in blob.lower()
            )
            assert "Papeete" not in blob and "Ensenada" not in blob and "Alofi" not in blob
        loc = poe.localized_query({"iso2": "ES", "name": "Spain"})
        assert loc and "habilitados" in loc
        assert "yates" not in (poe.localized_query({"iso2": "MX", "name": "Mexico"}) or "")

    def test_search_queries_are_per_vliz_polygon_not_country(self):
        hexagon = {
            "iso2": "FR", "sov_iso2": "FR", "name": "France", "sovereign": "France",
            "pol_type": "200NM", "mrgid": 5677,
        }
        mayotte = {
            "iso2": "YT", "sov_iso2": "FR", "name": "Mayotte", "sovereign": "France",
            "pol_type": "Overlapping claim", "mrgid": 48944,
        }
        qh = poe.localized_query(hexagon)
        qm = poe.localized_query(mayotte)
        assert qh and "France" in qh and "Mayotte" not in qh
        assert "hexagone" not in qh
        assert "plaisance" in qh and "PPF" in qh
        assert qm and "Mayotte" in qm and "hexagone" not in qm
        assert "PPF" not in qm
        hints_h = " ".join(poe.search_hint_queries(hexagon))
        hints_m = " ".join(poe.search_hint_queries(mayotte))
        assert "plaisance" in hints_h and "PPF" in hints_h
        assert "Mayotte" in hints_m
        assert "Mayotte" not in hints_h
        assert "PPF" not in hints_m
        assert "Papeete" not in hints_h and "Dzaoudzi" not in hints_m
        assert poe.zone_search_location(mayotte) == "YT"
        assert poe.zone_search_location(hexagon) == "FR"
        assert poe.is_france_mainland_eez(hexagon)
        assert not poe.is_france_mainland_eez(mayotte)

    def test_remember_seed_urls_without_port_names(self):
        exc = {"seed_urls": {}}
        added = poe.remember_seed_urls(
            {"iso2": "FJ"},
            ["https://www.customs.gov.fj/ports-of-entry", "https://evil.example/Alofi"],
            exceptions=exc, persist=False)
        assert added == ["https://www.customs.gov.fj/ports-of-entry"]
        assert "Alofi" not in json.dumps(exc)
        assert exc["seed_urls"]["FJ"][0].endswith("ports-of-entry")

    def test_official_attachments_prefer_list_pdf(self):
        from app.core.extract import official_attachments, should_follow_attachments
        md = (
            "### Puertos habilitados\n"
            "[Descarga](https://www.gob.mx/cms/uploads/attachment/file/1/PuertosYTerminalesHabilitados.pdf)\n"
            "[Autre](https://www.gob.mx/cms/uploads/attachment/file/2/logo.pdf)\n"
        )
        atts = official_attachments(md, "https://www.gob.mx/page")
        assert atts and "Habilitados" in atts[0]
        assert should_follow_attachments(md, "https://www.gob.mx/page")

    def test_france_landing_attachments_keep_plaisance_and_ppf(self):
        from app.core.extract import official_attachments, should_follow_attachments
        html = """
        <a href="/sites/default/files/2023-01/25/liste-EM-Schengen.pdf">Schengen</a>
        <a href="/sites/default/files/2022-07/11/carte-ppf-maritimes.pdf">old map</a>
        <a href="https://www.douane.gouv.fr/sites/default/files/uploads/files/carte-PPF-maritimes.pdf">PPF</a>
        <a href="/sites/default/files/2022-07/11/formulaire-immigration.pdf">form</a>
        <a href="/sites/default/files/2026-07/06/Liste-ports-de-plaisance-eligibles.pdf">liste</a>
        """
        base = "https://www.douane.gouv.fr/particuliers/vous-naviguez/vous-naviguez-en-provenance"
        atts = official_attachments(html, base)
        blob = " ".join(atts)
        assert "Liste-ports-de-plaisance-eligibles.pdf" in blob
        assert "uploads/files/carte-PPF-maritimes.pdf" in blob
        assert "formulaire-immigration" not in blob
        assert "2022-07/11/carte-ppf" not in blob
        assert should_follow_attachments(html, base)

    def test_english_customs_home_attachments_are_not_the_list(self):
        from app.core.extract import official_attachments, should_follow_attachments
        html = """
        <a href="/sites/default/files/2018-11/10-questions-before-exporting-en.pdf">export</a>
        <a href="/sites/default/files/2019-08/leaflet-trouble-free-travel-french-customs-advices-en.pdf">leaflet</a>
        <a href="/sites/default/files/2021-04/16/Brexit-How-to-make-a-successful-import.pdf">brexit</a>
        <a href="/sites/default/files/uploads/files/2019-04/customs-clearance-in-france-the-2016-ucc-aeo.pdf">aeo</a>
        <a href="https://assets.publishing.service.gov.uk/government/uploads/system/uploads/attachment_data/file/925140/BordersOpModel.pdf">uk</a>
        """
        base = "https://www.douane.gouv.fr/french-customs-information-available-english"
        atts = official_attachments(html, base)
        assert atts == []
        assert should_follow_attachments(html, base) is False

    def test_seed_urls_point_to_official_pages_not_names(self):
        mx = poe.seed_url_candidates({"iso2": "MX"})
        nu = poe.seed_url_candidates({"iso2": "NU"})
        blob = " ".join(c["url"] for c in mx + nu)
        assert any("puertos-y-terminales" in c["url"] for c in mx)
        assert any(c["url"].endswith(".pdf") and "gov.nu" in c["url"] for c in nu)
        assert "Ensenada" not in blob and "Alofi" not in blob
        assert poe.search_hint_queries({"iso2": "MX"})
        assert poe.search_hint_queries({"iso2": "NU"})
        assert not any("Alofi" in q for q in poe.search_hint_queries({"iso2": "NU"}))

    def test_example_official_pages_are_seeded_per_polygon_iso2(self):
        cases = {
            "FR": "vous-naviguez-en-provenance",
            "MX": "puertos-y-terminales",
            "NU": "niue_laws_vol4_part1",
            "NZ": "places-of-first-arrival-seaports",
            "NC": "formalites-douanieres-pour-les-navires-de-plaisance",
            "VE": "Ley-de-Marinas-y-Actividades-Conexas.pdf",
            "SX": "Pages/Customs.aspx",
            "YT": "JORFTEXT000030235682",
            "GB": "submit-a-pleasure-craft-report",
            "EG": "yacht-tourism",
            "AL": "autorizim-per-perjashtimin",
        }
        for iso, needle in cases.items():
            urls = " ".join(c["url"] for c in poe.seed_url_candidates({"iso2": iso}))
            assert needle in urls, (iso, urls)
        ve = " ".join(c["url"] for c in poe.seed_url_candidates({"iso2": "VE"}))
        assert "Ley-de-Marinas-y-Actividades-Conexas.pdf" in ve
        assert "CAPITANIAS-DE-PUERTO" in ve
        nz_on_niue = poe.seed_url_candidates({
            "iso2": "NU", "sov_iso2": "NZ", "name": "Niue", "sovereign": "New Zealand",
        })
        blob = " ".join(c["url"] for c in nz_on_niue)
        assert "mpi.govt.nz" not in blob and "customs.govt.nz" not in blob
        mayotte = poe.seed_url_candidates({
            "iso2": "YT", "sov_iso2": "FR", "name": "Mayotte", "sovereign": "France",
        })
        assert not any("vous-naviguez" in c["url"] for c in mayotte)
        nz_hints = " ".join(poe.search_hint_queries({
            "iso2": "NZ", "name": "New Zealand", "sovereign": "New Zealand",
        }))
        assert "places of first arrival" in nz_hints
        niue_hints = " ".join(poe.search_hint_queries({
            "iso2": "NU", "sov_iso2": "NZ", "name": "Niue", "sovereign": "New Zealand",
        }))
        assert "places of first arrival" not in niue_hints
        assert "mpi.govt.nz" not in poe.build_whitelist("NU", "NZ")

    def test_example_urls_outrank_homes(self):
        home_ve = poe.list_url_bonus("https://inea.gob.ve/")
        mpi = poe.list_url_bonus(
            "https://www.mpi.govt.nz/resources-and-forms/registers-and-lists/"
            "places-of-first-arrival-seaports")
        craft = poe.list_url_bonus(
            "https://www.customs.govt.nz/about-us/news/our-stories/"
            "sailing-to-new-zealand-this-small-craft-season")
        nc = poe.list_url_bonus(
            "https://douane.gouv.nc/particuliers/"
            "formalites-douanieres-pour-les-navires-de-plaisance")
        mx = poe.list_url_bonus(
            "https://www.gob.mx/puertosymarinamercante/acciones-y-programas/"
            "puertos-y-terminales")
        sx = poe.list_url_bonus(
            "https://www.sintmaartengov.org/Ministries/Departments/Pages/Customs.aspx")
        ley = poe.list_url_bonus(
            "https://inea.gob.ve/wp-content/uploads/2026/04/"
            "Ley-de-Marinas-y-Actividades-Conexas.pdf")
        cap = poe.list_url_bonus(
            "https://inea.gob.ve/wp-content/uploads/2026/04/"
            "REGLAMENTO-QUE-DETERMINA-LA-JURISDICCION-DE-LAS-CAPITANIAS-DE-PUERTO-DE-LA-REPUBLICA.pdf")
        assert mpi > 0.3 and craft > 0.2 and nc > 0.2 and mx > 0.2
        assert mpi > home_ve and mx > home_ve
        assert sx > home_ve
        assert ley > home_ve and cap > home_ve

    def test_numbered_law_is_not_a_port_catalog(self):
        from app.core.extract import looks_like_port_catalog
        law = "\n".join(f"{i}. Article transitoire sans coordonnées." for i in range(1, 20))
        assert looks_like_port_catalog(law) is False

    def test_foreign_gov_domains_rejected_for_other_eez(self):
        assert poe.is_foreign_gov_domain("congress.gov", {"ws"})
        assert poe.is_foreign_gov_domain("customs.gov.ph", {"ws"})
        assert not poe.is_foreign_gov_domain("revenue.gov.ws", {"ws"})
        assert not poe.is_foreign_gov_domain("customs.gov", {"us"})

    def test_challenge_page_is_blocked(self):
        from app.core.extract import looks_blocked, looks_hard_challenge, UA_READER, UA_BROWSER
        assert looks_blocked("Challenge Validation", html="<div class='sec-container'>",
                             title="Challenge Validation") is True
        assert looks_hard_challenge(html="<div class='sec-container'>",
                                    title="Challenge Validation") is True
        assert "Chrome/" not in UA_READER["User-Agent"]
        assert UA_READER["User-Agent"] != UA_BROWSER["User-Agent"]


# --- UNCLOS: empty islands of the run ----------------------------------------
class TestUnclosEmptyIslands:
    def test_uninhabited_run_islands(self):
        for name in (
            "Macquarie Island", "Clipperton Island", "Jarvis Island",
            "Palmyra Atoll", "Howland and Baker Islands", "Juan de Nova Island",
            "Bassas da India",
        ):
            q = poe.qualify_unclos({"poe_count": 0, "pol_type": "200NM",
                                    "anchor": [0, 10], "name": name})
            assert q and q["code"] == "uninhabited", (name, q)

    def test_overlapping_uninhabited_stays_claim(self):
        for name in (
            "Wake Island / Enenkio", "Glorioso Islands", "Ile Tromelin",
            "Abu musa, Greater and Lesser Tunb", "Navassa Island",
        ):
            q = poe.qualify_unclos({"poe_count": 0, "pol_type": "Overlapping claim",
                                    "anchor": [0, 10], "name": name})
            assert q and q["code"] == "overlapping_claim", (name, q)

    def test_abu_musa_uninhabited_when_not_overlapping(self):
        q = poe.qualify_unclos({"poe_count": 0, "pol_type": "200NM",
                                "anchor": [0, 10], "name": "Abu musa, Greater and Lesser Tunb"})
        assert q and q["code"] == "uninhabited", q


# --- Retrained NER: regulatory text, no longer only “Name, city (zone)”
class TestNerRegulatory:
    def test_extracts_ports_from_prose(self):
        from app.core.ml import extract_entities, has_ner_model
        assert has_ner_model()
        samples = {
            "The designated ports of entry for foreign pleasure craft include Ensenada.": "Ensenada",
            "Foreign yachts must clear customs at Alofi in Niue.": "Alofi",
            "Port de Papeete, Tahiti (French Polynesia). Clearance douanière au quai.": "Papeete",
        }
        for text, needle in samples.items():
            ports = [e["text"] for e in extract_entities(text) if e["label"] == "PORT_NAME"]
            assert any(needle in p for p in ports), (text, ports)


# --- Union SERP multi-moteurs + accord Jaccard --------------------------------
class TestSearchMergeAgreement:
    def test_normalize_www_and_slash(self):
        a = poe._normalize_url("https://www.Douane.gouv.fr/ports/")
        b = poe._normalize_url("https://douane.gouv.fr/ports")
        assert a == b

    def test_merge_keeps_two_paths_same_domain(self):
        a = [{"url": "https://aduana.gob.mx/noticias", "domain": "aduana.gob.mx",
              "engine": "searxng"}]
        b = [{"url": "https://www.aduana.gob.mx/decreto.pdf", "domain": "aduana.gob.mx",
              "engine": "tinyfish"}]
        merged = poe._merge_candidates(a, b)
        assert len(merged) == 2

    def test_merge_www_is_one_url(self):
        merged = poe._merge_candidates(
            [{"url": "https://www.douane.gouv.fr/x", "engine": "searxng"}],
            [{"url": "https://douane.gouv.fr/x", "engine": "tinyfish"}],
        )
        assert len(merged) == 1
        assert set(merged[0]["engine"]) == {"searxng", "tinyfish"}

    def test_list_url_bonus_ignores_hostname_customs(self):
        home = poe.list_url_bonus(
            "http://www.douane.gouv.fr/french-customs-information-available-english")
        pdf = poe.list_url_bonus(
            "https://www.douane.gouv.fr/sites/default/files/2025-02/28/"
            "Liste%20des%20ports%20de%20plaisance%20rattach%C3%A9s%20au%20dispositif.pdf")
        page = poe.list_url_bonus("https://www.douane.gouv.fr/demarche/ports-entree")
        assert pdf > home
        assert page > home
        assert pdf > 0.4

    def test_best_per_domain_prefers_decree_pdf(self):
        cands = [
            {"url": "https://aduana.gob.mx/noticias", "domain": "aduana.gob.mx",
             "score_serp": 0.4},
            {"url": "https://aduana.gob.mx/decreto.pdf", "domain": "aduana.gob.mx",
             "score_serp": 0.4},
        ]
        best = poe._best_per_domain(cands)
        urls = [c["url"] for c in best]
        assert any(u.endswith("decreto.pdf") for u in urls)
        assert any("noticias" in u for u in urls)

    def test_best_per_domain_keeps_france_list_and_ppf_pdfs(self):
        cands = [
            {"url": "http://www.douane.gouv.fr/french-customs-information-available-english",
             "domain": "douane.gouv.fr", "score_serp": 0.9},
            {"url": "https://www.douane.gouv.fr/particuliers/vous-naviguez/"
                    "vous-naviguez-en-provenance-ou-destination-dun-pays-non-membre-de",
             "domain": "douane.gouv.fr", "score_serp": 0.5},
            {"url": "https://www.douane.gouv.fr/sites/default/files/2018-11/"
                    "10-questions-before-exporting-en.pdf",
             "domain": "douane.gouv.fr", "score_serp": 0.8},
            {"url": "https://www.douane.gouv.fr/sites/default/files/2026-07/06/"
                    "Liste-ports-de-plaisance-eligibles.pdf",
             "domain": "douane.gouv.fr", "score_serp": 0.4},
            {"url": "https://www.douane.gouv.fr/sites/default/files/uploads/files/"
                    "carte-PPF-maritimes.pdf",
             "domain": "douane.gouv.fr", "score_serp": 0.4},
        ]
        best = poe._best_per_domain(cands)
        blob = " ".join(c["url"] for c in best)
        assert "Liste-ports-de-plaisance-eligibles.pdf" in blob
        assert "carte-PPF-maritimes.pdf" in blob
        assert "exporting-en.pdf" not in blob
        assert "information-available-english" not in blob

    def test_site_list_pdf_query_is_scoped_to_mainland(self):
        hexagon = {
            "iso2": "FR", "sov_iso2": "FR", "name": "France", "sovereign": "France",
            "pol_type": "200NM", "mrgid": 5677,
        }
        mayotte = {
            "iso2": "YT", "sov_iso2": "FR", "name": "Mayotte", "sovereign": "France",
            "pol_type": "Overlapping claim", "mrgid": 48944,
        }
        qh = poe.site_list_pdf_query("douane.gouv.fr", hexagon)
        qm = poe.site_list_pdf_query("douane.gouv.fr", mayotte)
        assert "filetype:pdf" in qh and "plaisance" in qh and "PPF" in qh
        assert "filetype:pdf" in qm and "PPF" not in qm
        assert "plaisance" not in qm

    def test_landing_candidates_are_per_mrgid_not_iso2(self):
        hexagon = poe.landing_url_candidates({"mrgid": 5677, "iso2": "FR"})
        mayotte = poe.landing_url_candidates({"mrgid": 48944, "iso2": "YT", "sov_iso2": "FR"})
        h = " ".join(c["url"] for c in hexagon)
        m = " ".join(c["url"] for c in mayotte)
        assert "vous-naviguez" in h
        assert not any(c["url"].lower().endswith(".pdf") for c in hexagon)
        assert "mayotte" in m.lower()
        assert "vous-naviguez-en-provenance" not in m

    def test_agreement_high_overlap_not_discordant(self):
        shared = [
            {"url": f"https://gov.tl/p{i}", "domain": "gov.tl"} for i in range(3)
        ]
        cmp_ = poe._search_agreement(shared, shared)
        assert cmp_["discordant"] is False
        assert cmp_["jaccard_domains"] == 1.0

    def test_agreement_disjoint_is_discordant(self):
        a = [{"url": f"https://a{i}.gov/x", "domain": f"a{i}.gov"} for i in range(5)]
        b = [{"url": f"https://b{i}.gob.mx/y", "domain": f"b{i}.gob.mx"} for i in range(5)]
        cmp_ = poe._search_agreement(a, b)
        assert cmp_["discordant"] is True
        assert cmp_["jaccard_domains"] < 0.3

    def test_empty_engine_is_not_discordant(self):
        a = [{"url": f"https://gov.tl/p{i}", "domain": "gov.tl"} for i in range(4)]
        assert poe._search_agreement(a, [])["discordant"] is False
        assert poe._search_agreement([], [])["discordant"] is False


class TestFindSourcesTinyfish:
    zone = {
        "name": "France", "geoname": "France", "iso2": "FR", "sov_iso2": "FR",
        "sovereign": "France",
    }

    def _run(self, monkeypatch, searx_hits, tf_hits, grounded_return=None):
        grounded_calls = []

        async def fake_searx(query, log):
            return list(searx_hits)

        async def fake_tf(query, key, log, location=None, language=None,
                          include_domains=None):
            if include_domains:
                return []
            return list(tf_hits)

        async def fake_grounded(zone, whitelist, log, query_override=None):
            grounded_calls.append(query_override or "default")
            return grounded_return if grounded_return is not None else ([], None)

        async def fake_serper(query, key, log, gl=None, hl="en"):
            return []

        monkeypatch.setattr(poe, "search_searxng", fake_searx)
        monkeypatch.setattr(poe, "_tf_search_safe", fake_tf)
        monkeypatch.setattr(poe, "_serper_search_safe", fake_serper)
        monkeypatch.setattr(poe, "search_grounded", fake_grounded)
        monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
        official, strict, syn = asyncio.run(poe._find_sources(
            self.zone, poe.build_whitelist("FR", "FR"), {}, lambda m: None,
            tf_key="test"))
        return official, strict, syn, grounded_calls

    def test_has_list_candidate_and_lessons_gate(self):
        pdf = {"url": "https://www.douane.gouv.fr/sites/x/Liste-ports-de-plaisance-eligibles.pdf"}
        home = {"url": "https://www.douane.gouv.fr/"}
        assert poe.has_list_candidate([pdf]) is True
        assert poe.needs_lessons_round([pdf]) is False
        assert poe.needs_lessons_round([home]) is True
        assert poe.needs_lessons_round([]) is True

    def test_bootstrap_org_not_foreign_cctld(self):
        sx = {"iso2": "SX", "sov_iso2": "NL"}
        boot = poe.bootstrap_national_hits([{
            "url": "https://www.sintmaartengov.org/Ministries/Departments/Pages/Customs.aspx",
            "domain": "sintmaartengov.org",
        }], sx)
        assert boot and boot[0]["domain"] == "sintmaartengov.org"
        assert not poe.bootstrap_national_hits([{
            "url": "https://www.government.nl/",
            "domain": "government.nl",
        }], sx)
        assert not poe.bootstrap_national_hits([{
            "url": "https://www.congress.gov/bill/1",
            "domain": "congress.gov",
        }], {"iso2": "WS", "sov_iso2": "WS"})

    def test_lessons_round_when_classic_is_shallow(self, monkeypatch):
        tf_queries = []

        async def fake_searx(query, log):
            return []

        async def fake_tf(query, key, log, location=None, language=None,
                          include_domains=None):
            tf_queries.append(query)
            return []

        async def fake_grounded(zone, whitelist, log, query_override=None):
            return [], None

        async def fake_serper(query, key, log, gl=None, hl="en"):
            return []

        monkeypatch.setattr(poe, "search_searxng", fake_searx)
        monkeypatch.setattr(poe, "_tf_search_safe", fake_tf)
        monkeypatch.setattr(poe, "_serper_search_safe", fake_serper)
        monkeypatch.setattr(poe, "search_grounded", fake_grounded)
        monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: [])
        monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
        zone = {"iso2": "HR", "sov_iso2": "HR", "name": "Croatia",
                "sovereign": "Croatia"}
        asyncio.run(poe._find_sources(
            zone, poe.build_whitelist("HR", "HR"), {}, lambda m: None,
            tf_key="test"))
        assert any("akcizë" in q or "yacht" in q for q in tf_queries)

    def test_seeded_france_skips_lessons_tf(self, monkeypatch):
        tf_queries = []

        async def fake_searx(query, log):
            return []

        async def fake_tf(query, key, log, location=None, language=None,
                          include_domains=None):
            tf_queries.append(query)
            return []

        async def fake_grounded(zone, whitelist, log, query_override=None):
            return [], None

        async def fake_serper(query, key, log, gl=None, hl="en"):
            return []

        monkeypatch.setattr(poe, "search_searxng", fake_searx)
        monkeypatch.setattr(poe, "_tf_search_safe", fake_tf)
        monkeypatch.setattr(poe, "_serper_search_safe", fake_serper)
        monkeypatch.setattr(poe, "search_grounded", fake_grounded)
        monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: [])
        monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
        zone = {
            "iso2": "FR", "sov_iso2": "FR", "name": "France",
            "sovereign": "France",
        }
        asyncio.run(poe._find_sources(
            zone, poe.build_whitelist("FR", "FR"), None, lambda m: None,
            tf_key="test"))
        assert not any("akcizë" in q for q in tf_queries)

    def test_tinyfish_gov_skips_grounded(self, monkeypatch):
        tf_hits = [{
            "url": "https://www.douane.gouv.fr/demarche/ports-entree",
            "domain": "douane.gouv.fr", "engine": "tinyfish",
        }]
        official, strict, _syn, grounded = self._run(monkeypatch, [], tf_hits)
        assert grounded == []
        assert strict is True
        assert official
        assert "gouv.fr" in (official[0]["domain"] or "")

    def test_both_empty_grounded_once(self, monkeypatch):
        _official, _strict, _syn, grounded = self._run(monkeypatch, [], [])
        assert len(grounded) == 1

    def test_serper_google_style_merged(self, monkeypatch):
        serper_queries = []

        async def fake_searx(query, log):
            return []

        async def fake_tf(query, key, log, location=None, language=None,
                          include_domains=None):
            return []

        async def fake_serper(query, key, log, gl=None, hl="en"):
            serper_queries.append(query)
            return [{
                "url": "https://www.douane.gouv.fr/demarche/liste-ports",
                "domain": "douane.gouv.fr", "engine": "serper",
            }]

        async def fake_grounded(zone, whitelist, log, query_override=None):
            return [], None

        monkeypatch.setattr(poe, "search_searxng", fake_searx)
        monkeypatch.setattr(poe, "_tf_search_safe", fake_tf)
        monkeypatch.setattr(poe, "_serper_search_safe", fake_serper)
        monkeypatch.setattr(poe, "search_grounded", fake_grounded)
        monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: [])
        monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
        official, strict, _syn = asyncio.run(poe._find_sources(
            self.zone, poe.build_whitelist("FR", "FR"), {}, lambda m: None,
            tf_key="test"))
        assert any("Ports of Entry" in q or "port d'entrée" in q for q in serper_queries)
        assert any(".fr" in q for q in serper_queries)
        assert official
        assert any(c.get("engine") == "serper" or "douane.gouv.fr" in (c.get("domain") or "")
                   for c in official)
        assert strict is True


def _catalog_text(n=10):
    blocks = [f"{i}.- Puerto {i}\nLatitud:{10 + i}.123\nLongitud:{-20 - i}.456\n"
              for i in range(1, n + 1)]
    return "".join(blocks) + (" Puerto habilitado oficial. " * 25)


class TestFetchMirrorArbitration:
    def test_catalog_beats_challenge(self):
        from app.core.extract import _arbitrate_mirror_texts
        catalog = _catalog_text()
        picked = _arbitrate_mirror_texts("Just a moment... Enable JavaScript", catalog)
        assert picked is not None
        text, level, cmp_ = picked
        assert level == "N3-mirror-tinyfish"
        assert cmp_["winner"] == "tinyfish"
        assert "Puerto" in text

    def test_jina_only_when_tf_blocked(self):
        from app.core.extract import _arbitrate_mirror_texts
        jina = _catalog_text()
        picked = _arbitrate_mirror_texts(jina, None)
        assert picked[1] == "N3-mirror-jina"
        assert picked[2]["winner"] == "jina"

    def test_fetch_mirror_tf_wins_when_jina_challenge(self, monkeypatch):
        from app.core import extract as ext
        catalog = _catalog_text()

        async def fake_jina(url, log):
            return "Just a moment... checking your browser"

        async def fake_tf(url, log):
            return catalog, []

        monkeypatch.setattr(ext, "_jina_mirror_text", fake_jina)
        monkeypatch.setattr(ext, "_tinyfish_mirror_text", fake_tf)
        monkeypatch.setattr("app.core.tinyfish.tf_api_key", lambda settings=None: "k")
        # fetch_mirror_text imports tf_api_key inside the function
        import app.core.tinyfish as tfmod
        monkeypatch.setattr(tfmod, "tf_api_key", lambda settings=None: "k")

        text, level, *rest = asyncio.run(ext.fetch_mirror_text("https://aduana.gob.mx/list"))
        assert level == "N3-mirror-tinyfish"
        assert "Puerto" in text


class TestCatalogSkipPolicy:
    def test_eight_port_of_fragments_still_call_llm(self, monkeypatch):
        called = []

        async def fake_llm(context, zone, settings=None, log=None):
            called.append(1)
            return [{"name": "Nouméa", "extraction_engine": "claude"}]

        monkeypatch.setattr(poe, "extract_ports", fake_llm)
        import app.core.ml as ml
        monkeypatch.setattr(ml, "extract_entities", lambda text: [])
        catalog = " ".join(f"le port de Fragment{i}," for i in range(1, 9))

        ports = asyncio.run(poe.extract_ports_llm(
            "slice", {"name": "New Caledonia"}, lambda m: None,
            catalog_text=catalog))
        assert called == [1]
        names = {p["name"] for p in ports}
        assert names == {"Nouméa"}

    def test_skip_returns_only_coord_ports(self, monkeypatch):
        called = []

        async def boom(context, zone, settings=None, log=None):
            called.append(1)
            return []

        monkeypatch.setattr(poe, "extract_ports", boom)
        mixed = TestStructuredDiscovery._MX_JINA + (
            "\nL'entrée s'effectue par le port de Papeete uniquement.\n")
        ports = asyncio.run(poe.extract_ports_llm(
            "slice", {"name": "Mexico"}, lambda m: None, catalog_text=mixed))
        assert called == []
        names = {p["name"] for p in ports}
        assert "Ensenada" in names and "Papeete" not in names
        assert all(p.get("lat") is not None for p in ports)


class TestGeocodePolicy:
    def _zone(self):
        return {"mrgid": 1, "name": "Testland", "iso2": "TL",
                "sovereign": "Testland", "geometry": None}

    def test_junk_name_skips_dual(self, monkeypatch):
        called = []

        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "eerste binnenkomst", "geocodeable": False,
                     "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            called.append(port["name"])
            return {"nominatim": [52.0, 5.0], "geonames": [52.0, 5.0],
                    "agree": True, "agreement_km": 0.1, "geonames_available": True}

        async def boom_llm(*a, **k):
            raise AssertionError("llm_geocode_port must not run on junk names")

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        monkeypatch.setattr(poe, "llm_geocode_port", boom_llm)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None))
        assert called == []
        assert docs[0]["lat"] is None
        assert docs[0]["geocode_arbitration"] == "not_geocodeable"

    def test_large_name_only_catalog_defers_geocode(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [
                {"name": f"Port {i}", "extraction_engine": "catalog"}
                for i in range(1, 9)
            ]

        async def fake_dual(port, zone, log=None):
            raise AssertionError("geocode_port_dual should be deferred")

        async def boom_llm(*a, **k):
            raise AssertionError("llm_geocode_port must not run on deferred catalog")

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        monkeypatch.setattr(poe, "llm_geocode_port", boom_llm)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None))
        assert len(docs) == 8
        assert all(d["lat"] is None for d in docs)
        assert {d["name"] for d in docs} == {f"Port {i}" for i in range(1, 9)}
        assert docs[0]["geocode_arbitration"] == "geocode_deferred"

    def test_source_coords_skip_dual(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Ensenada", "lat": 31.85, "lon": -116.62,
                     "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            raise AssertionError("geocode_port_dual should not run")

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None))
        assert docs[0]["lat"] == 31.85
        assert docs[0]["geocode_source"] == "source"
        assert docs[0]["geocode_arbitration"] == "single"

    def test_haiku_tiebreak_picks_geonames(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Port Alpha", "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            return {"nominatim": [10.0, 20.0], "geonames": [50.0, 5.0],
                    "agree": False, "agreement_km": 4000,
                    "geonames_available": True}

        async def fake_arb(zone, items, settings=None, log=None):
            assert len(items) == 1
            return {"Port Alpha": "geonames", "port alpha": "geonames"}

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        import app.core.llm as llm_mod
        monkeypatch.setattr(llm_mod, "arbitrate_geocode", fake_arb)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None))
        assert docs[0]["geocode_source"] == "geonames"
        assert docs[0]["lat"] == 50.0
        assert docs[0]["geocode_arbitration"] == "haiku_tiebreak"

    def test_haiku_none_rejects_both(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Port Alpha", "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            return {"nominatim": [10.0, 20.0], "geonames": [50.0, 5.0],
                    "agree": False, "agreement_km": 4000,
                    "geonames_available": True}

        async def fake_arb(zone, items, settings=None, log=None):
            return {"Port Alpha": "none", "port alpha": "none"}

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        import app.core.llm as llm_mod
        monkeypatch.setattr(llm_mod, "arbitrate_geocode", fake_arb)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None))
        assert docs[0]["lat"] is None
        assert docs[0]["geocode_arbitration"] == "haiku_none"

    def test_mute_directories_llm_gps_kept_without_polygon(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Port Alpha", "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            return {"nominatim": None, "geonames": None, "agree": None,
                    "geonames_available": True}

        async def fake_llm(port, zone, settings=None, log=None):
            assert port["name"] == "Port Alpha"
            assert zone.get("mrgid") == 1
            return 10.5, 20.5

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        monkeypatch.setattr(poe, "llm_geocode_port", fake_llm)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None, settings={}))
        assert docs[0]["lat"] == 10.5
        assert docs[0]["lon"] == 20.5
        assert docs[0]["geocode_source"] == "llm"
        assert docs[0]["geocode_arbitration"] == "llm"

    def test_mute_llm_outside_this_polygon_keeps_name_without_gps(self, monkeypatch):
        from shapely.geometry import mapping, box
        zone = {
            "mrgid": 5677, "name": "France", "iso2": "FR", "sovereign": "France",
            "geometry": mapping(box(-5.0, 42.0, 8.0, 51.5)),
        }

        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Dzaoudzi", "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            return {"nominatim": None, "geonames": None, "agree": None,
                    "geonames_available": True}

        async def fake_llm(port, zone, settings=None, log=None):
            return -12.78, 45.30  # Mayotte, not the hexagon

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        monkeypatch.setattr(poe, "llm_geocode_port", fake_llm)
        docs = asyncio.run(poe._extract_and_geocode(
            zone, "ctx", [], lambda m: None, settings={}))
        assert [d["name"] for d in docs] == ["Dzaoudzi"]
        assert docs[0]["lat"] is None
        assert docs[0]["geocode_arbitration"] == "llm_spatial_rejected"
        assert docs[0]["geocode_source"] is None

    def test_disagreement_does_not_call_llm_geocode_port(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Port Alpha", "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            return {"nominatim": [10.0, 20.0], "geonames": [50.0, 5.0],
                    "agree": False, "agreement_km": 4000,
                    "geonames_available": True}

        async def fake_arb(zone, items, settings=None, log=None):
            return {"Port Alpha": "nominatim", "port alpha": "nominatim"}

        async def boom_llm(*a, **k):
            raise AssertionError("désaccord n'est pas muet — pas de GPS inventé")

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        monkeypatch.setattr(poe, "llm_geocode_port", boom_llm)
        import app.core.llm as llm_mod
        monkeypatch.setattr(llm_mod, "arbitrate_geocode", fake_arb)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None, settings={}))
        assert docs[0]["geocode_source"] == "nominatim"
        assert docs[0]["lat"] == 10.0

    def test_directories_hit_skips_llm_geocode_port(self, monkeypatch):
        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [{"name": "Port Alpha", "extraction_engine": "claude"}]

        async def fake_dual(port, zone, log=None):
            return {"nominatim": [10.0, 20.0], "geonames": None, "agree": None,
                    "geonames_available": True}

        async def boom_llm(*a, **k):
            raise AssertionError("annuaires pas muets — pas de GPS inventé")

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        monkeypatch.setattr(poe, "llm_geocode_port", boom_llm)
        docs = asyncio.run(poe._extract_and_geocode(
            self._zone(), "ctx", [], lambda m: None, settings={}))
        assert docs[0]["geocode_source"] == "nominatim"
        assert docs[0]["lat"] == 10.0

    def test_sibling_polygon_port_is_not_written_on_this_fiche(self, monkeypatch):
        from shapely.geometry import mapping, box
        zone = {
            "mrgid": 5677, "name": "France", "iso2": "FR", "sovereign": "France",
            "geometry": mapping(box(-5.0, 42.0, 8.0, 51.5)),
        }

        async def fake_extract(context, zone, log, rec=None, catalog_text=None,
                              settings=None):
            return [
                {"name": "Marseille", "lat": 43.3, "lon": 5.4,
                 "extraction_engine": "catalog"},
                {"name": "Dzaoudzi", "lat": -12.78, "lon": 45.30,
                 "extraction_engine": "catalog"},
            ]

        async def fake_dual(port, zone, log=None):
            raise AssertionError("coords already in source")

        monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
        monkeypatch.setattr(poe, "geocode_port_dual", fake_dual)
        docs = asyncio.run(poe._extract_and_geocode(zone, "ctx", [], lambda m: None))
        names = [d["name"] for d in docs]
        assert names == ["Marseille"]
        assert docs[0]["mrgid"] == 5677
