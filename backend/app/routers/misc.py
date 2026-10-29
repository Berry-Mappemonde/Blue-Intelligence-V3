"""app.routers.misc — Health, settings, user manual, official route,
route EEZ crossings."""
import asyncio
import math
import os
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.config import ROUTE_FILE
from app.core.tasks import TaskState
from app.db import db, get_settings
from app.services.swarm_pipeline import now_iso
from app.services.zee_crossings import (
    EEZ_FILE,
    build_zee_crossings as run_build_zee_crossings,
    crossings_to_summary as zee_crossings_summary,
    filter_french_territories,
)

router = APIRouter(prefix="/api")

@router.get("/")
async def health():
    # `mongo` : cible de persistance ("atlas" = durable, "local" = pod
    # ephemeral — runs would be lost when the environment stops).
    import os
    url = (os.environ.get("MONGO_URL") or "").lower()
    if "mongodb+srv" in url or "mongodb.net" in url:
        mongo = "atlas"
    elif "localhost" in url or "127.0.0.1" in url:
        mongo = "local"
    else:
        mongo = "other"
    return {"service": "Blue Intelligence", "status": "operational",
            "mongo": mongo, "ts": now_iso()}


@router.get("/run-rules")
async def read_run_rules(mode: str | None = None, profile: str | None = None,
                         from_settings: bool = True):
    """Tunable-rules catalog + snapshot that would be taken for a run.

    `from_settings=false`: profile values (catalog + profile overrides),
    without merging Mongo settings — to fill the Console form.
    """
    from app.core.run_rules import RuleError, public_catalog, snapshot_for_run
    try:
        catalog = public_catalog(mode)
        settings = await get_settings() if from_settings else {}
        preview = snapshot_for_run(mode=mode, settings=settings, profile=profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    catalog["preview"] = {
        "hash": preview["hash"],
        "profile": preview["profile"],
        "counts": preview["counts"],
        "chosen": {
            k: {"value": v["value"], "source": v["source"], "unit": v["unit"]}
            for k, v in preview["chosen"].items()
        },
    }
    return catalog

class SettingsBody(BaseModel):
    nvidia_api_key: str | None = None
    llm_provider: str | None = None
    openrouter_api_key: str | None = None
    tinyfish_api_key: str | None = None
    serper_api_key: str | None = None
    tinyfish_agents: int | None = None
    extract_concurrency: int | None = None
    nvidia_max_concurrency: int | None = None
    max_coast_km: float | None = None
    max_inland_km: float | None = None
    min_marine_score: float | None = None
    gatekeeper_accept: float | None = None
    gatekeeper_reject: float | None = None
    allow_tinyfish_agent: bool | None = None
    test_max_urls_per_seed: int | None = None
    full_max_urls_per_seed: int | None = None
    min_zoom: int | None = None
    max_markers: int | None = None
    follow_the_money: bool | None = None
    max_partner_orgs: int | None = None
    ftm_min_s_ocean: float | None = None
    discover_concurrency: int | None = None
    listing_judge_concurrency: int | None = None
    saturation_limit: int | None = None
    rescan_after_days: float | None = None
    marina_search_radius_nm: float | None = None
    marina_batch_concurrency: int | None = None
    openrouter_min_credits_usd: float | None = None
    enrich_stale_days: int | None = None
    anthropic_api_key: str | None = None
    claude_budget_usd: float | None = None

# Map controls are not catalog rules. The settings form already publishes these bounds.
_MAP_SETTING_INTERVALS = {
    "min_zoom": (1, 8),
    "max_markers": (50, 5000),
}


def _settings_interval(key: str):
    """(rule id, [lo, hi]) from run_rules.json, else the map-form bounds."""
    extra = _MAP_SETTING_INTERVALS.get(key)
    if extra is not None:
        return key, extra
    from app.core.run_rules import all_rules
    for rule in all_rules():
        if rule.get("settings_key") == key and rule.get("interval") is not None:
            return rule["id"], rule["interval"]
    return None, None


def settings_out_of_range(updates: dict) -> str | None:
    """English detail when a numeric update leaves the catalog interval."""
    from app.core.run_rules import RuleError, _assert_in_interval
    problems = []
    for key, value in updates.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{key} must be a finite number")
            continue
        rid, interval = _settings_interval(key)
        if not interval:
            continue
        try:
            _assert_in_interval(rid, value, interval)
        except RuleError:
            lo, hi = interval
            problems.append(f"{key} must be between {lo} and {hi}")
    if not problems:
        return None
    return "; ".join(problems)


@router.get("/admin/check")
async def admin_check():
    """Validate the admin key: the `_admin_gate` middleware returns 401 before
    reaching here if ADMIN_KEY is set and the header is missing/wrong."""
    return {"ok": True,
            "admin_required": bool(os.environ.get("ADMIN_KEY", "").strip())}


@router.get("/settings")
async def read_settings():
    s = await get_settings()
    s.pop("_id", None)
    # Clean inherited keys from older versions (Gemini/Emergent/Cloudflare).
    for legacy in ("gemini_api_key", "cloudflare_model",
                   "gatekeeper_model", "extract_model", "extraction_engine"):
        s.pop(legacy, None)
    if s.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY"):
        s["nvidia_api_key_set"] = True
        s["nvidia_api_key"] = ""
    else:
        s["nvidia_api_key_set"] = False
    if s.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY"):
        s["openrouter_api_key_set"] = True
        s["openrouter_api_key"] = ""
    else:
        s["openrouter_api_key_set"] = False
    if s.get("tinyfish_api_key") or os.environ.get("TINYFISH_API_KEY"):
        s["tinyfish_api_key_set"] = True
        s["tinyfish_api_key"] = ""
    else:
        s["tinyfish_api_key_set"] = False
    if s.get("serper_api_key") or os.environ.get("SERPER_API_KEY"):
        s["serper_api_key_set"] = True
        s["serper_api_key"] = ""
    else:
        s["serper_api_key_set"] = False
    try:
        s["claude_budget_usd"] = float(s.get("claude_budget_usd") or 0)
    except (TypeError, ValueError):
        s["claude_budget_usd"] = 0.0
    from app.core.claude import usage_public
    s.update(usage_public(s))
    if s.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"):
        s["anthropic_api_key_set"] = True
        s["anthropic_api_key"] = ""
    else:
        s["anthropic_api_key_set"] = False
    return s


@router.put("/settings")
async def write_settings(body: SettingsBody):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    for k in ("openrouter_api_key", "tinyfish_api_key", "serper_api_key",
              "anthropic_api_key", "nvidia_api_key"):
        if k in updates and updates[k] == "":
            del updates[k]
    if updates:
        detail = settings_out_of_range(updates)
        if detail:
            raise HTTPException(422, detail)
        await db.settings.update_one({"_id": "global"}, {"$set": updates}, upsert=True)
    return await read_settings()

MANUALS = {
    "en": """# Blue Intelligence — User Manual

## Overview
Blue Intelligence turns the living web of maritime data into an executable geospatial database. The app offers six complementary modes, an operator console, and contextual GeoJSON exports. All AI-generated content is produced through OpenRouter (model configurable via the OPENROUTER_MODEL environment variable). Default language is French; default basemap is the nautical chart.

## Six modes (header switch)
The header pill lets you switch between six modes: Projects, Marinas, Offices, Ports of entry, MPAs, Science. Each mode paints the app with its own accent theme and shows its dedicated sidebar and map layer.

### 1) Projects (cyan)
- **World map**: single-world Leaflet map (nautical / dark / light). Project markers are coloured by category — tiny canvas dots, no clusters. The Berry-Mappemonde route is drawn as a neutral polyline.
- **Popup**: click a marker → photo, title, funder, category, description, S_ocean score and a "View project" link. The popup always stays fully on screen without moving the map.
- **Left sidebar**:
  - *Legend*: 9 color-coded categories (Conservation, Research, Fisheries, Policy & Advocacy, Pollution, Coastal & Habitat, Education, Restoration, Other). Click a category to filter.
  - *Organization filter* and *Category filter* dropdowns.
  - *Instant search* across titles, descriptions, funders and locations.
  - *Project list* with source links.
  - *"Missing project?"*: report a project we missed. It is emailed to the team and queued for the next Swarm run.
  - *Export GeoJSON* button — exports the projects visible in this mode.

### 2) Marinas (red)
- **Map**: world catalog of OpenStreetMap `leisure=marina` points (tiny canvas dots, no clusters). All points stay visible. A larger red dot marks a marina whose Google Maps `/place/` page was found (TinyFish Search or OSM tag) — we do not scrape Maps or wait for the `/search` → `/place` redirect. Each popup has the OSM website tag (unchecked) and a Google Maps link (place page when known, otherwise the deterministic search URL).
- **Left sidebar**: search by name or OSM id (list capped at 250 rows). Attribution: © OpenStreetMap contributors (ODbL).
- *Export GeoJSON* — slim FeatureCollection, uncapped.
- The world dump is started from the Console (resumable tiles, no purge). Formalities / Ports of Entry stay a separate job.

### 3) Formalities (amber)
- **Map**: world choropleth of the ~285 Exclusive Economic Zones (EEZ, Marine Regions/VLIZ v12) coloured by generation status, plus amber markers for every extracted official Port of Entry (pleasure craft). Click an EEZ to open its sheet.
- **Left sidebar**: searchable list of all EEZs (flag, sovereign, status, PoE count), a status filter and the permanent amber disclaimer ("Indicative information — verify with the authorities before departure").
- **EEZ sheet (map popup)**: status pill, PoE count, generation date, official sources used (clickable), and a Generate / Regenerate button that runs the full pipeline for that zone.
- **Status of a zone** (4 possible values):
  - `not generated` — grey, no AI content yet.
  - `AI · official sources` — amber, the sources passed the auto-generated government-domain whitelist.
  - `AI · no official source` — amber dashed, content extracted but no whitelisted official domain could be captured.
  - `error` — red, no port of entry could be extracted (typical for disputed rocks or landlocked claims).
- **Port of Entry popup**: name, town, note, geocoding source, and a spatial-validation badge (inside the EEZ / outside with distance).
- **Stale flag**: any zone older than 180 days shows a clock badge inviting a refresh. Re-extraction only happens when the source content changed (MD5 monitoring).
- *Export GeoJSON* button — exports all extracted Ports of Entry.

### 4) MPA / AMP (green)
- **Map**: ProtectedSeas Navigator polygons, coloured by Level of Fishing Protection (LFP 1–5). Zoom in on a coast (zoom ≥ 5); the world view only shows the invite, not 20 000 polygons.
- **Popup**: two separate URLs. Grey = manager website (ProtectedSeas `url` / Website). Green = visit / entry procedures. The visit URL is never a copy of the manager homepage.
- **Left sidebar**: search by site, country or designation; LFP filter; list of areas in the current tile.
- **Review**: admin tab only (hidden in Science). It is never a map overlay.
- *Export GeoJSON* — centroids + `manager_url` / `visit_url`, not official boundaries.

## Swarm Intelligence Audit (header toggle)
Operator console reserved for the crew / admin. It groups **all batch triggers** in one place (the "Swarm Intelligence Hub"):
- **Launch**: every mode uses the same Test | Full | Launch (+ Stop) chrome.
- **Projects — Swarm**: Test (few portals) or Full (all seeds). Isolated run; the live map is unchanged until you pick the run.
- **Marinas — World dump**: Test = one coastal tile. Full = worldwide dump then enrich by priority; also copies to the public map. Mouillages are worldwide (no 25 NM corridor).
- **Capitaineries — World dump**: tiled Overpass `office=harbour_master` / `seamark:building:function=harbour_master` worldwide, then SHOM WFS `buisgl_point` FUNCTN=2 plus SMCFAC CATSCF=6, then NOAA ENC Direct BUISGL FUNCTN=2. Phone and VHF from tags, TinyFish Search/Fetch + regex, NVIDIA NIM page chain (Pro → gpt-oss → Muse), then OpenRouter. No marina linking.
- **Formalities — EEZ referential & PoE batch**: build/refresh the world EEZ referential (VLIZ Marine Regions), then generate the Ports of Entry per zone in batches (5/10/25/all), with live logs, per-zone results and a Stop button.
- **MPA / AMP**: cache coverage and visit-URL coverage. A Console job fills visit URLs like Marinas Google-place: ProtectedSeas extra links, then TinyFish Fetch on the manager page, then Search. Never the manager homepage.
- **KPIs, telemetry table, failed extractions** for the projects pipeline, with Force Extract (TinyFish) per URL or global.

## Settings (right panel, gear icon)
- **Documentation**: download this manual (EN/FR).
- **Data**: Import GeoJSON (validates coordinates, counts and merges duplicates), Export GeoJSON, Clear all projects.
- **Marine filtering**: max coast distance (km), minimum marine score.
- **Extraction**: parallel TinyFish agents (1–2), extraction concurrency (1–20), Follow the Money toggle, Auto-Stop limit.
- **Map**: minimum zoom, max markers.
- **API keys**: TinyFish and OpenRouter (stored server-side, never exposed).

## Pipeline (how it works)
1. **Discovery**: TinyFish web agents navigate foundation portals (SSE live streaming, polling fallback, HTTP crawler fallback).
2. **Extraction**: Readability cleans the page → LLM Gatekeeper rejects terrestrial/freshwater projects → LLM extracts title, description (<250 chars), location, category, partners and S_ocean score.
3. **Geocoding**: extracted GPS → Nominatim → LLM smart geocoding → Point-in-Ocean test → coastal snapping when inland.
4. **Deduplication**: URL match, spatial proximity (<500 m) + title similarity (>90%) → funders merged.
5. **Ports of Entry pipeline (Formalities mode)**: search engines (SearXNG, OpenRouter web search) find the official customs/immigration sources of each EEZ; a whitelist of government domains (auto-generated from ISO codes + Public Suffix List + exceptions file) filters them; the pages/PDFs are parsed (trafilatura / PyMuPDF); a light LLM extracts the official PoE list as strict JSON; each port is geocoded (Nominatim/GeoNames) and spatially validated inside its EEZ polygon (shapely). MD5 hashes of the sources prevent useless re-extraction. The pipeline never invents content.
""",
    "fr": """# Blue Intelligence — Manuel utilisateur

## Vue d'ensemble
Blue Intelligence transforme le web vivant des données maritimes en base géospatiale exploitable. L'application propose six modes complémentaires, une console opérateur et des exports GeoJSON contextuels. Tous les contenus produits par IA le sont via OpenRouter (modèle configurable via la variable d'environnement OPENROUTER_MODEL). Langue par défaut : français. Fond par défaut : carte marine.

## Six modes (bascule dans l'en-tête)
La pastille de l'en-tête permet de basculer entre six modes : Projets, Marinas, Capitaineries, Ports d’entrée, AMP, Science. Chaque mode habille l'app avec sa teinte d'accent propre et affiche son bandeau et sa couche de carte dédiés.

### 1) Projets (cyan)
- **Carte mondiale** : carte Leaflet à monde unique (marine / sombre / clair). Marqueurs de projets colorés par catégorie — pastilles canvas, sans clusters. La route Berry-Mappemonde est une polyline neutre.
- **Popup** : cliquer un marqueur → photo, titre, financeur, catégorie, description, score S_ocean et lien « Voir le projet ». L'encadré reste toujours entièrement visible sans déplacer la carte.
- **Bandeau gauche** :
  - *Légende* : 9 catégories colorées (Conservation, Recherche, Pêcheries, Politique & Plaidoyer, Pollution, Côtes & Habitats, Éducation, Restauration, Autre). Cliquer une catégorie filtre la carte.
  - Menus *Filtre par organisation* et *Filtre par catégorie*.
  - *Recherche instantanée* sur titres, descriptions, financeurs et lieux.
  - *Liste des projets* avec liens sources.
  - *« Projet manquant ? »* : signaler un projet oublié. Un email est envoyé à l'équipe et le projet est mis en file pour le prochain run du Swarm.
  - Bouton *Export GeoJSON* — exporte les projets visibles dans ce mode.

### 2) Marinas (rouge)
- **Carte** : catalogue mondial OpenStreetMap `leisure=marina` (pastilles canvas, sans clusters). Tous les points restent visibles. Un point rouge plus gros signale une fiche Google `/maps/place/` déjà trouvée (TinyFish Search ou tag OSM) — pas de scrape Maps, pas d'attente de la redirection `/search` → `/place`. Chaque popup affiche le site tagué OSM (non vérifié) et un lien Google Maps (fiche `/place/` si connue, sinon recherche déterministe).
- **Bandeau gauche** : recherche par nom ou id OSM (liste plafonnée à 250 lignes). Attribution : © les contributeurs OpenStreetMap (ODbL).
- *Export GeoJSON* — FeatureCollection maigre, non plafonnée.
- Le dump mondial se lance depuis la Console (tuiles reprenables, pas de purge). Les Formalités / Ports d'Entrée restent un autre job.

### 3) Formalités (ambre)
- **Carte** : choroplèthe mondiale des ~285 Zones Économiques Exclusives (ZEE, Marine Regions/VLIZ v12) colorées par statut de génération, plus des marqueurs ambre pour chaque Port d'Entrée officiel extrait (plaisance). Cliquez une ZEE pour ouvrir sa fiche.
- **Bandeau gauche** : liste de toutes les ZEE avec recherche (drapeau, souverain, statut, nombre de PoE), un filtre par statut et le disclaimer ambre permanent (« Informations indicatives — à vérifier auprès des autorités avant le départ »).
- **Fiche ZEE (popup carte)** : pastille de statut, nombre de PoE, date de génération, sources officielles utilisées (cliquables). Pas de bouton Générer / Régénérer.
- **Statut d'une zone** (4 valeurs possibles) :
  - `non générée` — gris, aucun contenu IA pour l'instant.
  - `IA · sources officielles` — ambre, les sources passent la whitelist auto-générée de domaines gouvernementaux.
  - `IA · sans source officielle` — ambre pointillé, contenu extrait mais aucun domaine officiel whitelisté n'a pu être capté.
  - `erreur` — rouge, aucun port d'entrée n'a pu être extrait (typique des rochers disputés ou zones sans port).
- **Popup Port d'Entrée** : nom, ville, note, source de géocodage, et un badge de validation spatiale (dans la ZEE / hors ZEE avec distance).
- **Flag stale** : toute zone datant de plus de 180 jours affiche un badge horloge invitant au rafraîchissement. La ré-extraction n'a lieu que si le contenu source a changé (monitoring MD5).
- Bouton *Export GeoJSON* — exporte tous les Ports d'Entrée extraits.

### 4) AMP (vert)
- **Carte** : polygones ProtectedSeas Navigator, colorés par niveau de protection pêche (LFP 1–5). Zoomez sur une côte (zoom ≥ 5) ; la vue monde n'affiche que l'invite, pas 20 000 polygones.
- **Popup** : deux URL séparées. Gris = site du gestionnaire (champ ProtectedSeas `url` / Website). Vert = procédures de visite / d'entrée. L'URL de visite n'est jamais une copie de la page gestionnaire.
- **Bandeau gauche** : recherche par site, pays ou désignation ; filtre LFP ; liste des aires de la tuile courante.
- **Review** : onglet admin seulement (masqué en Science). Ce n’est jamais un calque carte.
- *Export GeoJSON* — centroïdes + `manager_url` / `visit_url`, pas les limites officielles.

## Audit Swarm Intelligence (bascule dans l'en-tête)
Console opérateur réservée à l'équipage / admin. Elle regroupe **tous les déclencheurs batch** au même endroit (le « Swarm Intelligence Hub ») :
- **Lancer** : chaque mode a la même tête Test | Complet | Lancer (+ Stop).
- **Projets — Swarm** : Test (quelques portails) ou Complet (toutes les graines). Run isolé ; la carte publique ne change pas tant que vous ne choisissez pas le run.
- **Marinas — Dump mondial** : Test = une tuile côtière. Complet = dump mondial puis enrichissement par priorité ; copie aussi sur la carte publique. Les mouillages sont mondiaux (plus de corridor 25 NM).
- **Capitaineries — Dump mondial** : tuiles Overpass `office=harbour_master` / `seamark:building:function=harbour_master`, puis overlay SHOM `buisgl_point` FUNCTN=2 (bureau de capitainerie) et SMCFAC CATSCF=6. Téléphone et VHF depuis les tags, puis le site officiel du bureau. Pas de rattachement marina.
- **Formalités — Référentiel ZEE** : construit/rafraîchit le référentiel mondial des ZEE (VLIZ Marine Regions). La génération batch des Ports d'Entrée (generate-batch) a été retirée.
- **AMP** : couverture du cache et des URL de visite. Un job Console remplit les URL de visite comme les fiches Google Marinas : liens extra ProtectedSeas, puis TinyFish Fetch sur la page gestionnaire, puis Search. Jamais la homepage gestionnaire.
- **KPIs, table de télémétrie, extractions échouées** pour le pipeline projets, avec Force Extract (TinyFish) par URL ou global.

## Paramètres (bandeau droit, icône engrenage)
- **Documentation** : télécharger ce manuel (EN/FR).
- **Données** : Importer GeoJSON (validation des coordonnées, comptage et fusion des doublons), Exporter GeoJSON, Effacer tous les projets.
- **Filtrage marin** : distance max à la côte (km), score marin minimum.
- **Extraction** : agents TinyFish en parallèle (1–2), concurrence des extractions (1–20), interrupteur Follow the Money, limite Auto-Stop.
- **Carte** : zoom minimum, marqueurs max.
- **Clés API** : TinyFish et OpenRouter (stockées côté serveur, jamais exposées).

## Pipeline (fonctionnement)
1. **Découverte** : les agents web TinyFish naviguent sur les portails des fondations (flux SSE en direct, repli polling, repli crawler HTTP).
2. **Extraction** : Readability nettoie la page → un LLM Gatekeeper rejette les projets terrestres/eau douce → un LLM extrait titre, description (<250 caractères), lieu, catégorie, partenaires et score S_ocean.
3. **Géocodage** : GPS extrait → Nominatim → géocodage intelligent par LLM → test Point-in-Ocean → recalage côtier si à l'intérieur des terres.
4. **Déduplication** : URL identique, proximité spatiale (<500 m) + similarité de titre (>90 %) → financeurs fusionnés.
5. **Pipeline Ports d'Entrée (mode Formalités)** : des moteurs de recherche (SearXNG, recherche web OpenRouter) trouvent les sources officielles douanes/immigration de chaque ZEE ; une whitelist de domaines gouvernementaux (auto-générée depuis les codes ISO + Public Suffix List + fichier d'exceptions) les filtre ; les pages/PDF sont parsés (trafilatura / PyMuPDF) ; un LLM léger extrait la liste des PoE officiels en JSON strict ; chaque port est géocodé (Nominatim/GeoNames) puis validé spatialement dans son polygone ZEE (shapely). Les hash MD5 des sources évitent toute ré-extraction inutile. Le pipeline n'invente jamais de contenu.
""",
}


@router.get("/manual")
async def manual(lang: str = "en"):
    text = MANUALS.get(lang, MANUALS["en"])
    # Phase 7bis — serve Markdown with a semantic content-type so browsers /
    # editors / IDEs render it correctly. The frontend Manual EN/FR buttons
    # still work (they window.open() the URL — no Accept header check).
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=blue_intelligence_manual_{lang}.md"},
    )

@router.get("/export/route.geojson")
async def export_route():
    from app.core.export_meta import export_response
    if not ROUTE_FILE.exists():
        raise HTTPException(404, "route.geojson not found")
    import json as _json
    data = _json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
    return export_response(
        data, "route", "route.geojson",
        license_note="Naviguide — Berry-Mappemonde (route officielle)",
    )

@router.get("/route")
async def get_route():
    """Serve the official Berry-Mappemonde expedition route (static, read-only)."""
    if not ROUTE_FILE.exists():
        raise HTTPException(404, "route.geojson not found")
    import json as _json
    try:
        data = _json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"invalid route geojson: {e}")
    return JSONResponse(
        data,
        headers={
            # Static official route: safe to cache 1h publicly + revalidate on redeploy
            "Cache-Control": "public, max-age=3600, must-revalidate",
            "X-Route-Source": "Naviguide Berry-Mappemonde (official)",
        },
    )

# ---------- ZEE Detection (Phase 8) ----------

ZEE_COMPUTE_STATE = TaskState(max_logs=600)


@router.get("/zee/crossings")
async def zee_get_crossings(french_only: bool = False):
    """
    EEZ crossings computed for the Berry-Mappemonde route (MongoDB cache).
    404 if no computation has been launched yet (POST /api/zee/compute).
    `french_only=true` keeps only French EEZs (non-null territory_code).
    """
    cached = await db.zee_crossings.find_one({"_id": "latest"})
    if not cached:
        raise HTTPException(404, "No ZEE crossings computed yet. Call POST /api/zee/compute first.")
    crossings: list[dict] = cached.get("crossings") or []
    if french_only:
        crossings = filter_french_territories(crossings)
    return {
        "computed_at": cached.get("computed_at"),
        "eez_source": cached.get("eez_source"),
        "detection_method": cached.get("detection_method"),
        "summary": zee_crossings_summary(crossings),
        "crossings": crossings,
    }


class ZeeComputeBody(BaseModel):
    force_download: bool = False
    use_point_api_fallback: bool = True


@router.post("/zee/compute")
async def zee_compute(body: ZeeComputeBody | None = None):
    """
    Launch EEZ-crossing computation as a background task:
      1. load EEZ polygons (local MarineRegions v12 file, else WFS),
      2. intersect maritime segments (shapely, thread),
      3. persist in MongoDB (zee_crossings, _id="latest").
    409 if a computation is already running; follow via GET /api/zee/compute/status.
    """
    if ZEE_COMPUTE_STATE.running:
        raise HTTPException(409, "A ZEE computation is already running")
    body = body or ZeeComputeBody()

    ZEE_COMPUTE_STATE.running = True
    ZEE_COMPUTE_STATE.started_at = time.time()
    ZEE_COMPUTE_STATE.finished_at = None
    ZEE_COMPUTE_STATE.error = None
    ZEE_COMPUTE_STATE.logs = []
    ZEE_COMPUTE_STATE.result = None

    async def _runner():
        try:
            crossings = await run_build_zee_crossings(
                route_path=ROUTE_FILE,
                eez_path=EEZ_FILE,
                force_download=body.force_download,
                use_point_api_fallback=body.use_point_api_fallback,
                logger=ZEE_COMPUTE_STATE.log,
            )
            summary = zee_crossings_summary(crossings)
            method = crossings[0]["detection_method"] if crossings else "none"
            eez_source = (
                "MarineRegions Maritime Boundaries v12 (local, CC-BY 4.0)"
                if EEZ_FILE.exists() else "MarineRegions REST API"
            )
            doc = {
                "_id": "latest",
                "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "eez_source": eez_source,
                "detection_method": method,
                "crossings": crossings,
                "summary": summary,
            }
            await db.zee_crossings.replace_one({"_id": "latest"}, doc, upsert=True)
            ZEE_COMPUTE_STATE.result = doc
            ZEE_COMPUTE_STATE.log(
                f"ZEE compute complete — {summary['total_crossings']} crossings, "
                f"{summary['unique_territories']} unique FR territories: {summary['territory_codes']}"
            )
        except Exception as exc:
            ZEE_COMPUTE_STATE.error = f"{type(exc).__name__}: {exc}"
            ZEE_COMPUTE_STATE.log(f"FATAL: {ZEE_COMPUTE_STATE.error}")
        finally:
            ZEE_COMPUTE_STATE.finished_at = time.time()
            ZEE_COMPUTE_STATE.running = False

    asyncio.create_task(_runner())
    return {
        "started": True,
        "force_download": body.force_download,
        "use_point_api_fallback": body.use_point_api_fallback,
    }


@router.get("/zee/compute/status")
async def zee_compute_status():
    s = ZEE_COMPUTE_STATE
    return {
        "running": s.running,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "error": s.error,
        "logs_tail": s.logs[-60:],
        "result_summary": s.result.get("summary") if s.result else None,
    }


@router.delete("/zee/crossings")
async def zee_clear_crossings(delete_eez_file: bool = False):
    """Clear the crossings cache (and optionally the local EEZ file)."""
    res = await db.zee_crossings.delete_one({"_id": "latest"})
    eez_deleted = False
    if delete_eez_file and EEZ_FILE.exists():
        try:
            EEZ_FILE.unlink()
            eez_deleted = True
        except Exception:
            pass
    return {"cache_deleted": res.deleted_count > 0, "eez_file_deleted": eez_deleted}
