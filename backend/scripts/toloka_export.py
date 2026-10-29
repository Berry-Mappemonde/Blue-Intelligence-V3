#!/usr/bin/env python3
"""Build a local Toloka review batch (TSV + JSON).

Never sent without Clement approval. This script does not call Toloka
and does not upload anything.

Usage (from backend/):
    python3 scripts/toloka_export.py --run runs/<id> --out /tmp/toloka
    python3 scripts/toloka_export.py --answers answers.json --out /tmp/toloka
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.services.toloka_export import (  # noqa: E402
    aggregate_batch,
    load_answer_rows,
    prepare_run,
    write_aggregation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a stratified Toloka review batch on disk. "
            "Never sent without Clement approval. No network, no upload."
        )
    )
    parser.add_argument(
        "--run",
        type=Path,
        help="Run directory (entities.geojson) or a JSON/GeoJSON file of entities",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Directory where TSV and JSON are written",
    )
    parser.add_argument("--batch", type=int, default=None, help="Batch size (default 23)")
    parser.add_argument("--overlap", type=int, default=None, help="Annotators per item (default 3)")
    parser.add_argument("--seed", type=int, default=None, help="Sample seed (default 42)")
    parser.add_argument(
        "--answers",
        type=Path,
        help="Local JSON of annotator answers to aggregate. Not uploaded.",
    )
    args = parser.parse_args(argv)

    if args.answers is None and (args.run is None or args.out is None):
        parser.error("--run and --out are required to build a batch")

    if args.run is not None:
        if args.out is None:
            parser.error("--out is required with --run")
        try:
            written = prepare_run(
                args.run,
                args.out,
                batch_size=args.batch,
                overlap=args.overlap,
                seed=args.seed,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(
            f"wrote {written['batch_size']} items "
            f"(overlap {written['overlap']}) to {args.out} — "
            "never sent without Clement approval"
        )

    if args.answers is not None:
        try:
            rows = load_answer_rows(args.answers)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        summary = aggregate_batch(rows)
        if args.out is not None:
            path = write_aggregation(summary, args.out)
            counts = summary["counts"]
            print(
                f"aggregated {len(summary['items'])} items → {path} "
                f"(confirmed {counts['confirmed']}, split {counts['split']}, "
                f"rejected {counts['rejected']})"
            )
        else:
            json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
