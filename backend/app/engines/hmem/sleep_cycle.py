# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
from .embeddings import cosine
from .types import DAY_MS, DEFAULTS


class SleepCycleService:
    def __init__(self, store, ledger, clock, actor='system'):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor
        self.backups = {}

    def preview(self):
        records, now = self.store.all(), self.clock()
        referenced = set()
        for m in records:
            referenced.update(m['relatedTo'])
            referenced.update(m.get('derivedFrom', []))
            if m.get('supersedes'):
                referenced.add(m['supersedes'])
        consolidate = [m['id'] for m in records if m['layer'] == 'short_term'
                       and now - m['lastAccessed'] >= DEFAULTS['consolidationDays'] * DAY_MS]
        archive = [m['id'] for m in records
                   if now - m['timestamp'] >= DEFAULTS['archiveMinDays'] * DAY_MS
                   and m['accessCount'] <= DEFAULTS['archiveMaxAccess'] and m['id'] not in referenced
                   and not m.get('supersedes') and not m.get('derivedFrom') and len(m['relatedTo']) < 3]
        groups, used = [], set()
        for i, seed in enumerate(records):
            if seed['id'] in used:
                continue
            group = [seed['id']] + [m['id'] for m in records[i + 1:] if m['id'] not in used
                        and cosine(seed['embedding'], m['embedding']) >= DEFAULTS['synthesisCosine']]
            if len(group) >= 2:
                groups.append(group)
                used.update(group)
        return dict(toConsolidate=consolidate, toArchive=archive, toSynthesize=groups)

    def execute(self, preview=None):
        plan = self.preview() if preview is None else preview
        backup_id = f'backup-{len(self.backups) + 1}'
        self.backups[backup_id] = self.store.snapshot_all()
        actions = [f'backup {backup_id} created']
        for memory_id in plan['toConsolidate']:
            if self.store.get(memory_id):
                self.store.promote(memory_id, 'long_term')
                self.ledger.log(memory_id, 'sleep_consolidate', self.actor, details={'from': 'short_term', 'to': 'long_term'})
                actions.append(f'consolidated {memory_id}')
        for memory_id in plan['toArchive']:
            memory = self.store.get(memory_id)
            if memory:
                self.ledger.log(memory_id, 'sleep_archive', self.actor,
                                details=dict(age=self.clock() - memory['timestamp'], accessCount=memory['accessCount']))
                self.store.delete(memory_id, 'sleep archive')
                actions.append(f'archived {memory_id}')
        synthesized = 0
        for group in plan['toSynthesize']:
            members = [self.store.get(i) for i in group if self.store.get(i)]
            if len(members) < 2:
                continue
            merged = self.store.add(dict(content='Synthesis: ' + ' | '.join(m['content'] for m in members),
                type='factual', layer='long_term', tags=list(dict.fromkeys(t for m in members for t in m['tags'])),
                confidence=max(m['confidence'] for m in members), source='sleep:synthesis',
                provenance=dict(origin='sleep_synthesis', learnedAt=self.clock(), label=f'synthesized from {len(members)} memories'),
                derivedFrom=[m['id'] for m in members]))
            self.ledger.log(merged['id'], 'sleep_synthesize', self.actor, details={'sources': group})
            for memory in members:
                self.store.delete(memory['id'], 'merged by sleep synthesis')
            synthesized += 1
            actions.append(f"synthesized {merged['id']} from {', '.join(group)}")
        self.store.persist()
        return dict(backupId=backup_id, consolidated=len(plan['toConsolidate']),
                    archived=len(plan['toArchive']), synthesized=synthesized, actionLog=actions)

    def restore(self, backup_id):
        if backup_id not in self.backups:
            raise KeyError(f'H-MEM: unknown backup {backup_id}')
        self.store.restore_all(**self.backups[backup_id])
        self.ledger.log('*', 'restore', self.actor, details={'backupId': backup_id})
