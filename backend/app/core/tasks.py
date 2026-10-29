"""
app.core.tasks — Generic background-task state (unifies the 4 historic
classes: TaskState from poe_routes, EnrichBatchState and ZeeComputeState from
server.py, BuildState from marinas.py).
"""
import time

LOGS_TAIL_N = 200


class TaskState:
    """In-memory state of a background task/batch, with a bounded journal."""

    def __init__(self, max_logs: int = 800):
        self.max_logs = max_logs
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.progress = 0
        self.total = 0
        self.results: list[dict] = []
        self.logs: list[str] = []
        self.error = None
        self.summary = None
        self.result = None
        self.cancel = False
        self.run_id = None

    def log(self, msg: str):
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

    def reset(self):
        self.__init__(self.max_logs)

    def start(self):
        self.reset()
        self.running = True
        self.started_at = time.time()

    def finish(self):
        self.finished_at = time.time()
        self.running = False

    def status(self):
        return {
            "running": self.running, "started_at": self.started_at, "finished_at": self.finished_at,
            "progress": self.progress, "total": self.total, "results": self.results[-40:],
            "logs_tail": self.logs[-LOGS_TAIL_N:], "error": self.error, "summary": self.summary,
            "cancelling": self.cancel and self.running,
            "run_id": self.run_id,
        }


# Semantic aliases (historic signatures)
BuildState = TaskState


def prune_tasks(registry: dict, max_age_s: int = 3600):
    """Purge on-demand tasks (dict by id) finished for > max_age_s."""
    now = time.time()
    for k in list(registry.keys()):
        t = registry.get(k) or {}
        finished = t.get("finished_at") or 0
        if finished and (now - finished) > max_age_s:
            registry.pop(k, None)


def new_task() -> dict:
    """Descriptor of a one-shot on-demand task (marina/project enrich, PoE generation)."""
    return {"state": "running", "started_at": time.time(), "finished_at": None,
            "result": None, "error": None, "logs": []}
