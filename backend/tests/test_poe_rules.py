"""Business rules for ports of entry — offline, no HTTP.

United Kingdom: every port is a port of entry via the official app; a missing
designated list is not a data gap. Publish only where a State source says a
foreign sailing yacht can enter. Noonsite never makes an item Gold or
confirmed; a URL seen by both arms does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.dedup import normalize_name  # noqa: E402
from app.services.listing_control import compare_to_listing  # noqa: E402
from app.services.poe_confidence import reasons_en, score_port  # noqa: E402
from app.services.poe_pipeline import qualify_unclos  # noqa: E402
from app.services.poe_rules import (  # noqa: E402
    counts_as_data_gap,
    entry_mode,
    every_port_is_entry,
    is_noonsite_url,
    is_uk_app_entry,
    may_publish,
    noonsite_blocks_confirmation,
    source_is_gold,
)
from app.services.poe_seed_enrich import apply_judge_verdict  # noqa: E402
from app.services.poe_zone_fiche import assemble_zone_fiche  # noqa: E402
from app.services.review_choices import kept_official_urls  # noqa: E402


def _uk_zone(**extra):
    zone = {
        "mrgid": 5696,
        "name": "United Kingdom",
        "geoname": "British Exclusive Economic Zone",
        "iso2": "GB",
        "sov_iso2": "GB",
        "sovereign": "United Kingdom",
        "pol_type": "200NM",
        "anchor": [-2.0, 54.0],
        "poe_count": 0,
    }
    zone.update(extra)
    return zone


def _listing(name, *, slug, country, iso2, mrgid, **extra):
    row = {
        "name": name,
        "role": "poe",
        "slug": slug,
        "country": country,
        "country_iso2": iso2,
        "mrgid": mrgid,
        "mrgids": [mrgid],
        "zone_name": country,
    }
    row.update(extra)
    return row


def test_noonsite_host_never_matches_a_lookalike():
    assert is_noonsite_url("https://www.noonsite.com/place/uk")
    assert is_noonsite_url("https://news.noonsite.com/uk")
    assert not is_noonsite_url("https://notnoonsite.com/uk")
    assert not is_noonsite_url("https://noonsite.com.evil.example/uk")
    assert not is_noonsite_url("https://www.gov.uk/submit-a-pleasure-craft-report")


def test_uk_every_port_is_entry_and_missing_list_is_not_a_gap():
    zone = _uk_zone()
    assert every_port_is_entry(zone) is True
    assert is_uk_app_entry(zone) is True
    assert entry_mode(zone) == "official_app"
    assert counts_as_data_gap(zone, missing_designated_list=True) is False
    # API qualify omits iso2; the name + geoname still identify the regime.
    bare = {
        "poe_count": 0,
        "name": "United Kingdom",
        "geoname": "British Exclusive Economic Zone",
        "pol_type": "200NM",
        "anchor": [0, 54],
    }
    assert is_uk_app_entry(bare) is True
    qual = qualify_unclos(zone)
    assert qual["code"] == "app_entry"
    assert qual["data_gap"] is False
    assert qual["entry_mode"] == "official_app"
    assert qualify_unclos(bare)["code"] == "app_entry"
    # Ports already on the sheet: no empty-zone label at all.
    assert qualify_unclos(_uk_zone(poe_count=4)) is None


def test_uk_joint_regime_and_overseas_stay_outside_the_app_rule():
    joint = _uk_zone(
        mrgid=48967,
        pol_type="Joint regime",
        geoname="Joint regime area: United Kingdom / Denmark (Faeroe Islands)",
    )
    assert is_uk_app_entry(joint) is False
    assert entry_mode(joint) == "designated_port"
    assert qualify_unclos(joint)["code"] == "joint_regime"
    assert counts_as_data_gap(joint, missing_designated_list=True) is True
    bvi = {
        "poe_count": 0,
        "name": "British Virgin Islands",
        "iso2": "VG",
        "sovereign": "United Kingdom",
        "pol_type": "200NM",
        "anchor": [-64.5, 18.4],
    }
    assert is_uk_app_entry(bvi) is False
    assert qualify_unclos(bvi)["code"] == "sovereign_entry"
    # A port literally named like the country is not the regime.
    assert is_uk_app_entry({"name": "United Kingdom"}) is False
    assert qualify_unclos({
        "poe_count": 0, "name": "Cuba", "pol_type": "200NM", "anchor": [0, 22],
    })["code"] == "sovereign_entry"


def test_uk_listing_miss_is_not_a_coverage_gap():
    cowes = _listing("Cowes", slug="united-kingdom", country="United Kingdom",
                     iso2="GB", mrgid=5696)
    alofi = _listing("Alofi", slug="niue", country="Niue", iso2="NU", mrgid=8447)
    road = _listing(
        "Road Town", slug="british-virgin-islands",
        country="British Virgin Islands", iso2="VG", mrgid=8394,
        sovereign="United Kingdom",
    )
    scope = {5696, 8447, 8394}
    missed = compare_to_listing([], [cowes, alofi, road], run_mrgids=scope)
    names = [row["listing"]["name"] for row in missed["review"]["listing_only"]]
    assert names == ["Alofi", "Road Town"]
    assert missed["summary"]["listing_only"] == 2
    assert missed["summary"]["uk_app_entry_not_a_gap"] == 1
    # Denominator drops the UK miss: 2 real holes, 0 confident → coverage 0.
    assert missed["summary"]["coverage"] == 0.0

    only_uk = compare_to_listing([], [cowes], run_mrgids={5696})
    assert only_uk["review"]["listing_only"] == []
    assert only_uk["summary"]["uk_app_entry_not_a_gap"] == 1
    assert only_uk["summary"]["coverage"] is None

    run = {
        "mrgid": 5696,
        "name": "Cowes",
        "zone_name": "United Kingdom",
        "country_iso2": "GB",
        "dedup_key": f"5696:{normalize_name('Cowes')}",
    }
    hit = compare_to_listing([run], [cowes], run_mrgids={5696})
    assert hit["summary"]["confident"] == 1
    assert hit["summary"]["listing_only"] == 0
    assert hit["summary"]["uk_app_entry_not_a_gap"] == 0
    assert hit["summary"]["coverage"] == 1.0


def test_fiche_marks_uk_sheet_as_app_entry_without_a_gap():
    uk = assemble_zone_fiche(_uk_zone(mrgid=0, sources=[]), [])
    assert uk["entry_mode"] == "official_app"
    assert uk["data_gap"] is False
    france = {
        "mrgid": 0, "name": "France", "iso2": "FR", "sov_iso2": "FR",
        "pol_type": "200NM", "sources": [],
    }
    fr = assemble_zone_fiche(france, [])
    assert fr["entry_mode"] == "designated_port"
    assert fr["data_gap"] is True


def test_golden_rule_publishes_only_a_state_yacht_entry():
    yacht = {
        "name": "La Rochelle",
        "country_iso2": "FR",
        "note": "port de plaisance for a foreign sailing yacht",
        "source_urls": ["https://www.douane.gouv.fr/ports"],
    }
    assert may_publish(yacht, official_source=True) is True
    assert may_publish(yacht, official_source=False) is True
    assert may_publish(
        {"name": "Marina", "country_iso2": "HR", "note": "leisure=marina"},
        official_source=False,
    ) is False
    assert may_publish(
        {"name": "Tanker Berth", "judge_kind": "cargo",
         "note": "container terminal",
         "source_urls": ["https://customs.gov/terminal"]},
        official_source=True,
    ) is False
    # Whitelist host that is not a .gov name still needs the caller's flag.
    zoll = {"note": "pleasure craft clearance", "source_urls": ["https://zoll.de/x"]}
    assert may_publish(zoll, official_source=False) is False
    assert may_publish(zoll, official_source=True) is True
    # The UK regime is the State source: any UK port, no per-port decree.
    assert may_publish(
        {"name": "Falmouth", "country_iso2": "GB", "mrgid": 5696},
        official_source=False,
    ) is True
    assert may_publish({"name": "United Kingdom"}, official_source=False) is False


def test_noonsite_url_is_never_gold_and_both_arms_are():
    noon = "https://www.noonsite.com/place/brest"
    assert source_is_gold(noon, seen_by_top_down=True, seen_by_bottom_up=True) is False
    state = "https://douane.gouv.fr/ports"
    assert source_is_gold(state, seen_by_top_down=True, seen_by_bottom_up=True) is True
    assert source_is_gold(state, seen_by_top_down=True, seen_by_bottom_up=False) is False
    assert source_is_gold(state, seen_by_top_down=False, seen_by_bottom_up=True) is False
    assert noonsite_blocks_confirmation({"source_urls": [noon]}) is True
    assert noonsite_blocks_confirmation({"judge_sources": [noon]}) is True
    assert noonsite_blocks_confirmation({}) is False
    assert noonsite_blocks_confirmation({
        "source_urls": [noon],
        "sources_td": [state],
        "sources_bu": ["https://www.douane.gouv.fr/ports"],
    }) is False


def test_score_uses_the_rules_without_changing_a_state_source_score():
    base = {
        "name": "Papeete",
        "extraction_engine": "catalog",
        "extraction_agreement": True,
        "validated": True,
        "spatial_kind": "in_eez",
        "geocode_agree": True,
        "lat": -17.5,
        "lon": -149.5,
        "note": "catalogue officiel (nom + coordonnées dans la source)",
        "source_urls": ["https://douane.gouv.fr/x"],
        "osm_confidence": 0.9,
    }
    official = score_port(base, official_source=True)
    same = score_port({**base, "country_iso2": "FR"}, official_source=True)
    uk = score_port({**base, "country_iso2": "GB", "mrgid": 5696}, official_source=True)
    assert official["confidence"] == same["confidence"] == uk["confidence"]
    assert official["parts"] == uk["parts"]
    assert "source d'État" in official["reasons"]
    assert official["publishable"] is False
    assert official["publish_basis"] == "no_foreign_yacht_attestation"
    assert official["gold"] is False
    assert uk["publishable"] is True
    assert uk["publish_basis"] == "uk_official_app"
    assert uk["data_gap"] is False

    noon = "https://www.noonsite.com/place/papeete"
    blocked = score_port(
        {**base, "country_iso2": "PF", "source_urls": [noon],
         "sources_td": [noon], "sources_bu": [noon],
         "note": "foreign sailing yacht"},
        official_source=True,
    )
    assert blocked["publishable"] is False
    assert blocked["publish_basis"] == "noonsite_not_a_source"
    assert blocked["gold"] is False
    assert blocked["parts"]["source"] == 4
    assert "source d'État" not in blocked["reasons"]
    assert "Noonsite : signal, jamais une source Gold" in blocked["reasons"]
    assert reasons_en(blocked["reasons"])[blocked["reasons"].index(
        "Noonsite : signal, jamais une source Gold"
    )] == "Noonsite: signal only, never a Gold source"

    state = "https://www.douane.gouv.fr/ports-plaisance"
    both = score_port({
        **base,
        "note": "foreign sailing yacht clearance",
        "source_urls": [state],
        "sources_td": ["https://douane.gouv.fr/ports-plaisance"],
        "sources_bu": ["http://www.douane.gouv.fr/ports-plaisance"],
    }, official_source=True)
    assert both["gold"] is True
    assert both["publishable"] is True
    assert both["parts"]["source"] == 30
    one_arm = score_port({
        **base,
        "note": "foreign sailing yacht clearance",
        "source_urls": [state],
        "sources_td": [state],
        "sources_bu": ["https://douane.gouv.fr/autre"],
    }, official_source=True)
    assert one_arm["gold"] is False
    assert one_arm["publishable"] is True

    cargo = score_port({
        "name": "Tanker Berth",
        "judge_kind": "cargo",
        "note": "container terminal",
        "source_urls": ["https://customs.gov/terminal"],
        "sources_td": ["https://customs.gov/terminal"],
        "sources_bu": ["https://www.customs.gov/terminal"],
    }, official_source=True)
    assert cargo["gold"] is True
    assert cargo["publishable"] is False

    uk_noon = score_port({
        "name": "Plymouth",
        "country_iso2": "GB",
        "mrgid": 5696,
        "source_urls": ["https://noonsite.com/uk/plymouth"],
        "sources_td": ["https://noonsite.com/uk/plymouth"],
        "sources_bu": ["https://www.noonsite.com/uk/plymouth"],
    }, official_source=True)
    assert uk_noon["publishable"] is True
    assert uk_noon["publish_basis"] == "uk_official_app"
    assert uk_noon["gold"] is False
    assert uk_noon["data_gap"] is False
    assert "source d'État" not in uk_noon["reasons"]


def test_fiche_url_in_both_arms_is_gold_and_noonsite_is_not():
    zone = {
        "mrgid": 26518, "name": "Saba", "geoname": "Saba", "iso2": "BQ",
        "sov_iso2": "NL", "sovereign": "Netherlands", "pol_type": "200NM",
        "sources": [{"url": "https://gov.example/list", "domain": "gov.example"}],
    }
    ports = [{"_id": "p1", "name": "Fort Bay", "lat": 17.62, "lon": -63.25,
              "source_urls": ["https://www.noonsite.com/fort-bay"]}]
    seeds = [{"name": "Fort Bay", "judge_sources": [
        "https://gov.example/list",
        "https://noonsite.com/x",
    ]}]
    fiche = assemble_zone_fiche(zone, ports, seeds=seeds)
    assert fiche["url_td"]["url"] == "https://gov.example/list"
    assert fiche["url_td"]["from_arm"] == "both"
    assert fiche["url_td"]["gold"] is True
    assert fiche["ports"][0]["url_bu"]["gold"] is True
    blob = " ".join(
        rec.get("url") or ""
        for rec in (fiche["sources_td"] + fiche["sources_bu"] + fiche["ports"][0]["urls_bu"])
    )
    assert "noonsite" not in blob.lower()
    assert fiche["data_gap"] is False
    assert fiche["entry_mode"] == "designated_port"


def test_noonsite_url_does_not_confirm_and_does_not_light_review_gold():
    accepted = {"judge_status": "accepted"}
    noon = {
        "verify_verdict": "unverified",
        "seed_sources": ["listing"],
        "has_coords": True,
        "judge_sources": ["https://www.noonsite.com/place"],
    }
    assert apply_judge_verdict(noon, accepted) == "probable"
    plain = {
        "verify_verdict": "unverified",
        "seed_sources": ["listing"],
        "has_coords": True,
    }
    assert apply_judge_verdict(plain, accepted) == "confirmed"
    both = {
        **noon,
        "sources_td": ["https://douane.gouv.fr/liste"],
        "sources_bu": ["https://www.douane.gouv.fr/liste"],
    }
    assert apply_judge_verdict(both, accepted) == "confirmed"

    fiche = {"sources_td": [
        {"url": "https://www.noonsite.com/uk"},
        {"url": "https://www.gov.uk/submit-a-pleasure-craft-report"},
    ]}
    choices = {"td": {
        "https://www.noonsite.com/uk": "keep",
        "https://www.gov.uk/submit-a-pleasure-craft-report": "keep",
    }}
    assert kept_official_urls(fiche, choices) == [
        "https://www.gov.uk/submit-a-pleasure-craft-report",
    ]
