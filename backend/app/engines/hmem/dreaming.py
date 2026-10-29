# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
from .embeddings import cosine, jaccard
from .types import DEFAULTS


class DreamingService:
    def __init__(self, store, ledger, clock, actor='system'):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor
        self.proposals = {}

    @staticmethod
    def relatedness(a, b):
        return (.5 * max(0, cosine(a['embedding'], b['embedding'])) + .2 * jaccard(a['tags'], b['tags'])
                + .1 * (a['domain'] == b['domain']) + .2 * jaccard(a['entities'], b['entities']))

    def _clusters(self):
        used, clusters = set(), []
        for seed in self.store.all():
            if seed['id'] in used:
                continue
            group = [seed] + [m for m in self.store.all() if m['id'] != seed['id'] and m['id'] not in used
                            and self.relatedness(seed, m) >= DEFAULTS['dreamRelatedness']]
            if len(group) >= DEFAULTS['dreamClusterMinSize']:
                used.update(m['id'] for m in group)
                clusters.append(group)
        return clusters

    def dream(self):
        created = []

        def propose(kind, content, sources, **extra):
            proposal = dict(id=f'dream-{len(self.proposals) + 1}', status='proposed', type=kind,
                            content=content, derivedFrom=sources, **extra)
            self.proposals[proposal['id']] = proposal
            created.append(proposal)
            self.ledger.log(proposal['id'], 'dream_proposed', self.actor, details=dict(type=kind, derivedFrom=sources))

        for group in self._clusters()[:DEFAULTS['maxDreamProposalsPerType']]:
            domains = '/'.join(dict.fromkeys(m['domain'] for m in group))
            propose('insight', f'Across {len(group)} related memories ({domains}): ' + ' + '.join(m['content'] for m in group), [m['id'] for m in group])
        domains = 0
        for memory in self.store.all():
            if domains >= DEFAULTS['maxDreamProposalsPerType']:
                break
            suggested = self.store.classify_domain(memory['content'], memory['tags'], memory['type'])
            if memory['domain'] == 'general' and suggested != 'general':
                propose('domain', f'Reclassify "{memory["content"][:60]}" as {suggested}', [memory['id']], suggestedDomain=suggested)
                domains += 1
        by_domain = {}
        for memory in self.store.all():
            if memory['type'] in ('preference', 'personal') or 'event:user_correction' in memory['tags']:
                by_domain.setdefault(memory['domain'], []).append(memory)
        invariants = 0
        for domain, group in by_domain.items():
            if invariants >= DEFAULTS['maxDreamProposalsPerType']:
                break
            if len(group) >= 2:
                propose('invariant', f'Stable pattern in {domain}: ' + '; '.join(m['content'] for m in group), [m['id'] for m in group])
                invariants += 1
        return created

    def pending(self):
        return [p for p in self.proposals.values() if p['status'] == 'proposed']

    def _pending(self, proposal_id):
        proposal = self.proposals.get(proposal_id)
        if not proposal or proposal['status'] != 'proposed':
            raise ValueError(f'H-MEM: no pending proposal {proposal_id}')
        return proposal

    def approve(self, proposal_id):
        proposal = self._pending(proposal_id)
        proposal['status'] = 'approved'
        self.ledger.log(proposal_id, 'dream_approved', self.actor, details={'type': proposal['type']})
        if proposal['type'] == 'domain':
            for memory_id in proposal['derivedFrom']:
                if self.store.get(memory_id) and proposal.get('suggestedDomain'):
                    self.store.update(memory_id, {'domain': proposal['suggestedDomain']})
            return None
        return self.store.add(dict(content=proposal['content'],
            type='factual' if proposal['type'] == 'insight' else 'preference', layer='long_term',
            tags=[f"dream:{proposal['type']}"], confidence=.75, source='dreaming',
            provenance=dict(origin='dream', learnedAt=self.clock(), label=f"approved {proposal['type']} proposal"),
            derivedFrom=proposal['derivedFrom']))

    def reject(self, proposal_id):
        proposal = self._pending(proposal_id)
        proposal['status'] = 'rejected'
        self.ledger.log(proposal_id, 'dream_rejected', self.actor, details={'type': proposal['type']})
