"""Shared repository paths: single source of truth for the repo root.

Avoids fragile `parents[N]` chains. The constants and the get_*() functions
return the same paths. Importing this module has no side effects: it reads no
environment or configuration and writes nothing; callers create directories
when they need them.
"""
from pathlib import Path

# This file lives at backend/app/core/paths.py -> parents[3] = repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
# Repo-level runtime state such as H-MEM; not app.config.DATA_DIR (backend/data).
DATA_DIR = REPO_ROOT / "data"
LOGS_DIR = REPO_ROOT / "logs"
SAMPLE_DATA_DIR = BACKEND_DIR / "data" / "sample"


def get_repo_root() -> Path:
    """Return the repository root (directory containing backend/, frontend/, etc.)."""
    return REPO_ROOT


def get_backend_root() -> Path:
    """Return the backend/ directory."""
    return BACKEND_DIR


def get_data_dir() -> Path:
    """Return the repo-level data/ directory (callers create it if needed)."""
    return DATA_DIR


def get_logs_dir() -> Path:
    """Return the logs/ directory (callers create it if needed)."""
    return LOGS_DIR
