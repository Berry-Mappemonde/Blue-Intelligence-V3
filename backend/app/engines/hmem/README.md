Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0

Source: https://github.com/Meterless/Meterless/tree/1742dec56a66e58eef9dda2d2b865823758ca8cd/engines/hmem

The unmodified `backend/tests/fixtures/hmem_vectors.json` conformance vectors are
credited to Meterless, from the same commit. The Apache-2.0 license is included
in LICENSE. This directory contains a Python adaptation, not upstream TS files.

Use from backend/:

```python
from app.engines.hmem import HMEM
h = HMEM()  # HMEM_DIR or <repo>/data/hmem
h.add({"content": "Always retain verification sources", "source": "manual"})
results = h.query("verification sources")
```

Python service method names use snake_case; JSON records keep the reference's
camelCase keys. Clocks return milliseconds. `persist_dir=None` opts into a purely
in-memory engine. The optional `miner_fn(prompt)` is a synchronous callable that
returns JSON text; no provider or network is required.

The deterministic 64-dimensional FNV-1a embedder, hybrid score, defaults,
feedback, conflict gate, sleep plan and dream approval boundary follow the
runnable TypeScript reference. Dream proposals, conflict queues and sleep backups
are instance-local, as in the reference. Call `approve()` explicitly to materialise
a dream; neither Blue Intelligence node invokes dreaming or sleep automatically.

Requested persistence adaptation: working.jsonl and long_term.jsonl are atomically
replaced, while ledger.jsonl is append-only. Short-term records are not reloaded.
ID high-water marks survive restart/restore. Tier files and the ledger are not a
multi-file database transaction; backups remain in memory. The standalone engine
is single-writer; memory nodes serialize operations with a local filesystem lock:
`filelock` when installed (Windows, Linux, macOS), else `fcntl` (Linux, macOS), else
a thread-only lock that does not protect against other processes. This is not
distributed storage.

Blue Intelligence's `recall_node` filters by normalized port name and zone, then
ranks the top five with H-MEM. A zero threshold is intentional for that exact
identity scope, including low-scoring superseded history. `remember_node` records
each finalised result with canonical `model_response` provenance, an evaluator-run
label, refs and missionId. Same verdicts reward the most recent active record.
Changed verdict tags constitute structured conflict evidence; an explicit,
audited latest-finalised-run policy supersedes every older contradictory active
record without deleting it. This application resolution is separate from the
engine's unchanged 0.70 automatic-resolution gate. Prior verdicts do not change
clearance scores or authorize entry.

Graph wiring: `recall_node -> evaluate/retry loop -> finalise -> remember_node`.
Both nodes preserve run_id and append HMemAgent handoff events using the evaluator's
TRACE_LOG_PATH / TRACE_STDOUT contract. No existing graph files are changed.
