"""
ml_core.py — ML bootstrapping / weak supervision on existing data.

Records already in the database (projects + PoE) serve as the initial
training set ("treasure") — NO destructive writes on these collections.

- Relevance classifier (local Gatekeeper): TF-IDF + LogisticRegression
  trained on validated projects (positives) vs terrestrial corpus (negatives).
- Spatial anomaly detection on PoEs: IsolationForest (per-zone offsets)
  + per-zone haversine DBSCAN (corroboration). Non-destructive flags.
- NER dataset export (PORT_NAME / PROJECT_NAME / LOCATION) for spaCy.
"""
import asyncio
import json
import time

from app.config import MODELS_DIR

MODELS_DIR.mkdir(exist_ok=True)
GATEKEEPER_FILE = MODELS_DIR / "gatekeeper_tfidf_logreg.joblib"
ANOMALY_REPORT_FILE = MODELS_DIR / "poe_anomaly_report.json"
NER_DATASET_FILE = MODELS_DIR / "ner_dataset.jsonl"
NER_MODEL_DIR = MODELS_DIR / "ner_spacy"
NER_METRICS_FILE = MODELS_DIR / "ner_metrics.json"
SERP_MODEL_FILE = MODELS_DIR / "serp_classifier.joblib"

_gatekeeper_cache = None
_ner_cache = None
_serp_cache = None

# Synthetic terrestrial corpus — fills in real negatives (db.failed) that are too rare.
_LAND_NEGATIVES = [
    "Reforestation of the Amazon rainforest canopy to protect terrestrial biodiversity",
    "Mountain gorilla habitat protection in the Virunga highlands and inland forests",
    "Prairie grassland restoration for pollinators across the Great Plains farmland",
    "Freshwater lake water quality monitoring program in alpine watersheds",
    "Urban tree planting initiative to cool city streets and reduce heat islands",
    "Desert tortoise conservation in arid savanna and dune ecosystems",
    "River basin agriculture irrigation efficiency project for inland farmers",
    "Old-growth inland forest carbon sequestration and timber policy reform",
    "Alpine meadow wildflower restoration after ski resort development",
    "Soil health regeneration program for organic wheat and corn farmland",
    "Wolf reintroduction to mountain national parks and terrestrial corridors",
    "Highway wildlife crossing structures for deer and elk migration routes",
    "Peatland bog restoration in inland moorland far from any coast",
    "Community solar farm development on former mining land",
    "School curriculum on prairie ecosystem ecology and grassland birds",
    "Groundwater aquifer recharge project in continental drylands",
    "Rainforest canopy research station and terrestrial insect inventory",
    "National park trail maintenance and mountain hut renovation program",
    "Savanna elephant anti-poaching patrols in landlocked reserves",
    "Vineyard biodiversity program with hedgerows for terrestrial songbirds",
]


# ---------------------------------------------------------------------------
# Dataset statistics (Bottom-Up: the DB is the training set)
# ---------------------------------------------------------------------------
async def dataset_stats(db) -> dict:
    projects = await db.projects.count_documents({})
    poe = await db.poe_ports.count_documents({})
    poe_geocoded = await db.poe_ports.count_documents({"lat": {"$ne": None}})
    failed = await db.failed.count_documents({})
    osm_checked = await db.poe_ports.count_documents({"osm_checked_at": {"$exists": True}})
    anomalies = await db.poe_ports.count_documents({"spatial_anomaly": True})
    return {
        "projects": projects, "poe_ports": poe, "poe_geocoded": poe_geocoded,
        "failed_urls": failed, "poe_osm_checked": osm_checked,
        "poe_spatial_anomalies": anomalies,
        "total_training_entities": projects + poe,
    }


def models_status() -> dict:
    out = {"gatekeeper": None, "anomaly_report": None, "ner_dataset": None,
           "ner_model": None, "serp_classifier": None}
    if GATEKEEPER_FILE.exists():
        try:
            import joblib
            bundle = joblib.load(GATEKEEPER_FILE)
            out["gatekeeper"] = {k: bundle.get(k) for k in ("trained_at", "n_pos", "n_neg", "accuracy", "f1")}
        except Exception as e:
            out["gatekeeper"] = {"error": str(e)[:100]}
    if ANOMALY_REPORT_FILE.exists():
        try:
            out["anomaly_report"] = json.loads(ANOMALY_REPORT_FILE.read_text())["summary"]
        except Exception:
            pass
    if NER_DATASET_FILE.exists():
        out["ner_dataset"] = {"lines": sum(1 for _ in NER_DATASET_FILE.open())}
    if NER_METRICS_FILE.exists():
        try:
            out["ner_model"] = json.loads(NER_METRICS_FILE.read_text())
        except Exception:
            pass
    if SERP_MODEL_FILE.exists():
        try:
            import joblib
            bundle = joblib.load(SERP_MODEL_FILE)
            out["serp_classifier"] = {k: bundle.get(k) for k in ("trained_at", "n_pos", "n_neg", "accuracy", "f1")}
        except Exception as e:
            out["serp_classifier"] = {"error": str(e)[:100]}
    return out


# ---------------------------------------------------------------------------
# Relevance classifier (local Gatekeeper — weak supervision)
# ---------------------------------------------------------------------------
async def build_gatekeeper_dataset(db, max_docs: int = 8000):
    positives = []
    async for p in db.projects.find({}, {"title": 1, "description": 1, "location": 1}).limit(max_docs):
        txt = " ".join(str(p.get(k) or "") for k in ("title", "description", "location")).strip()
        if len(txt) > 20:
            positives.append(txt)
    negatives = []
    async for f in db.failed.find({"stage": "gatekeeper"}, {"reason": 1, "url": 1}).limit(2000):
        reason = str(f.get("reason") or "")
        if "terrestrial" in reason or "freshwater" in reason or "not marine" in reason.lower():
            negatives.append(f"{f.get('url', '')} {reason}")
    n_real_neg = len(negatives)
    # Weak supervision: fill in with the synthetic terrestrial corpus
    target_neg = max(len(_LAND_NEGATIVES) * 10, len(positives) // 3)
    i = 0
    while len(negatives) < target_neg:
        negatives.append(_LAND_NEGATIVES[i % len(_LAND_NEGATIVES)] + f" (variant {i // len(_LAND_NEGATIVES)})")
        i += 1
    return positives, negatives, n_real_neg


def _train_sync(positives: list[str], negatives: list[str]) -> dict:
    import joblib
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, f1_score

    texts = positives + negatives
    labels = [1] * len(positives) + [0] * len(negatives)
    Xtr_txt, Xte_txt, ytr, yte = train_test_split(texts, labels, test_size=0.15,
                                                  random_state=42, stratify=labels)
    vec = TfidfVectorizer(ngram_range=(1, 2), max_features=30000, sublinear_tf=True)
    Xtr = vec.fit_transform(Xtr_txt)
    Xte = vec.transform(Xte_txt)
    clf = LogisticRegression(max_iter=1000, C=2.0, class_weight="balanced")
    clf.fit(Xtr, ytr)
    pred = clf.predict(Xte)
    metrics = {
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_pos": len(positives), "n_neg": len(negatives),
        "accuracy": round(float(accuracy_score(yte, pred)), 4),
        "f1": round(float(f1_score(yte, pred)), 4),
    }
    joblib.dump({"vectorizer": vec, "model": clf, **metrics}, GATEKEEPER_FILE)
    return metrics


async def train_gatekeeper(db, log=None) -> dict:
    global _gatekeeper_cache
    log = log or (lambda m: None)
    positives, negatives, n_real_neg = await build_gatekeeper_dataset(db)
    if len(positives) < 100:
        raise ValueError(f"dataset trop petit ({len(positives)} positifs) — bootstrapping impossible")
    log(f"dataset weak supervision: {len(positives)} positifs (BDD projets), "
        f"{len(negatives)} négatifs ({n_real_neg} réels + synthétiques)")
    metrics = await asyncio.to_thread(_train_sync, positives, negatives)
    metrics["n_neg_real"] = n_real_neg
    _gatekeeper_cache = None  # invalidate
    log(f"modèle entraîné: accuracy={metrics['accuracy']} f1={metrics['f1']} → {GATEKEEPER_FILE.name}")
    return metrics


def _load_gatekeeper():
    global _gatekeeper_cache
    if _gatekeeper_cache is not None:
        return _gatekeeper_cache
    if not GATEKEEPER_FILE.exists():
        return None
    try:
        import joblib
        _gatekeeper_cache = joblib.load(GATEKEEPER_FILE)
    except Exception:
        return None
    return _gatekeeper_cache


def predict_relevance(text: str):
    """Marine score [0-1] from the local classifier, or None if the model is missing/too weak."""
    bundle = _load_gatekeeper()
    if not bundle or bundle.get("n_pos", 0) < 500 or not text:
        return None
    try:
        X = bundle["vectorizer"].transform([text])
        score = float(bundle["model"].predict_proba(X)[0][1])
        return {"score": round(score, 4), "marine": score >= 0.5}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PoE spatial anomalies (IsolationForest + DBSCAN — non-destructive)
# ---------------------------------------------------------------------------
async def scan_poe_anomalies(db, state=None, contamination: float = 0.05) -> dict:
    from app.core.geo import isolation_forest_scores, dbscan_noise_flags, haversine_km
    log = state.log if state else (lambda m: None)
    ports = await db.poe_ports.find({"lat": {"$ne": None}, "lon": {"$ne": None}}).to_list(20000)
    if len(ports) < 20:
        raise ValueError(f"seulement {len(ports)} PoE géocodés — scan non significatif")
    if state:
        state.total = len(ports)
    log(f"{len(ports)} PoE géocodés chargés (jeu Bottom-Up)")

    # Group by zone + zone median
    zones: dict[int, list[dict]] = {}
    for p in ports:
        zones.setdefault(p.get("mrgid") or 0, []).append(p)

    def _median(vals):
        s = sorted(vals)
        return s[len(s) // 2]

    features, dbscan_flags = [], {}
    for mrgid, zports in zones.items():
        mlat = _median([p["lat"] for p in zports])
        mlon = _median([p["lon"] for p in zports])
        coords = [(p["lat"], p["lon"]) for p in zports]
        try:
            noise = dbscan_noise_flags(coords, eps_km=150.0, min_samples=2)
        except Exception:
            noise = [False] * len(zports)
        for p, n in zip(zports, noise):
            dbscan_flags[p["_id"]] = n
            dx = haversine_km(p["lat"], p["lon"], mlat, p["lon"])
            dy = haversine_km(p["lat"], p["lon"], p["lat"], mlon)
            features.append([dx, dy, float(p.get("distance_km") or 0.0)])

    ordered = [p for zports in zones.values() for p in zports]
    labels, scores = isolation_forest_scores(features, contamination=contamination)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    flagged, cleared = 0, 0
    details = []
    for i, p in enumerate(ordered):
        iso_anom = labels[i] == -1
        db_anom = dbscan_flags.get(p["_id"], False)
        is_anom = iso_anom and (db_anom or float(p.get("distance_km") or 0) > 100 or abs(scores[i]) > 0.15)
        methods = [m for m, f in (("isolation_forest", iso_anom), ("dbscan_noise", db_anom)) if f]
        if is_anom:
            flagged += 1
            details.append({"id": p["_id"], "name": p.get("name"), "zone": p.get("zone_name"),
                            "lat": p["lat"], "lon": p["lon"], "score": round(float(scores[i]), 4),
                            "methods": methods})
            await db.poe_ports.update_one({"_id": p["_id"]}, {"$set": {
                "spatial_anomaly": True, "anomaly_score": round(float(scores[i]), 4),
                "anomaly_methods": methods, "anomaly_checked_at": now,
            }})
        else:
            if p.get("spatial_anomaly"):
                cleared += 1
            await db.poe_ports.update_one({"_id": p["_id"]}, {"$set": {
                "spatial_anomaly": False, "anomaly_checked_at": now,
            }})
        if state:
            state.progress = i + 1

    summary = {"ports_checked": len(ordered), "anomalies_flagged": flagged,
               "previously_flagged_cleared": cleared, "zones": len(zones), "checked_at": now}
    ANOMALY_REPORT_FILE.write_text(json.dumps({"summary": summary, "anomalies": details[:200]},
                                              ensure_ascii=False, indent=2))
    log(f"scan terminé: {flagged} anomalie(s) flaguée(s) sur {len(ordered)} PoE — rapport écrit")
    return summary


# ---------------------------------------------------------------------------
# NER dataset (spaCy prep — PORT_NAME / PROJECT_NAME / LOCATION)
# ---------------------------------------------------------------------------
def _ner_spans(text: str, needle: str, label: str) -> list | None:
    if not needle:
        return None
    idx = text.find(needle)
    if idx < 0:
        return None
    return [idx, idx + len(needle), label]


def _regulatory_port_examples(name: str, city: str, zone: str) -> list[dict]:
    """Regulatory sentences around a real port name — the catalog
    `{name}, {city} ({zone})` alone left NER mute on official HTML."""
    city = (city or "").strip()
    zone = (zone or "").strip()
    templates = [
        f"The designated ports of entry for foreign pleasure craft include {name}.",
        f"Foreign yachts must clear customs at {name}" + (f" in {zone}." if zone else "."),
        f"Les ports d'entrée officiels pour la plaisance sont {name}"
        + (f" ({zone})." if zone else "."),
        f"Official clearance is available at {name}. Pleasure craft shall report to customs on arrival.",
        f"Port of entry: {name} — designated harbour for foreign vessels"
        + (f" in {zone}." if zone else "."),
    ]
    if city:
        templates.append(f"Puerto de entrada habilitado: {name}, {city}.")
    out = []
    for text in templates:
        span = _ner_spans(text, name, "PORT_NAME")
        if not span:
            continue
        ents = [span]
        if zone:
            loc = _ner_spans(text, zone, "LOCATION")
            if loc and loc[0] >= span[1]:
                ents.append(loc)
        out.append({"text": text[:500], "entities": ents})
    return out


async def export_ner_dataset(db) -> dict:
    lines = 0
    with NER_DATASET_FILE.open("w", encoding="utf-8") as f:
        async for p in db.projects.find({}, {"title": 1, "description": 1, "location": 1}).limit(10000):
            title = str(p.get("title") or "").strip()
            loc = str(p.get("location") or "").strip()
            desc = str(p.get("description") or "").strip()
            if not title:
                continue
            text = f"{title}. {desc}"
            ents = [[0, len(title), "PROJECT_NAME"]]
            if loc and loc in text:
                idx = text.find(loc)
                ents.append([idx, idx + len(loc), "LOCATION"])
            f.write(json.dumps({"text": text[:500], "entities": ents}, ensure_ascii=False) + "\n")
            lines += 1
        n_ports = 0
        async for p in db.poe_ports.find({}, {"name": 1, "city": 1, "zone_name": 1, "note": 1}).limit(10000):
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            zone = str(p.get("zone_name") or "").strip()
            city = str(p.get("city") or "").strip()
            text = f"{name}, {city} ({zone}). {p.get('note') or ''}".strip()
            ents = [[0, len(name), "PORT_NAME"]]
            if zone and zone in text:
                idx = text.find(zone)
                ents.append([idx, idx + len(zone), "LOCATION"])
            f.write(json.dumps({"text": text[:500], "entities": ents}, ensure_ascii=False) + "\n")
            lines += 1
            n_ports += 1
            # 2 regulatory sentences per port (all) + 3 more for a
            # sample, to learn context without exploding the dataset.
            extra = _regulatory_port_examples(name, city, zone)
            chosen = extra[:2] if n_ports % 4 else extra
            for row in chosen:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                lines += 1
        # Hard anchors: tests and the real run cite these phrasings.
        for name, city, zone in (
            ("Port de Papeete", "Tahiti", "French Polynesia"),
            ("Alofi", "Alofi", "Niue"),
            ("Ensenada", "Baja California", "Mexico"),
            ("Valletta", "Valletta", "Malta"),
        ):
            for row in _regulatory_port_examples(name, city, zone):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                lines += 1
    return {"file": str(NER_DATASET_FILE), "lines": lines}


# ---------------------------------------------------------------------------
# Custom spaCy NER (PORT_NAME / PROJECT_NAME / LOCATION) — no LLM
# ---------------------------------------------------------------------------
def _train_ner_sync(n_iter: int = 12, log=print) -> dict:
    import random
    import spacy
    from spacy.training import Example
    from spacy.util import minibatch, compounding

    raw = [json.loads(l) for l in NER_DATASET_FILE.open(encoding="utf-8")]
    nlp = spacy.blank("xx")
    ner = nlp.add_pipe("ner")
    labels = {lbl for d in raw for _, _, lbl in d["entities"]}
    for lbl in sorted(labels):
        ner.add_label(lbl)
    examples, dropped = [], 0
    for d in raw:
        doc = nlp.make_doc(d["text"])
        try:
            ex = Example.from_dict(doc, {"entities": [tuple(e) for e in d["entities"]]})
        except Exception:
            dropped += 1
            continue
        if not ex.reference.ents:
            dropped += 1
            continue
        examples.append(ex)
    random.Random(42).shuffle(examples)
    split = max(1, int(len(examples) * 0.1))
    dev, train = examples[:split], examples[split:]
    log(f"NER: {len(train)} exemples train / {len(dev)} dev ({dropped} désalignés ignorés) — labels: {sorted(labels)}")
    optimizer = nlp.initialize(get_examples=lambda: train[:200])
    losses = {}
    for it in range(n_iter):
        random.shuffle(train)
        losses = {}
        for batch in minibatch(train, size=compounding(16.0, 128.0, 1.2)):
            nlp.update(batch, drop=0.2, losses=losses, sgd=optimizer)
        log(f"NER: itération {it + 1}/{n_iter} — loss {losses.get('ner', 0):.1f}")
    scores = nlp.evaluate(dev)
    NER_MODEL_DIR.mkdir(exist_ok=True)
    nlp.to_disk(NER_MODEL_DIR)
    metrics = {
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_train": len(train), "n_dev": len(dev), "dropped": dropped,
        "labels": sorted(labels),
        "ents_f": round(float(scores.get("ents_f") or 0), 4),
        "ents_p": round(float(scores.get("ents_p") or 0), 4),
        "ents_r": round(float(scores.get("ents_r") or 0), 4),
        "final_loss": round(float(losses.get("ner", 0)), 2),
    }
    NER_METRICS_FILE.write_text(json.dumps(metrics, indent=2))
    return metrics


async def train_ner(db, log=None, n_iter: int = 12) -> dict:
    """NER bootstrapping: rebuild the dataset from the DB (weak supervision)
    then train a local spaCy model. No writes on the collections."""
    global _ner_cache
    log = log or (lambda m: None)
    ds = await export_ner_dataset(db)
    log(f"dataset NER regénéré depuis la BDD: {ds['lines']} entités")
    if ds["lines"] < 200:
        raise ValueError(f"dataset NER trop petit ({ds['lines']} lignes)")
    metrics = await asyncio.to_thread(_train_ner_sync, n_iter, log)
    _ner_cache = None
    log(f"NER entraîné: F1={metrics['ents_f']} P={metrics['ents_p']} R={metrics['ents_r']}")
    return metrics


def has_ner_model() -> bool:
    return NER_MODEL_DIR.exists()


def _load_ner():
    global _ner_cache
    if _ner_cache is not None:
        return _ner_cache
    if not NER_MODEL_DIR.exists():
        return None
    try:
        import spacy
        _ner_cache = spacy.load(NER_MODEL_DIR)
    except Exception:
        return None
    return _ner_cache


def extract_entities(text: str) -> list[dict]:
    """Local entity extraction (PORT_NAME / PROJECT_NAME / LOCATION), no LLM."""
    nlp = _load_ner()
    if nlp is None or not text:
        return []
    out = []
    for start in range(0, min(len(text), 40000), 4000):
        doc = nlp(text[start:start + 4000])
        for ent in doc.ents:
            out.append({"text": ent.text.strip(), "label": ent.label_,
                        "start": start + ent.start_char, "end": start + ent.end_char})
    seen, uniq = set(), []
    for e in out:
        k = (e["text"].lower(), e["label"])
        if e["text"] and k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


# ---------------------------------------------------------------------------
# SERP classifier (weak supervision) — predicts whether a search-result URL
# leads to an official port list, BEFORE any download.
# Positives: source URLs that actually produced PoEs in the DB.
# Negatives: synthetic corpus of tourism/blog/aggregator URLs.
# ---------------------------------------------------------------------------
_SERP_NEG_TEMPLATES = [
    "https://www.tripadvisor.com/Attractions-g{i}-Activities-{c}.html",
    "https://www.booking.com/searchresults.html?ss={c}+marina+hotel",
    "https://en.wikipedia.org/wiki/Tourism_in_{c}",
    "https://www.cruisecritic.com/ports/newparent.cfm?port={c}",
    "https://sailing-adventures.blogspot.com/2024/05/our-trip-to-{c}-beaches.html",
    "https://forum.cruisersforum.com/threads/best-anchorages-{c}.{i}/",
    "https://www.expedia.com/{c}-Vacations.d{i}.Travel-Guide",
    "https://www.marinetraffic.com/en/ais/details/ports/{i}/{c}",
    "https://maritime-news.com/{c}-yacht-show-highlights-{i}",
    "https://www.pinterest.com/pin/{i}-sailing-{c}/",
    "https://www.youtube.com/watch?v=sail{i}",
    "https://travel-guide.net/{c}/top-10-beaches-and-resorts",
    "https://yacht-charter-deals.com/destinations/{c}/prices",
    "https://dutyfree-shopping.com/{c}/airport-terminal",
    "https://baggage-allowances.info/{c}/customs-rules-for-tourists",
    "https://www.hotels.com/{c}/waterfront-resorts",
    "https://medium.com/@sailor{i}/exploring-{c}-by-boat",
    "https://www.reddit.com/r/sailing/comments/{i}/visiting_{c}/",
]


async def build_serp_dataset(db):
    pos = set()
    async for p in db.poe_ports.find({}, {"source_urls": 1}):
        for u in (p.get("source_urls") or []):
            if isinstance(u, str) and u.startswith("http"):
                pos.add(u.strip())
    async for z in db.eez_zones.find({"poe_count": {"$gt": 0}}, {"sources": 1}):
        for s in (z.get("sources") or []):
            u = (s or {}).get("url")
            if isinstance(u, str) and u.startswith("http"):
                pos.add(u.strip())
    names = []
    async for z in db.eez_zones.find({}, {"name": 1}):
        n = (z.get("name") or "").strip().replace(" ", "-")
        if n:
            names.append(n)
    names = names or ["Atlantis"]
    neg, i = [], 0
    target = max(len(pos), 300)
    while len(neg) < target:
        neg.append(_SERP_NEG_TEMPLATES[i % len(_SERP_NEG_TEMPLATES)].format(
            c=names[i % len(names)], i=10000 + i))
        i += 1
    return sorted(pos), neg


def _train_serp_sync(pos: list[str], neg: list[str]) -> dict:
    import joblib
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, f1_score

    urls = pos + neg
    labels = [1] * len(pos) + [0] * len(neg)
    Xtr_u, Xte_u, ytr, yte = train_test_split(urls, labels, test_size=0.15,
                                              random_state=42, stratify=labels)
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), max_features=40000)
    Xtr = vec.fit_transform(Xtr_u)
    Xte = vec.transform(Xte_u)
    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    clf.fit(Xtr, ytr)
    pred = clf.predict(Xte)
    metrics = {
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_pos": len(pos), "n_neg": len(neg),
        "accuracy": round(float(accuracy_score(yte, pred)), 4),
        "f1": round(float(f1_score(yte, pred)), 4),
    }
    joblib.dump({"vectorizer": vec, "model": clf, **metrics}, SERP_MODEL_FILE)
    return metrics


async def train_serp(db, log=None) -> dict:
    global _serp_cache
    log = log or (lambda m: None)
    pos, neg = await build_serp_dataset(db)
    if len(pos) < 50:
        raise ValueError(f"seulement {len(pos)} URLs sources positives en base — entraînement non significatif")
    log(f"dataset SERP weak supervision: {len(pos)} URLs sources réelles (BDD PoE), {len(neg)} négatifs synthétiques")
    metrics = await asyncio.to_thread(_train_serp_sync, pos, neg)
    _serp_cache = None
    log(f"classifieur SERP entraîné: accuracy={metrics['accuracy']} f1={metrics['f1']}")
    return metrics


def _load_serp():
    global _serp_cache
    if _serp_cache is not None:
        return _serp_cache
    if not SERP_MODEL_FILE.exists():
        return None
    try:
        import joblib
        _serp_cache = joblib.load(SERP_MODEL_FILE)
    except Exception:
        return None
    return _serp_cache


def predict_serp(url: str):
    """Probability [0-1] that the URL leads to an official port list.
    None if the model is missing or trained on too little data."""
    bundle = _load_serp()
    if not bundle or bundle.get("n_pos", 0) < 50 or not url:
        return None
    try:
        X = bundle["vectorizer"].transform([url])
        score = float(bundle["model"].predict_proba(X)[0][1])
        return {"score": round(score, 4), "official_list": score >= 0.5}
    except Exception as e:
        print(f"[ml_core] predict_serp failed: {type(e).__name__}: {e}")
        return None
