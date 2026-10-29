# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from .embeddings import tokenize, embed, cosine
from .types import MemoryRecord, clamp01

TECH_TERMS = 'react python typescript javascript rust sqlite postgres indexeddb node docker graphql redis webrtc tauri electron llm embedding'.split()
DOMAIN_KEYWORDS = {
    'tech': 'code bug api database deploy framework library server'.split() + TECH_TERMS,
    'work': 'meeting deadline client project sprint review report'.split(),
    'personal': 'family hobby prefer like dislike morning evening'.split(),
}


def snapshot(record):
    return deepcopy({k: v for k, v in record.items() if k != 'embedding'})


class MemoryStoreService:
    """Single-writer store; durable tiers are atomically rewritten JSONL files.

    Short-term records are not restored. IDs use the ledger high-water mark so
    an ephemeral or deleted record's ID is never reused after restarting.
    """
    def __init__(self, clock, ledger, actor='system', persist_dir=None):
        self.clock, self.ledger, self.actor = clock, ledger, actor
        self.directory = Path(persist_dir) if persist_dir is not None else None
        self.records = {}
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            for tier in ('working', 'long_term'):
                path = self.directory / f'{tier}.jsonl'
                if path.exists():
                    for line in path.read_text().splitlines():
                        if line.strip():
                            record = json.loads(line)
                            self.records[record['id']] = record
        ids = list(self.records) + [e['memoryId'] for e in ledger.all()]
        self.seq = max((int(i[4:]) for i in ids if re.fullmatch(r'mem-\d+', i)), default=0)

    def persist(self):
        if self.directory is None:
            return
        for tier in ('working', 'long_term'):
            fd, name = tempfile.mkstemp(dir=self.directory, prefix=f'.{tier}-')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    for record in sorted(self.all(), key=lambda r: r['id']):
                        if record['layer'] == tier:
                            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.directory / f'{tier}.jsonl')
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def add(self, data: dict) -> MemoryRecord:
        if not data.get('source') or not data['source'].strip():
            raise ValueError('H-MEM: a memory record requires a non-empty source')
        now = self.clock()
        self.seq += 1
        tags = list(data.get('tags', []))
        kind = data.get('type', 'general')
        domain = self.classify_domain(data['content'], tags, kind)
        bucket = 'profile' if kind in ('personal', 'preference') else 'facts' if kind == 'factual' else 'notes'
        record = dict(id=f'mem-{self.seq}', content=data['content'], type=kind,
                      layer=data.get('layer', 'working'), timestamp=now, lastAccessed=now,
                      accessCount=0, tags=tags, embedding=embed(data['content']),
                      confidence=clamp01(data.get('confidence', .7)), source=data['source'],
                      provenance=deepcopy(data.get('provenance', dict(origin='manual', learnedAt=now, label=data['source']))),
                      domain=domain, namespace=f'{domain}/{bucket}',
                      entities=self.extract_entities(data['content']), relatedTo=[])
        for key in ('chatId', 'missionId', 'goalRunId', 'supersedes', 'derivedFrom'):
            if key in data:
                record[key] = deepcopy(data[key])
        self._discover_relationships(record)
        self.records[record['id']] = record
        self.ledger.log(record['id'], 'create', self.actor, newState=snapshot(record))
        self.persist()
        return record

    def get(self, memory_id):
        return self.records.get(memory_id)

    def all(self):
        return list(self.records.values())

    def must_get(self, memory_id):
        if memory_id not in self.records:
            raise KeyError(f'H-MEM: unknown memory id {memory_id}')
        return self.records[memory_id]

    def update(self, memory_id, patch):
        record = self.must_get(memory_id)
        previous = snapshot(record)
        record.update(deepcopy(patch))
        self.ledger.log(memory_id, 'update', self.actor, previousState=previous, newState=snapshot(record))
        self.persist()
        return record

    def delete(self, memory_id, reason):
        record = self.must_get(memory_id)
        del self.records[memory_id]
        self.ledger.log(memory_id, 'delete', self.actor, previousState=snapshot(record), details={'reason': reason})
        self.persist()

    def touch(self, memory_id):
        record = self.must_get(memory_id)
        record['lastAccessed'] = self.clock()
        record['accessCount'] += 1

    def promote(self, memory_id, layer):
        record = self.must_get(memory_id)
        previous = snapshot(record)
        record['layer'] = layer
        self.ledger.log(memory_id, 'promote', self.actor, previousState=previous, newState=snapshot(record))
        self.persist()

    def snapshot_all(self):
        return deepcopy(dict(records=self.all(), seq=self.seq))

    def restore_all(self, records, seq):
        self.records = {r['id']: deepcopy(r) for r in records}
        self.seq = max(self.seq, seq)
        self.persist()

    @staticmethod
    def classify_domain(content, tags, kind):
        for tag in tags:
            if tag.startswith('project:'):
                return tag[len('project:'):]
            if tag.startswith('tech:'):
                return 'tech'
        tokens, best, best_score = set(tokenize(content)), 'general', 0
        for domain, keywords in DOMAIN_KEYWORDS.items():
            score = sum(k in tokens for k in keywords)
            if domain == 'personal' and kind in ('personal', 'preference'):
                score += 1
            if score > best_score:
                best, best_score = domain, score
        return best

    @staticmethod
    def extract_entities(content):
        entities = [t for t in tokenize(content) if t in TECH_TERMS]
        entities.extend(m.lower() for m in re.findall(r'\b([A-Z][a-zA-Z]{2,})\b', content, re.ASCII)
                        if m.lower() not in ('the', 'this', 'that', 'with'))
        return list(dict.fromkeys(entities))[:10]

    def _discover_relationships(self, record):
        for other in self.all():
            if len(record['relatedTo']) >= 5:
                break
            if (not record.get('supersedes') and cosine(record['embedding'], other['embedding']) > .85
                    and re.search(r'\b(actually|instead|no longer|not anymore|changed to|correction)\b', record['content'], re.I)):
                record['supersedes'] = other['id']
                continue
            if set(record['entities']) & set(other['entities']) and other['domain'] == record['domain']:
                record['relatedTo'].append(other['id'])
                if len(other['relatedTo']) < 5 and record['id'] not in other['relatedTo']:
                    other['relatedTo'].append(record['id'])
