"""Offline verification memory nodes: recall -> evaluate/finalise -> remember.

HMEM_DIR selects persistent storage; TRACE_LOG_PATH and TRACE_STDOUT match the
EvaluatorAgent contract. Prior verdicts are evidence, not clearance authority.
"""
import hashlib
import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.engines.hmem import HMEM
from app.core.paths import get_repo_root, get_data_dir, get_logs_dir
try:
    import filelock
    HAS_FILELOCK = True
except ImportError:
    HAS_FILELOCK = False

REPO_ROOT = get_repo_root()
_LOCK = RLock()


def _acquire_lock(directory):
    """Cross-platform lock: filelock if available, else fcntl on Unix, else thread-only."""
    directory.mkdir(parents=True, exist_ok=True)
    # Use centralized data dir helper
    data_dir = get_data_dir() if directory.is_relative_to(REPO_ROOT) else directory
    lock_path = data_dir / '.lock'
    if HAS_FILELOCK:
        # filelock handles cross-platform advisory locking internally.
        return filelock.FileLock(str(lock_path))
    try:
        import fcntl  # available on Unix (macOS/Linux in CI)
        lock_fd = lock_path.open('a')
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd  # caller must release via .close()
    except ImportError:
        # No filelock, no fcntl: rely on RLock for thread safety only.
        # Document: NOT safe for true multi-process without OS locking.
        _LOCK.acquire()
        return _LOCK  # caller must release via .release()


@contextmanager
def _engine():
    # Serialize node read/modify/write cycles across threads and local processes.
    directory = Path(os.environ.get('HMEM_DIR') or get_data_dir() / 'hmem')
    directory.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(directory)
    try:
        if HAS_FILELOCK:
            with lock:
                yield HMEM(persist_dir=directory, actor='HMemAgent')
        else:
            # fcntl path: lock_fd is held; thread-only path: _LOCK is held.
            yield HMEM(persist_dir=directory, actor='HMemAgent')
    finally:
        # If filelock, context manager already released; otherwise release once.
        if not HAS_FILELOCK:
            if lock is _LOCK:
                _LOCK.release()
            else:
                lock.close()  # closing the fd releases the fcntl lock


def _state(state):
    out = dict(state)
    run_id = state.get('run_id')
    if run_id is None:
        run_id = str(uuid4())
    if not isinstance(run_id, str) or re.fullmatch(r'[A-Za-z0-9_-]+', run_id) is None:
        raise ValueError('run_id must contain only letters, digits, underscores or hyphens')
    out.update(run_id=run_id, ports=[dict(p) for p in state.get('ports', [])], trace=list(state.get('trace') or []))
    return out


def _port_tag(port):
    identity = json.dumps([str(port.get('name') or '').strip().casefold(),
                           str(port.get('zone_name') or '').strip().casefold()], ensure_ascii=False)
    return 'port:' + hashlib.sha256(identity.encode()).hexdigest()


def _port_label(port):
    """'Port <name>', unless the name already starts with the word Port ('Port of St. George's')."""
    name = str(port.get('name') or '').strip()
    return name if re.match(r'port\b', name, re.IGNORECASE) else f'Port {name}'


def _event(state, port, action, **details):
    return dict(timestamp=datetime.now(timezone.utc).isoformat(), agent_name='HMemAgent',
                run_id=state['run_id'], action=action,
                input_summary=dict(port_name=port.get('name'), zone_name=port.get('zone_name')),
                **details)


def _trace(state, entries):
    if entries:
        path = Path(os.environ.get('TRACE_LOG_PATH') or get_logs_dir() / f"trace-{state['run_id']}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in entries)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(payload)
        if os.environ.get('TRACE_STDOUT', '1') != '0':
            print(payload, end='', flush=True)
        state['trace'].extend(entries)
    return state


def recall_node(state):
    out, entries = _state(state), []
    with _engine() as engine:
        for port in out['ports']:
            tag = _port_tag(port)
            result = engine.query(f"{_port_label(port)} ({port.get('zone_name') or ''})", dict(
                topN=5, threshold=0, predicate=lambda m: tag in m['tags']))
            port['memory'] = [dict(id=m['memory']['id'], content=m['memory']['content'],
                score=m['relevance'], source=m['memory']['source'],
                provenance=deepcopy(m['memory']['provenance']),
                supersededBy=m['memory'].get('supersededBy')) for m in result['memories']]
            entries.append(_event(out, port, 'recall', memories=deepcopy(port['memory']),
                                  handoff={'from': 'HMemAgent', 'to': 'EvaluatorAgent'}))
    return _trace(out, entries)


def _official_source_of(memory):
    match = re.search(r'; official source (.*?); reasons ', memory.get('content') or '')
    return match.group(1) if match else None


def remember_node(state):
    out, entries = _state(state), []
    # Validate the whole batch before writing any memories.
    if any(p.get('verdict') not in ('pass', 'flag') for p in out['ports']):
        raise ValueError('remember_node requires finalised pass/flag verdicts')
    with _engine() as engine:
        for port in out['ports']:
            tag, verdict = _port_tag(port), port['verdict']
            prior = [m for m in engine.store.all() if tag in m['tags'] and not m.get('supersededBy')]
            previous = max(prior, key=lambda m: (m['timestamp'], int(m['id'][4:])), default=None)
            urls = [u for u in (port.get('source_urls') or []) if u]
            official = next((u for u in urls if any(s in u.lower() for s in ('gov', 'gouv', 'gob'))), 'none')
            zone = port.get('zone_name') or ''
            if (previous and f'verdict:{verdict}' in previous['tags']
                    and _official_source_of(previous) == official):
                # Unchanged verdict and official source: reinforce, do not duplicate.
                engine.feedback(previous['id'], 'helpful')
                entries.append(_event(out, port, 'remember', memory_id=previous['id'], outcome='reinforced',
                                      handoff={'from': 'EvaluatorAgent', 'to': 'HMemAgent'}))
                continue
            record = engine.add(dict(
                content=f"{_port_label(port)} ({zone}): verdict {verdict}; official source {official}; reasons {'; '.join(port.get('reasons_en') or [])}",
                type='factual', layer='long_term', source=urls[0] if urls else 'evaluator',
                provenance=dict(origin='model_response', learnedAt=engine.clock(),
                                label=f"evaluator run {out['run_id']}", refs=[out['run_id']]),
                tags=['project:blue-intelligence', f'zone:{zone}', f'verdict:{verdict}', tag],
                missionId=out['run_id']))
            action = 'created'
            # Resolve every active older contradictory verification, including repeats.
            for old in prior:
                if f'verdict:{verdict}' not in old['tags']:
                    conflict = engine.conflicts.register(old, record, 'same port and zone, different evaluator verdict', 1)
                    engine.conflicts.resolve(conflict, record['id'], policy='latest finalised evaluator run')
                    action = 'superseded'
            entries.append(_event(out, port, 'remember', memory_id=record['id'], outcome=action,
                                  handoff={'from': 'EvaluatorAgent', 'to': 'HMemAgent'}))
    return _trace(out, entries)
