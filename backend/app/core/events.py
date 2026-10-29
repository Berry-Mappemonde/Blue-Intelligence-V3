"""
events_core — Structured event journal of the PoE pipeline.

Each micro-step of the pipeline emits a persistent event
{run_id, seq, ts, mrgid, zone, step, payload} that records DECISIONS and
REJECTED CANDIDATES (queries issued, URLs dropped and why, gatekeeper
verdicts, cascade levels, LLM/NER outputs, geocoding candidates from
both providers, arbitrations…).

Dual output:
  - Mongo collection `poe_run_events` (queryable — feeds the run report);
  - JSONL file `backend/data/runs/<run_id>.jsonl` (direct read, artifact).

These traces serve audit, inter-run diff, the quantitative report and
future ML training sets (weak supervision).
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

RUNS_DIR = DATA_DIR / "runs"
HEARTBEAT_S = 60.0

_MAX_STR = 4000          # truncation of long strings (LLM outputs…)
_MAX_LIST = 60           # truncation of long lists (SERP…)


def _clip(value):
    """Recursively clip the payload (events stay bounded)."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…[tronqué]"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        clipped = [_clip(v) for v in list(value)[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            clipped.append(f"…[{len(value) - _MAX_LIST} éléments tronqués]")
        return clipped
    return value


class RunRecorder:
    """Run journal: write each event to Mongo + JSONL."""

    def __init__(self, run_id: str, db=None, to_file: bool = True,
                 events_coll: str = "poe_run_events"):
        self.run_id = run_id
        self.db = db
        self.events_coll = events_coll
        self._seq = 0
        self._seq_lock = asyncio.Lock()
        self.path = None
        if to_file:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            self.path = RUNS_DIR / f"{run_id}.jsonl"

    async def event(self, step: str, mrgid: int | None = None,
                    zone: str | None = None, **payload):
        async with self._seq_lock:
            self._seq += 1
            seq = self._seq
        doc = {
            "_id": str(uuid.uuid4()),
            "run_id": self.run_id,
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "t": round(time.time(), 3),
            "mrgid": mrgid,
            "zone": zone,
            "step": step,
            "payload": _clip(payload),
        }
        if self.db is not None:
            try:
                await getattr(self.db, self.events_coll).insert_one(doc)
            except Exception as exc:
                logger.warning(
                    "RunRecorder Mongo insert failed run_id=%s step=%s: %s",
                    self.run_id, step, exc,
                )
        if self.path is not None:
            try:
                line = json.dumps({k: v for k, v in doc.items() if k != "_id"},
                                  ensure_ascii=False, default=str)
                await asyncio.to_thread(self._append_line, line)
            except Exception as exc:
                logger.warning(
                    "RunRecorder JSONL append failed run_id=%s step=%s path=%s: %s",
                    self.run_id, step, self.path, exc,
                )

    def _append_line(self, line: str):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def resume_seq(self):
        """Resume the Mongo seq so a `run_done` is not re-emitted at seq=1."""
        if self.db is None:
            return
        try:
            cur = getattr(self.db, self.events_coll).find({"run_id": self.run_id})
            if hasattr(cur, "sort"):
                cur = cur.sort("seq", -1)
            docs = await cur.to_list(1)
            if docs:
                self._seq = max(self._seq, int(docs[0].get("seq") or 0))
        except Exception as exc:
            logger.warning(
                "RunRecorder resume_seq failed run_id=%s: %s", self.run_id, exc,
            )


class ZoneRecorder:
    """Recorder bound to a zone: mrgid/zone filled automatically."""

    def __init__(self, recorder: RunRecorder, mrgid: int, zone: str | None):
        self._rec = recorder
        self.mrgid = int(mrgid)
        self.zone = zone

    async def event(self, step: str, **payload):
        await self._rec.event(step, mrgid=self.mrgid, zone=self.zone, **payload)


async def emit(rec, step: str, **payload):
    """Tolerant emit: no-op if no recorder is attached."""
    if rec is not None:
        await rec.event(step, **payload)


class HeartbeatWatch:
    """Emit `heartbeat` every ~60 s without progress (slow vs hung)."""

    def __init__(self, recorder, interval_s: float = HEARTBEAT_S):
        self.recorder = recorder
        self.interval_s = float(interval_s)
        self._progress = None
        self._last_item = None
        self._extra: dict = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sig = None

    def update(self, progress=None, last_item=None, **extra):
        if progress is not None:
            self._progress = progress
        if last_item is not None:
            self._last_item = last_item
        if extra:
            self._extra.update(extra)

    def snapshot(self) -> dict:
        out = {
            "progress": self._progress,
            "last_item": self._last_item,
            **self._extra,
        }
        return out

    def start(self, get_snapshot=None):
        if self.recorder is None or self.interval_s <= 0:
            return self
        self._sig = (self._progress, self._last_item)
        self._task = asyncio.create_task(self._loop(get_snapshot))
        return self

    async def _loop(self, get_snapshot):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            snap = dict(self._extra)
            if get_snapshot:
                try:
                    snap.update(get_snapshot() or {})
                except Exception:
                    pass
            snap.setdefault("progress", self._progress)
            snap.setdefault("last_item", self._last_item)
            sig = (snap.get("progress"), snap.get("last_item"))
            if sig == self._sig:
                await emit(self.recorder, "heartbeat", **snap)
            self._sig = sig
            self._progress = snap.get("progress", self._progress)
            self._last_item = snap.get("last_item", self._last_item)

    async def aclose(self):
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@dataclass
class RunContext:
    """Versioned-run context: identifier, journal and target collections.
    When a RunContext is passed to the pipeline, writes go in the run
    space (poe_run_ports / poe_run_zones) — v1 collections (poe_ports,
    eez_zones) are NEVER touched (PRD "treasure" constraint)."""
    run_id: str
    recorder: RunRecorder
    ports_coll: str = "poe_run_ports"
    zones_coll: str = "poe_run_zones"
    started_at: float = field(default_factory=time.time)
    variant: str = "tinyfish"
