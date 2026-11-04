"""Score a PoE run against a hand-checked gold file (app/services/gold_score.py).

    python scripts/score_gold.py --run-id 20261103-161330-59c1e5         # stored run (Mongo, read only)
    python scripts/score_gold.py --csv tests/fixtures/run_59c1e5_ports.csv \\
        --gold data/gold/pilot_4zones.json                               # an export, offline
    ... --json                                                           # the full score as JSON

Without --gold, the first file in backend/data/gold that covers every zone of the run is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import gold_score  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run-id", help="a stored run (poe_run_zones / poe_run_ports)")
    src.add_argument("--csv", help="a run port export (zone, mrgid, port_name, merged_names, level, …)")
    ap.add_argument("--gold", help="gold file (default: the one covering the run's zones)")
    ap.add_argument("--json", action="store_true", help="print the full score as JSON")
    args = ap.parse_args(argv)

    if args.csv:
        ports = gold_score.ports_from_csv(args.csv)
        if args.gold:
            gold_path, gold = Path(args.gold), gold_score.load_gold(args.gold)
        else:
            hit = gold_score.gold_for_zones({p["mrgid"] for p in ports})
            if hit is None:
                print("no gold file covers these zones", file=sys.stderr)
                return 2
            gold_path, gold = hit
        score = gold_score.score_ports(ports, gold)
        score.update(run_id=Path(args.csv).name, gold_file=gold_path.name)
    else:
        from app.db import db
        score = asyncio.run(gold_score.score_run(db, args.run_id, gold_path=args.gold))
        if score is None:
            print(f"no gold file covers the zones of run {args.run_id}", file=sys.stderr)
            return 2
    print(json.dumps(score, ensure_ascii=False, indent=2) if args.json else gold_score.format_report(score))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
