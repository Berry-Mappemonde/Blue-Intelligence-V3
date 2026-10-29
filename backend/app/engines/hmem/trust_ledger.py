# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from .types import ACTIONS


class TrustLedgerService:
    def __init__(self, clock, persist_dir=None):
        self.clock = clock
        self.path = Path(persist_dir) / 'ledger.jsonl' if persist_dir is not None else None
        self.entries = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self.entries = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def log(self, memory_id, action, actor, **extra):
        if action not in ACTIONS:
            raise ValueError(f'Unknown H-MEM ledger action: {action}')
        entry = deepcopy(dict(memoryId=memory_id, action=action, actor=actor,
                              timestamp=self.clock(), **extra))
        if self.path:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        self.entries.append(entry)
        return deepcopy(entry)

    def all(self):
        return deepcopy(self.entries)

    def history(self, memory_id):
        return [e for e in self.all() if e['memoryId'] == memory_id]

    def stats(self):
        return dict(Counter(e['action'] for e in self.entries))
