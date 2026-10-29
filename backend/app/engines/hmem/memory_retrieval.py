# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
import math
from .embeddings import embed, tokenize, cosine, jaccard
from .types import DAY_MS, DEFAULTS, FEEDBACK_DELTAS, clamp01
from .memory_store import snapshot


class MemoryRetrievalService:
    def __init__(self, store, ledger, clock, actor='system'):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor

    def score_one(self, memory, query):
        tokens = tokenize(query)[:8]
        domain = self.store.classify_domain(query, [], 'general')
        age = (self.clock() - memory['lastAccessed']) / DAY_MS
        raw = (.35 * clamp01(cosine(embed(query), memory['embedding']))
               + .20 * jaccard(tokens, tokenize(memory['content']))
               + .10 * jaccard(tokens, [t.lower() for t in memory['tags']])
               + .10 * (domain == memory['domain'])
               + .15 * jaccard(self.store.extract_entities(query), memory['entities'])
               + .05 * {'long_term': 1, 'working': .8, 'short_term': .6}[memory['layer']]
               + .05 * math.exp(-age / DEFAULTS['recencyDays'])
               - .20 * bool(memory.get('supersededBy')))
        return clamp01(raw) * memory['confidence']

    def query(self, query, opts=None):
        opts = opts or {}
        matches = []
        for memory in self.store.all():
            if opts.get('chatId') and memory.get('chatId') and opts['chatId'] != memory['chatId']:
                continue
            # Optional application filter; hybrid ranking itself is unchanged.
            if opts.get('predicate') and not opts['predicate'](memory):
                continue
            score = self.score_one(memory, query)
            if score >= opts.get('threshold', DEFAULTS['retrievalThreshold']):
                matches.append(dict(memory=memory, relevance=score))
        matches.sort(key=lambda m: (-m['relevance'], m['memory']['id']))
        matches = matches[:opts.get('topN', DEFAULTS['retrievalTopN'])]
        groups = {}
        for match in matches:
            memory = match['memory']
            self.store.touch(memory['id'])
            self.ledger.log(memory['id'], 'read', self.actor, details={'query': query})
            groups.setdefault(memory['domain'], []).append(memory)
        if matches:
            self.store.persist()
        lines = ['[MEMORY CONTEXT]']
        for domain in sorted(groups):
            lines.append(f'## {domain}')
            for memory in groups[domain]:
                layer = {'long_term': 'LT', 'working': 'WK', 'short_term': 'ST'}[memory['layer']]
                entities = f" (entities: {', '.join(memory['entities'])})" if memory['entities'] else ''
                lines.append(f"- [{layer}] {memory['content']}{entities} <{memory['id']}>")
        lines.append('[/MEMORY CONTEXT]')
        strategy = 'minimal' if len(matches) <= 1 else 'personal' if any(m['memory']['type'] in ('personal', 'preference') for m in matches) else 'comprehensive'
        domain = self.store.classify_domain(query, [], 'general')
        return dict(memories=matches, context='\n'.join(lines) if matches else '', trace=dict(
            retrievalReason=f'hybrid ranking for "{query}" (domain: {domain})', strategy=strategy,
            scores={m['memory']['id']: round(m['relevance'], 4) for m in matches}))

    def feedback(self, memory_id, kind):
        delta = FEEDBACK_DELTAS[kind]
        memory = self.store.must_get(memory_id)
        previous = snapshot(memory)
        memory['confidence'] = clamp01(memory['confidence'] + delta)
        if kind == 'helpful':
            memory['accessCount'] += 1
        if kind == 'wrong' and 'review' not in memory['tags']:
            memory['tags'].append('review')
        self.ledger.log(memory_id, 'feedback', self.actor, previousState=previous,
                        newState=snapshot(memory), details=dict(kind=kind, delta=delta))
        self.store.persist()
