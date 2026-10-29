# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
import re
from .embeddings import cosine
from .types import clamp01, DEFAULTS
from .memory_store import snapshot


class ConflictDetectionService:
    def __init__(self, store, ledger, clock, actor='system'):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor
        self.conflicts = []

    def register(self, a, b, reason, confidence):
        """Record a detected conflict, including structured application evidence."""
        conflict = dict(id=f'conflict-{len(self.conflicts) + 1}', memoryA=a['id'],
                        memoryB=b['id'], reason=reason, confidence=confidence, resolved=False)
        self.conflicts.append(conflict)
        self.ledger.log(a['id'], 'conflict_detected', self.actor,
                        details={'with': b['id'], 'reason': reason, 'confidence': confidence})
        return conflict

    def scan(self):
        found = []
        records = self.store.all()
        for i, a in enumerate(records):
            for b in records[i + 1:]:
                if (a.get('supersedes') == b['id'] or b.get('supersedes') == a['id']
                        or a.get('supersededBy') == b['id'] or b.get('supersededBy') == a['id']):
                    continue
                sim = cosine(a['embedding'], b['embedding'])
                reason, confidence = None, 0
                for left, right in [('always', 'never'), ('enable[ds]?', 'disable[ds]?'),
                                    ('prefer[s]?', 'avoid[s]?|dislike[s]?'), ('on', 'off'), ('yes', 'no')]:
                    def has(pattern, record):
                        return re.search(r'\b' + pattern + r'\b', record['content'], re.I)
                    if sim > .5 and ((has(left, a) and has(right, b)) or (has(right, a) and has(left, b))):
                        reason, confidence = 'opposing phrase pair on similar content', .5 + .3 * sim
                na, nb = (re.findall(r'\d+(?:\.\d+)?', r['content'], re.ASCII) for r in (a, b))
                if reason is None and sim > .8 and na and nb and na != nb:
                    reason, confidence = 'high-similarity conflicting numeric values', .55 + .25 * sim
                if reason:
                    confidence += .1 * bool(set(a['entities']) & set(b['entities']))
                    confidence += .05 * (a['domain'] == b['domain'])
                    confidence -= .1 * (b['id'] in a['relatedTo'])
                    found.append(self.register(a, b, reason, clamp01(confidence)))
        return found

    def _weight(self, memory):
        return (.3 * memory['lastAccessed'] / max(1, self.clock())
                + .2 * min(1, memory['accessCount'] / 10)
                + .25 * memory['confidence']
                + .1 * min(1, len(memory['content'].encode('utf-16-le')) / 2 / 200)
                + .15 * (1 if memory['provenance']['origin'] == 'user_correction' else .5))

    def auto_resolve(self, conflict):
        a, b = (self.store.must_get(conflict[key]) for key in ('memoryA', 'memoryB'))
        sa, sb = self._weight(a), self._weight(b)
        margin = abs(sa - sb) / max(sa, sb)
        decision = clamp01(conflict['confidence'] * .6 + margin * .8)
        if decision < DEFAULTS['conflictAutoResolveGate']:
            return 'queued'
        self.resolve(conflict, a['id'] if sa >= sb else b['id'], decisionConfidence=round(decision, 3))
        return 'resolved'

    def resolve(self, conflict, winner_id, **details):
        """Explicit resolution is separate from the reference's 0.70 auto gate."""
        if conflict['resolved'] or winner_id not in (conflict['memoryA'], conflict['memoryB']):
            raise ValueError('Conflict is resolved or winner is not a participant')
        loser_id = conflict['memoryB'] if winner_id == conflict['memoryA'] else conflict['memoryA']
        loser = self.store.must_get(loser_id)
        previous = snapshot(loser)
        loser['supersededBy'] = winner_id
        conflict.update(resolved=True, resolution='keep_a' if winner_id == conflict['memoryA'] else 'keep_b')
        self.ledger.log(loser_id, 'conflict_resolved', self.actor, previousState=previous,
                        newState=snapshot(loser), details=dict(conflictId=conflict['id'], winner=winner_id, **details))
        self.store.persist()

    def queue(self):
        return [c for c in self.conflicts if not c['resolved']]
