"""Project discovery seeds.

The 21 curated listings give reliable listing URLs. `MASTER_SEEDS`
loads the v1 union (~861 funders) from `data/master_seeds.json` (CDC C5).
Without a file, fallback = the 21 curated ones. Complet queue = `queue=crawl` only
(official home or list page). The rest is `resolve` / `skip`.
"""

CURATED_SEEDS = [
    {"name": "The Ocean Foundation", "url": "https://oceanfdn.org/projects/", "country": "US", "category": "Conservation & Research", "listing_kind": "projects_index", "aliases": ["Ocean Foundation"]},
    {"name": "Oceana", "url": "https://oceana.org/campaigns/", "country": "US", "category": "Advocacy & Campaigns", "listing_kind": "projects_index"},
    {"name": "Blue Marine Foundation", "url": "https://www.bluemarinefoundation.com/projects/", "country": "UK", "category": "Conservation & Research", "listing_kind": "projects_index"},
    {"name": "Fondation de la Mer", "url": "https://www.fondationdelamer.org/nos-programmes/", "country": "FR", "category": "Conservation & Research", "listing_kind": "projects_index"},
    {"name": "Pure Ocean Foundation", "url": "https://www.pure-ocean.org/nos-projets/", "country": "FR", "category": "Applied Research", "listing_kind": "projects_index", "aliases": ["Pure Ocean"]},
    {"name": "Fondation CMA CGM", "url": "https://www.cmacgm-group.com/fr/fondation", "country": "FR", "category": "Shipping & Ocean Protection", "listing_kind": "home_only", "aliases": ["CMA CGM Group"]},
    {"name": "IFREMER", "url": "https://www.ifremer.fr/fr", "country": "FR", "category": "Marine Science", "listing_kind": "home_only", "aliases": [
        "IFREMER – France",
        "Institut français de recherche pour l'exploitation de la mer (IFREMER)",
        "Institut Français de recherche pour l'exploitation de la mer (IFREMER)",
    ]},
    {"name": "Institut Océanographique Paul Ricard", "url": "https://www.institut-paul-ricard.org/", "country": "FR", "category": "Marine Biology", "listing_kind": "home_only"},
    {"name": "SHOM", "url": "https://www.shom.fr/fr", "country": "FR", "category": "Hydrography", "listing_kind": "home_only"},
    {"name": "CORDIS Europe", "url": "https://cordis.europa.eu/projects/en", "country": "EU", "category": "EU Research", "listing_kind": "projects_index"},
    {"name": "Prince Albert II Foundation", "url": "https://www.fpa2.org/en/projects/", "country": "MC", "category": "Mediterranean & MPA", "listing_kind": "projects_index", "aliases": [
        "Prince Albert II of Monaco Foundation",
        "Fondation Prince Albert II de Monaco",
        "FPA2",
    ]},
    {"name": "Coral Reef Alliance", "url": "https://coral.org/en/where-we-work/", "country": "US", "category": "Coral Reefs", "listing_kind": "projects_index", "aliases": [
        "CORAL", "Coral Reef Alliance (CORAL)", "The Coral Reef Alliance (CORAL)",
    ]},
    {"name": "Mission Blue", "url": "https://missionblue.org/hope-spots/", "country": "US", "category": "Hope Spots", "listing_kind": "projects_index"},
    {"name": "Seacology", "url": "https://www.seacology.org/projects/", "country": "US", "category": "Island Conservation", "listing_kind": "projects_index"},
    {"name": "Ocean Conservancy", "url": "https://oceanconservancy.org/programs/", "country": "US", "category": "Conservation & Research", "listing_kind": "projects_index"},
    {"name": "Pew Charitable Trusts", "url": "https://www.pewtrusts.org/en/projects", "country": "US", "category": "Policy & Research", "listing_kind": "projects_index", "aliases": ["The Pew Charitable Trusts", "The Pew Charitable Trust"]},
    {"name": "WWF Oceans", "url": "https://www.worldwildlife.org/initiatives/oceans", "country": "INT", "category": "Conservation & Research", "listing_kind": "projects_index", "aliases": ["WWF"]},
    {"name": "Packard Foundation", "url": "https://www.packard.org/what-we-fund/ocean/", "country": "US", "category": "Philanthropy", "listing_kind": "projects_index", "aliases": [
        "The David and Lucile Packard Foundation", "David and Lucile Packard Foundation",
    ]},
    {"name": "Rare Fish Forever", "url": "https://rare.org/program/fish-forever/", "country": "US", "category": "Sustainable Fisheries", "listing_kind": "projects_index", "aliases": ["Rare"]},
    {"name": "Fauna & Flora Oceans", "url": "https://www.fauna-flora.org/environments/oceans/", "country": "UK", "category": "Conservation & Research", "listing_kind": "projects_index", "aliases": ["Fauna & Flora"]},
    {"name": "Wildlife Conservation Society Marine", "url": "https://www.wcs.org/our-work/oceans", "country": "US", "category": "Conservation & Research", "listing_kind": "projects_index", "aliases": ["WCS"]},
]

TEST_SEED_COUNT = 3

URL_PATTERNS = [
    "/project", "/campaign", "/initiative", "/hope-spot", "/where-we-work/",
    "/programs/", "/grants/", "/our-work/", "/program/", "/projets",
    "/projet", "/nos-actions", "/actions", "/missions", "/expeditions",
    "/nos-programmes", "/nos-projets", "/iw-projects", "/reef-plus",
    "/our-campaigns",
]

CRAWL_BLACKLIST = (
    "contact", "about", "a-propos", "privacy", "terms", "donate", "don", "blog",
    "news", "actualites", "team", "equipe", "login", "cart", "shop", "boutique",
    "event", "job", "recrutement", "press", "presse", "media", "faq",
    "mentions-legales", "cookies", "newsletter", "sitemap", "search", "tag",
    "qui-sommes-nous", "who-we-are", "notre-comite-scientifique",
    "nos-ambassadeurs",
)

# Late import: master_seeds.py imports CURATED_SEEDS from this module.
from app.services.master_seeds import load_master_seeds  # noqa: E402

MASTER_SEEDS = load_master_seeds()
