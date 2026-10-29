import json, os, tempfile
from bi_v3 import certify, dedup, diff, runs, toloka
from bi_v3.hmem import HMem
from bi_v3.pipeline import _coords_in_text

def E(name, lat=None, lon=None, **kw):
    return {"layer": "marina", "territory": "T", "name": name, "key": dedup.normalize_name(name), "lat": lat, "lon": lon, **kw}

def test_dedup_name_and_space():
    assert dedup.is_duplicate(E("Marina Pointe du Bout"), E("Pointe du Bout Marina"))
    assert dedup.is_duplicate(E("Marina du Marin", 14.47, -60.87), E("Marina Le Marin", 14.4701, -60.8701))
    assert not dedup.is_duplicate(E("Marina A", 14.0, -61.0), E("Port Z", 15.0, -61.0))

def test_levels_never_llm_only():
    e = E("Marina du Marin", evidence=[{"url": "https://navily.com/x", "extracted": True, "judge": "supported"}])
    assert certify.level(e) == "C0"  # aggregator is not official
    e["evidence"][0]["url"] = "https://www.martinique.gouv.fr/x"
    assert certify.level(e) == "C2"
    e["geo"] = {"agree": True, "independent_sources": 2}; assert certify.level(e) == "C3"
    e["human"] = "confirmed"; assert certify.level(e) == "C4"

def test_diff_flags():
    a = [E("Marina X", 14.0, -61.0, level="C1", source_urls=["https://a.fr"])]
    b = [E("Marina X", 14.05, -61.0, level="C2", source_urls=["https://b.fr"]), E("New One", 1, 1)]
    s = diff.diff_runs(a, b)["summary"]
    assert s["added"] == 1 and s["moved"] == 1 and s["recertified"] == 1 and s["resourced"] == 1

def test_runs_immutable_and_hash():
    d = tempfile.mkdtemp(); ents = [E("A", 1, 2, level="C0")]
    _, m = runs.write_run(d, "r1", ents, {})
    try:
        runs.write_run(d, "r1", ents, {}); assert False
    except FileExistsError:
        pass
    assert runs.from_geojson(os.path.join(d, "r1", "entities.geojson"))[0]["lat"] == 1

def test_hmem_ledger_tamper_evident():
    d = tempfile.mkdtemp(); m = HMem(d)
    m.remember("r1", E("A", level="C1")); m.remember("r2", E("A", level="C2")); m.save()
    assert m.verify() and m.facts["marina:T:a"]["best"]["from_run"] == "r2"
    lines = open(m.ledger).read().replace('"C1"', '"C4"'); open(m.ledger, "w").write(lines)
    assert not HMem(d).verify()

def test_page_coords_must_be_in_text():
    assert _coords_in_text(14.4667, -60.8667, "Position 14°28' N 60°52' W")
    assert not _coords_in_text(14.4667, -60.8667, "no coordinates here")

def test_toloka_sample(tmp_path):
    ents = [E(f"M{i}", level="C2", source_urls=["https://x.fr"]) for i in range(30)]
    s = toloka.sample(ents, 25); assert len(s) == 25
    assert toloka.export_tsv(s, str(tmp_path / "t.tsv")) == 25
