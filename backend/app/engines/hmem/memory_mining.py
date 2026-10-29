# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
import json
import re

EVENT_TYPES = dict(chat_message='general', user_correction='preference', file_save='factual',
                   file_open='general', file_edit='factual', model_response='factual', plan_completion='factual')


def parse_strict_json_array(text):
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    try:
        values = json.loads(text[text.index('['):text.rindex(']') + 1])
    except (ValueError, TypeError):
        return []
    return [s for s in values if isinstance(s, str) and len(s.strip()) >= 5] if isinstance(values, list) else []


class MemoryMiningService:
    def __init__(self, store, clock, miner_fn=None):
        self.store, self.clock, self.miner_fn = store, clock, miner_fn

    def _candidates(self, event, text):
        if self.miner_fn:
            return parse_strict_json_array(self.miner_fn(
                f'Extract durable memories from this {event} as a strict JSON array of strings:\n\n{text}'))
        candidates = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', text)]
        return [s for s in candidates if 20 <= len(s) <= 300 and re.search(
            r'\b(prefer|always|never|decided|chose|choose|use|because|instead|must|should|important|remember)\b', s, re.I)][:10]

    def mine_interaction(self, event, text, opts=None):
        opts = opts or {}
        return [self.store.add(dict(content=content, type=EVENT_TYPES[event],
                    layer=opts.get('layer', 'working' if self.miner_fn else 'short_term'),
                    tags=[f'event:{event}'], source=f'interaction:{event}',
                    provenance=dict(origin=event, learnedAt=self.clock(), label=f'mined from {event}'),
                    **({'chatId': opts['chatId']} if 'chatId' in opts else {})))
                for content in self._candidates(event, text)]

    def mine_document(self, name, text):
        return [self.store.add(dict(content=c, type='factual', layer='working',
                    tags=['document', f'file:{name}'], source=f'document:{name}'))
                for c in self._candidates('file_save', text[:8000])]
