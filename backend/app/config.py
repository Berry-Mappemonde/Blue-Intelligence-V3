"""
app.config — Paths, environment variables and default settings.

Loaded first by every module: the backend .env is read here,
before any os.environ access (Mongo, API keys…).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")

DATA_DIR = BACKEND_DIR / "data"
MODELS_DIR = BACKEND_DIR / "models"
ROUTE_FILE = DATA_DIR / "route.geojson"

MONGO_URL = os.environ.get("MONGO_URL") or os.environ.get("MONGO_URL_FALLBACK", "")
DB_NAME = os.environ.get("DB_NAME") or os.environ.get("DB_NAME_FALLBACK", "app")

# Completions: NVIDIA NIM if NVIDIA_API_KEY (per-usage chains, see nvidia.CHAINS).
# Nemotron uses Nebius Token Factory (EU) when NEBIUS_API_KEY is set or
# LLM_PROVIDER=tokenfactory. Absent key keeps NVIDIA NIM. See tokenfactory.py.
# OpenRouter stays for web search (:online) and fallback.
# load_dotenv does not overwrite a MONGO_URL already in the process.
DEFAULT_SETTINGS = {
    "_id": "global",
    "nvidia_api_key": "",
    "llm_provider": "auto",
    "openrouter_api_key": "",
    "tinyfish_api_key": "",
    "serper_api_key": "",
    "tinyfish_agents": 2,
    "extract_concurrency": 2,
    "nvidia_max_concurrency": 2,
    "max_coast_km": 50,
    "max_inland_km": 15.0,
    "min_marine_score": 0.5,
    "gatekeeper_accept": 0.85,
    "gatekeeper_reject": 0.12,
    "allow_tinyfish_agent": True,
    "test_max_urls_per_seed": 6,
    "full_max_urls_per_seed": 20,
    "min_zoom": 2,
    "max_markers": 1000,
    "follow_the_money": True,
    "max_partner_orgs": 15,
    "ftm_min_s_ocean": 0.7,
    "discover_concurrency": 8,
    "listing_judge_concurrency": 1,
    "saturation_limit": 50,
    "rescan_after_days": 7,
    "marina_search_radius_nm": 10.0,
    "marina_batch_concurrency": 2,
    "openrouter_min_credits_usd": 0.5,
    "enrich_stale_days": 365,
    "anthropic_api_key": "",
    "claude_budget_usd": 0.0,
}
