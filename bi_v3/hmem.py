"""Run memory inspired by Meterless H-MEM (hierarchical memory + append-only trust ledger).
Tiers: evidence (short-term, per run) -> entity facts (long-term, across runs).
Nothing is ever overwritten: each write appends to a hash-chained ledger."""
from __future__ import annotations
import hashlib, json, os, time

class HMem:
    def __init__(self, root: str):
        self.root = root; os.makedirs(root, exist_ok=True)
        self.ledger = os.path.join(root, "ledger.jsonl"); self.facts_path = os.path.join(root, "facts.json")
        self.facts = json.load(open(self.facts_path)) if os.path.exists(self.facts_path) else {}

    def _last_hash(self):
        if not os.path.exists(self.ledger):
            return "0" * 64
        last = None
        for line in open(self.ledger):
            last = line
        return json.loads(last)["hash"] if last else "0" * 64

    def append(self, kind: str, payload: dict):
        prev = self._last_hash()
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, "payload": payload, "prev": prev}
        rec["hash"] = hashlib.sha256((prev + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        with open(self.ledger, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec["hash"]

    def remember(self, run_id: str, entity: dict):
        key = f'{entity["layer"]}:{entity["territory"]}:{entity["key"]}'
        hist = self.facts.setdefault(key, {"name": entity["name"], "runs": []})
        hist["runs"].append({"run_id": run_id, "level": entity["level"], "lat": entity.get("lat"),
                             "lon": entity.get("lon"), "sources": entity.get("source_urls", [])})
        best = max(hist["runs"], key=lambda r: r["level"])
        hist["best"] = {"from_run": best["run_id"], "level": best["level"]}
        self.append("remember", {"key": key, "run_id": run_id, "level": entity["level"]})

    def what_did_we_know(self, run_id: str):
        return {k: next((r for r in v["runs"] if r["run_id"] == run_id), None) for k, v in self.facts.items()}

    def save(self):
        json.dump(self.facts, open(self.facts_path, "w"), ensure_ascii=False, indent=1)

    def verify(self) -> bool:
        prev = "0" * 64
        for line in open(self.ledger):
            r = json.loads(line)
            h = hashlib.sha256((prev + json.dumps(r["payload"], sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
            if r["prev"] != prev or r["hash"] != h:
                return False
            prev = h
        return True
