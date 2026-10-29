"""
app.main — Blue Intelligence FastAPI application.

Assembles domain routers, middlewares (gzip, CORS) and startup
events (Mongo indexes, automatic PoE refresh).
Entry point: `uvicorn server:app` (shim) or `uvicorn app.main:app`.

When ``frontend/build/`` exists (or ``SERVE_FRONTEND=1``), also serves the
React bundle on ``/`` for a unified preview (API + UI on port 8001).
"""
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.db import client
from app.routers import amp, capitaineries, climatology, exports, formalities, health, marinas, misc, ml, project_runs, projects, review, runs, science, swarm

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_BUILD = _REPO_ROOT / "frontend" / "build"


def _serve_frontend_enabled() -> bool:
    flag = os.environ.get("SERVE_FRONTEND", "auto").lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return _FRONTEND_BUILD.is_dir()


_SERVE_FRONTEND = _serve_frontend_enabled()

app = FastAPI(
    title="Blue Intelligence API",
    docs_url="/api/docs" if _SERVE_FRONTEND else "/docs",
    redoc_url="/api/redoc" if _SERVE_FRONTEND else "/redoc",
    openapi_url="/api/openapi.json" if _SERVE_FRONTEND else "/openapi.json",
)

# ---------------------------------------------------------------------------
# Admin gate — when ADMIN_KEY is set (production), all writes
# /api/* and sensitive reads (review, admin) require the
# X-Admin-Key header. Without ADMIN_KEY (dev / tests), everything stays open.
# ---------------------------------------------------------------------------
_PUBLIC_WRITE_PATHS = {"/api/report-project"}   # public project reports
_PUBLIC_PATHS = {"/api/health"}
_ADMIN_GET_PREFIXES = ("/api/review", "/api/admin", "/api/reports")


@app.middleware("http")
async def _admin_gate(request, call_next):
    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    if admin_key:
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        needs_key = False
        if path.startswith("/api/"):
            if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                    and path not in _PUBLIC_WRITE_PATHS):
                needs_key = True
            elif path.startswith(_ADMIN_GET_PREFIXES):
                needs_key = True
        if needs_key:
            provided = request.headers.get("x-admin-key", "")
            if not secrets.compare_digest(provided, admin_key):
                return JSONResponse({"detail": "Admin key required"}, status_code=401)
    return await call_next(request)


for module in (health, project_runs, projects, swarm, marinas, capitaineries, formalities, amp, science, climatology, runs, review, ml, misc, exports):
    app.include_router(module.router)


if not _SERVE_FRONTEND:
    # Production ingress: only /api/* routes are exposed.
    @app.get("/api/openapi.json")
    async def openapi_under_api():
        return JSONResponse(app.openapi())


app.add_middleware(GZipMiddleware, minimum_size=1500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    """Index eez_zones / poe_ports collections, drop inherited
    collections, then start Formalities background tasks."""
    from app.db import db
    try:
        await db.eez_zones.create_index("mrgid", unique=True)
        await db.poe_ports.create_index("dedup_key", unique=True)
        await db.poe_ports.create_index("mrgid")
        await db.poe_evidence.create_index([("mrgid", 1), ("port_key", 1), ("url", 1)],
                                           unique=True, name="mrgid_port_key_url")
        await db.poe_listing_review.create_index("run_id")
        await db.poe_listing_review.create_index([("run_id", 1), ("reason", 1)])
        await db.poe_seed_ports.create_index("dedup_key", unique=True, sparse=True)
        await db.poe_seed_ports.create_index("mrgid")
        await db.poe_seed_ports.create_index("verify_verdict")
        await db.capitaineries.create_index("osm_id", unique=True, sparse=True)
        await db.capitaineries.create_index("shom_id", unique=True, sparse=True)
        await db.capitaineries.create_index("name")
        await db.capitaineries.create_index("source")
    except Exception as e:
        print(f"[startup] poe index creation failed (non-fatal): {e}")
    try:
        from app.core.geo import ensure_geo_indexes
        await ensure_geo_indexes(db)
    except Exception as e:
        print(f"[startup] geocode cache index creation failed (non-fatal): {e}")
    try:
        from app.services.project_runs import ensure_run_indexes
        await ensure_run_indexes(db)
        from app.services.isolated_runs import ensure_indexes as ensure_isolated_indexes
        await ensure_isolated_indexes(db)
    except Exception as e:
        print(f"[startup] project run index creation failed (non-fatal): {e}")
    try:
        from app.services.review_queue import ensure_review_indexes
        await ensure_review_indexes(db)
    except Exception as e:
        print(f"[startup] review comment index creation failed (non-fatal): {e}")
    try:
        existing = await db.list_collection_names()
        for legacy in ("formalities", "mpa_cache"):
            if legacy in existing:
                await db.drop_collection(legacy)
                print(f"[startup] dropped legacy {legacy} collection")
    except Exception as e:
        print(f"[startup] legacy collection drop failed (non-fatal): {e}")
    try:
        from app.services.amp import ensure_amp_indexes
        await ensure_amp_indexes(db)
    except Exception as e:
        print(f"[startup] amp index creation failed (non-fatal): {e}")
    try:
        amp.start_wide_amp_warmup()
    except Exception as e:
        print(f"[startup] amp wide-view warmup failed (non-fatal): {e}")
    try:
        from app.services.science_build import ensure_indexes as ensure_science_indexes
        await ensure_science_indexes(db.science_items)
        await db.science_runs.create_index("created_at")
        await db.depth_samples.create_index("fetched_at")
    except Exception as e:
        print(f"[startup] science index creation failed (non-fatal): {e}")
    # Automatic refresh: stale zones re-checked (MD5 monitoring)
    # and errors retried, without manual action.
    formalities.start_auto_refresh()
    # Automatic resume of interrupted background jobs (OSM validation)
    formalities.schedule_job_resume()
    # Warm up the marinas GeoJSON dump (several minutes on remote Mongo).
    try:
        marinas.start_marinas_fc_warmup()
    except Exception as e:
        print(f"[startup] marinas cache warmup failed (non-fatal): {e}")


@app.on_event("shutdown")
async def _shutdown():
    from app.core.render import shutdown_render
    await shutdown_render()
    client.close()


if _SERVE_FRONTEND:
    _RESERVED_ROOT = {"api", "docs", "redoc", "openapi.json"}

    def _spa_index_response() -> FileResponse:
        # no-cache: the browser revalidates index.html on every deploy,
        # otherwise it may keep an old bundle that references missing chunks.
        return FileResponse(
            _FRONTEND_BUILD / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    def _spa_file_or_index(full_path: str) -> FileResponse:
        head = full_path.split("/", 1)[0] if full_path else ""
        if head in _RESERVED_ROOT:
            raise HTTPException(404, detail="Not Found")
        try:
            candidate = (_FRONTEND_BUILD / full_path).resolve()
        except (OSError, ValueError):
            raise HTTPException(404, detail="Not Found")
        if not candidate.is_relative_to(_FRONTEND_BUILD):
            raise HTTPException(404, detail="Not Found")
        if candidate.is_file():
            return FileResponse(candidate)
        if head == "static":
            # Missing fingerprinted asset = stale client bundle: a frank 404
            # is better than index.html served in place of a JS file.
            raise HTTPException(404, detail="Not Found")
        return _spa_index_response()

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa_index():
        return _spa_index_response()

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa_assets(full_path: str):
        return _spa_file_or_index(full_path)
