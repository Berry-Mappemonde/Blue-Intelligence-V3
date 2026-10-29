# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
"""Deterministic H-MEM facade. HMEM_DIR defaults to <repo>/data/hmem.

Pass persist_dir=None for an ephemeral engine. Clocks return milliseconds.
A persistent directory supports one writer at a time (like the TS reference).
"""
import os
import time
from pathlib import Path
from app.core.paths import get_repo_root
from .trust_ledger import TrustLedgerService
from .memory_store import MemoryStoreService
from .memory_mining import MemoryMiningService
from .memory_retrieval import MemoryRetrievalService
from .conflict_detection import ConflictDetectionService
from .sleep_cycle import SleepCycleService
from .dreaming import DreamingService

_DEFAULT = object()


class HMEM:
    def __init__(self, *, clock=None, actor='system', persist_dir=_DEFAULT, miner_fn=None):
        self.clock = clock if clock is not None else lambda: int(time.time() * 1000)
        if persist_dir is _DEFAULT:
            persist_dir = os.environ.get('HMEM_DIR') or get_repo_root() / 'data' / 'hmem'
        self.ledger = TrustLedgerService(self.clock, persist_dir)
        self.store = MemoryStoreService(self.clock, self.ledger, actor, persist_dir)
        self.mining = MemoryMiningService(self.store, self.clock, miner_fn)
        self.retrieval = MemoryRetrievalService(self.store, self.ledger, self.clock, actor)
        self.conflicts = ConflictDetectionService(self.store, self.ledger, self.clock, actor)
        self.sleep = SleepCycleService(self.store, self.ledger, self.clock, actor)
        self.dreaming = DreamingService(self.store, self.ledger, self.clock, actor)

    def add(self, data):
        return self.store.add(data)

    def mine(self, event, text, opts=None):
        return self.mining.mine_interaction(event, text, opts)

    def query(self, query, opts=None):
        return self.retrieval.query(query, opts)

    def feedback(self, memory_id, kind):
        return self.retrieval.feedback(memory_id, kind)
