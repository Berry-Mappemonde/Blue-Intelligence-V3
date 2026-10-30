#!/usr/bin/env python3
"""Blue Intelligence CLI - Port verification pipeline runner (JSON in, JSON out).

  python backend/run.py --zone "Bermuda" [--run-id RUN123] [--sample-mode] [--timeout 300] [--trace PATH]
  python backend/run.py --input input_examples/01_bermuda.json [--output out.json]
  python backend/run.py --serve        # POST /run, demo page and GET /healthz on PORT (default 8000)
  python backend/run.py --resume RUN_ID [--retry-errored]   # resume a PoE run (unified runner, P0-4)
  python backend/run.py --export RUN_ID                     # export a finished run (data/exports/RUN_ID)

It runs a single cycle of the PoE verification pipeline. For the full Blue
Intelligence API use uvicorn app.main:app on port 8001.

Works from a fresh clone with no credentials and no MongoDB: the repo-root .env
and backend/.env are loaded if present (never overriding the real environment),
Mongo defaults satisfy app.config at import (the pipeline never connects), and
SAMPLE_MODE is switched on when Zetaris or Meterless credentials are missing.
Trace events and the result JSON go to stdout; the readable summary and logs go to stderr.

--resume and --export work on the Mongo run space (poe_runs, ...) through
app.services.unified_runs, without the API: they need MONGO_URL / DB_NAME
pointing at the database that holds the run. The run summary or export
result goes to stdout as JSON. Upload stays off unless BI_OBJECT_STORAGE_UPLOAD is set.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
LOG = logging.getLogger('run')
_CREDENTIALS = {
    'Zetaris': (('ZETARIS_API_KEY',), ('ZETARIS_ORG_ID',)),
    'Meterless': (('METERLESS_RUNTIME_TOKEN', 'MODEL_API_KEY'),),
}


def prepare_environment(load_env_files: bool = True) -> None:
    """Run before any app import: .env files, Mongo defaults, SAMPLE_MODE switch."""
    if load_env_files:
        from dotenv import load_dotenv
        for path in (REPO_ROOT / '.env', BACKEND_DIR / '.env'):
            if path.is_file():
                load_dotenv(path)  # never overrides variables already set
    # app.config reads these at import time; the pipeline itself never connects.
    os.environ.setdefault('MONGO_URL', 'mongodb://127.0.0.1:27017')
    os.environ.setdefault('DB_NAME', 'bi_local')
    missing = [' or '.join(names) for groups in _CREDENTIALS.values() for names in groups
               if not any(os.environ.get(name, '').strip() for name in names)]
    if os.environ.get('SAMPLE_MODE', '').strip():
        LOG.info('SAMPLE_MODE=%s (set explicitly)', os.environ['SAMPLE_MODE'])
    elif missing:
        os.environ['SAMPLE_MODE'] = 'true'
        LOG.warning('SAMPLE_MODE enabled automatically; missing: %s', ', '.join(missing))
    else:
        LOG.info('Live mode: Zetaris and Meterless credentials found')
    # The app package lives in backend/ (this file's directory), not the repo root.
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def _read_input(arg: str) -> dict:
    path = Path(arg)
    if not path.is_file() and not path.is_absolute() and (REPO_ROOT / path).is_file():
        path = REPO_ROOT / path
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('input must be a JSON object: {"zone_name": str, "run_id": optional str}')
    return data


def _print_summary(result: dict) -> None:
    """Readable summary on stderr, so stdout stays machine-readable JSON."""
    out = sys.stderr
    print('Pipeline Result', file=out)
    print(f"Run ID:   {result['run_id']}", file=out)
    print(f"Zone:     {result['zone']}", file=out)
    print(f"Mode:     {result['mode']}", file=out)
    print(f"Ports:    {len(result['ports'])}", file=out)
    print(f"Summary:  {result.get('summary', {})}", file=out)
    print(f"Brief:    {len(result.get('brief') or '')} chars", file=out)
    llm = result.get('llm') or {}
    if llm:
        print(f"LLM:      {llm.get('provider', 'N/A')} / {llm.get('model', 'N/A')}", file=out)
        if llm.get('fallback_reason'):
            print(f"Fallback: {llm['fallback_reason']}", file=out)
    if result.get('error'):
        print(f"Error:    {result['error']}", file=out)
    mem = result.get('memory') or {}
    if mem:
        print(f"Memory:   {mem.get('recalled', 0)} recalled, "
              f"{'remembered' if mem.get('remembered') else 'not remembered'}", file=out)


def _default_db():
    from app.db import db
    return db


async def resume_run(run_id: str, *, retry_errored: bool = False, db=None, state=None) -> dict:
    """Resume a run through the unified runner; logs go to stderr."""
    from app.core.tasks import TaskState
    from app.services import unified_runs

    if state is None:
        state = TaskState(max_logs=2000)
        state.start()
        _log = state.log

        def log(msg: str) -> None:
            _log(msg)
            print(msg, file=sys.stderr, flush=True)
        state.log = log
    try:
        return await unified_runs.start_run(db if db is not None else _default_db(), state,
                                            run_id=run_id, resume=True,
                                            retry_errored=retry_errored)
    finally:
        state.finish()


async def export_run(run_id: str, *, db=None, **kwargs) -> dict:
    from app.services import unified_runs
    return await unified_runs.export_run(db if db is not None else _default_db(), run_id, **kwargs)


def run_command(args, db=None) -> int:
    """--resume / --export. Result JSON on stdout, errors on stderr."""
    import asyncio
    try:
        if args.resume:
            result = asyncio.run(resume_run(args.resume, retry_errored=args.retry_errored, db=db))
        else:
            result = asyncio.run(export_run(args.export, db=db))
    except (ValueError, FileExistsError) as exc:
        LOG.error('%s', exc)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str), flush=True)
    return 1 if args.resume and result.get('errors') else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--zone', help='EEZ zone name to verify')
    mode.add_argument('--input', help='JSON file: {"zone_name": str, "run_id": optional str}')
    mode.add_argument('--serve', action='store_true', help='serve POST /run, the demo page and GET /healthz')
    mode.add_argument('--resume', metavar='RUN_ID', help='resume a PoE run; completed zones are never re-run')
    mode.add_argument('--export', metavar='RUN_ID', help='export a finished run to data/exports/RUN_ID')
    parser.add_argument('--retry-errored', action='store_true',
                        help='with --resume: redo errored zones once (default: errored zones count as done)')
    parser.add_argument('--run-id', help='Run ID (generated if not provided)')
    parser.add_argument('--sample-mode', action='store_true',
                        help='Run in sample mode (offline, no API keys needed)')
    parser.add_argument('--timeout', type=float, default=300.0,
                        help='Pipeline timeout in seconds (default: 300)')
    parser.add_argument('--trace', help='Custom trace log path')
    parser.add_argument('--output', help='also write the result JSON to this file')
    parser.add_argument('--no-dotenv', action='store_true',
                        help='ignore .env files (simulates a fresh clone)')
    args = parser.parse_args(argv)
    if args.retry_errored and not args.resume:
        parser.error('--retry-errored requires --resume')
    level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), stream=sys.stderr,
                        format='%(levelname)s %(name)s: %(message)s')
    if args.sample_mode:  # explicit offline, reproducible mode
        os.environ['SAMPLE_MODE'] = 'true'
    if args.trace:
        os.environ['TRACE_LOG_PATH'] = args.trace
    prepare_environment(load_env_files=not args.no_dotenv)

    if args.resume or args.export:
        return run_command(args)

    if args.serve:
        import uvicorn
        from app.pipeline.api import app
        uvicorn.run(app, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '8000')))
        return 0

    import asyncio
    from app.pipeline.run_pipeline import run_pipeline, validate_input
    try:
        if args.input:
            data = _read_input(args.input)
            zone_name, run_id = validate_input(data.get('zone_name'), data.get('run_id'))
        else:
            zone_name, run_id = validate_input(args.zone, args.run_id)
    except (OSError, ValueError) as exc:
        LOG.error('Invalid input: %s', exc)
        return 2
    result = asyncio.run(run_pipeline(zone_name, run_id, timeout_s=args.timeout))
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text, flush=True)
    if args.output:
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    _print_summary(result)
    return 1 if result['error'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
