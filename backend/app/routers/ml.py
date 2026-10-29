"""
app.routers.ml — FastAPI endpoints for ML bootstrapping (weak supervision, anomalies, NER).
"""
import asyncio
import time

from fastapi import APIRouter, Body, HTTPException

from app.core import ml as ml_core
from app.core import rag as rag_core
from app.core.tasks import TaskState
from app.db import db as _db

router = APIRouter(prefix="/api/ml")

TRAIN_STATE = TaskState()
ANOM_STATE = TaskState()
NER_STATE = TaskState()
SERP_STATE = TaskState()


@router.get("/status")
async def ml_status():
    return {
        "dataset": await ml_core.dataset_stats(_db),
        "models": ml_core.models_status(),
        "embedding_backend": rag_core.embedding_backend(),
    }


# --- Classifieur de pertinence (Gatekeeper local) ---------------------------
@router.post("/train/gatekeeper", status_code=202)
async def train_gatekeeper():
    if TRAIN_STATE.running:
        raise HTTPException(409, "Training already running")
    TRAIN_STATE.reset()
    TRAIN_STATE.running = True
    TRAIN_STATE.started_at = time.time()

    async def _runner():
        try:
            TRAIN_STATE.summary = await ml_core.train_gatekeeper(_db, log=TRAIN_STATE.log)
        except Exception as e:
            TRAIN_STATE.error = f"{type(e).__name__}: {e}"
            TRAIN_STATE.log(f"FATAL: {TRAIN_STATE.error}")
        finally:
            TRAIN_STATE.finished_at = time.time()
            TRAIN_STATE.running = False

    asyncio.create_task(_runner())
    return {"status": "started"}


@router.get("/train/status")
async def train_status():
    return TRAIN_STATE.status()


@router.post("/gatekeeper/predict")
async def gatekeeper_predict(body: dict = Body(...)):
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "body must contain 'text'")
    pred = ml_core.predict_relevance(text)
    if pred is None:
        raise HTTPException(404, "no trained gatekeeper model — POST /api/ml/train/gatekeeper first")
    return pred


# --- Spatial PoE anomalies --------------------------------------------------
@router.post("/anomalies/scan", status_code=202)
async def anomalies_scan(body: dict | None = Body(default=None)):
    if ANOM_STATE.running:
        raise HTTPException(409, "Anomaly scan already running")
    contamination = float((body or {}).get("contamination", 0.05))
    ANOM_STATE.reset()
    ANOM_STATE.running = True
    ANOM_STATE.started_at = time.time()

    async def _runner():
        try:
            ANOM_STATE.summary = await ml_core.scan_poe_anomalies(_db, state=ANOM_STATE,
                                                                  contamination=contamination)
        except Exception as e:
            ANOM_STATE.error = f"{type(e).__name__}: {e}"
            ANOM_STATE.log(f"FATAL: {ANOM_STATE.error}")
        finally:
            ANOM_STATE.finished_at = time.time()
            ANOM_STATE.running = False

    asyncio.create_task(_runner())
    return {"status": "started"}


@router.get("/anomalies/status")
async def anomalies_status():
    return ANOM_STATE.status()


@router.get("/anomalies/report")
async def anomalies_report():
    if not ml_core.ANOMALY_REPORT_FILE.exists():
        raise HTTPException(404, "no anomaly report — POST /api/ml/anomalies/scan first")
    import json
    return json.loads(ml_core.ANOMALY_REPORT_FILE.read_text())


# --- NER dataset (spaCy prep) ------------------------------------------------
@router.post("/ner/export-dataset")
async def ner_export():
    return await ml_core.export_ner_dataset(_db)


# --- Local spaCy NER training + extraction -------------------------------------
@router.post("/train/ner", status_code=202)
async def train_ner(body: dict | None = Body(default=None)):
    if NER_STATE.running:
        raise HTTPException(409, "NER training already running")
    n_iter = max(1, min(30, int((body or {}).get("iterations", 12))))
    NER_STATE.reset()
    NER_STATE.running = True
    NER_STATE.started_at = time.time()

    async def _runner():
        try:
            NER_STATE.summary = await ml_core.train_ner(_db, log=NER_STATE.log, n_iter=n_iter)
        except Exception as e:
            NER_STATE.error = f"{type(e).__name__}: {e}"
            NER_STATE.log(f"FATAL: {NER_STATE.error}")
        finally:
            NER_STATE.finished_at = time.time()
            NER_STATE.running = False

    asyncio.create_task(_runner())
    return {"status": "started", "iterations": n_iter}


@router.get("/train/ner/status")
async def train_ner_status():
    st = NER_STATE.status()
    # Status survives reloads: model metrics persisted on disk
    if st.get("summary") is None and ml_core.NER_METRICS_FILE.exists():
        import json
        try:
            st["model_on_disk"] = json.loads(ml_core.NER_METRICS_FILE.read_text())
        except Exception:
            pass
    return st


@router.post("/ner/extract")
async def ner_extract(body: dict = Body(...)):
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "body must contain 'text'")
    if not ml_core.has_ner_model():
        raise HTTPException(404, "no trained NER model — POST /api/ml/train/ner first")
    entities = await asyncio.to_thread(ml_core.extract_entities, text)
    return {"entities": entities, "count": len(entities)}


# --- SERP classifier (rank links before download) -----------------------------
@router.post("/train/serp", status_code=202)
async def train_serp():
    if SERP_STATE.running:
        raise HTTPException(409, "SERP training already running")
    SERP_STATE.reset()
    SERP_STATE.running = True
    SERP_STATE.started_at = time.time()

    async def _runner():
        try:
            SERP_STATE.summary = await ml_core.train_serp(_db, log=SERP_STATE.log)
        except Exception as e:
            SERP_STATE.error = f"{type(e).__name__}: {e}"
            SERP_STATE.log(f"FATAL: {SERP_STATE.error}")
        finally:
            SERP_STATE.finished_at = time.time()
            SERP_STATE.running = False

    asyncio.create_task(_runner())
    return {"status": "started"}


@router.get("/train/serp/status")
async def train_serp_status():
    st = SERP_STATE.status()
    if st.get("summary") is None:
        model = ml_core.models_status().get("serp_classifier")
        if model:
            st["model_on_disk"] = model
    return st


@router.post("/serp/predict")
async def serp_predict(body: dict = Body(...)):
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "body must contain 'url'")
    pred = ml_core.predict_serp(url)
    if pred is None:
        raise HTTPException(404, "no trained SERP classifier — POST /api/ml/train/serp first")
    return {"url": url, **pred}
