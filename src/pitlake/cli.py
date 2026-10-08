"""Command line: `pitlake ingest | asof | history | changes | corrections | runs | serve`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from pitlake import query
from pitlake.config import data_dir, load_universe
from pitlake.lake import Lake


def _lake(args) -> Lake:
    return Lake(Path(args.lake) if args.lake else data_dir() / "lake")


def _print(rows: list[dict]) -> None:
    print(json.dumps(rows, indent=2, default=str))


def cmd_ingest(args) -> int:
    from pitlake.pipeline import run

    if args.sec_user_agent:  # e.g. passed as a job parameter where env vars are awkward
        os.environ["SEC_USER_AGENT"] = args.sec_user_agent
    tickers = args.tickers or (load_universe() if args.source == "sec" else None)
    rec = run(args.source, tickers, lake_root=_lake(args).root)
    print(f"{rec.status}: {rec.message} (run {rec.run_id})")
    return 0 if rec.status == "succeeded" else 1


def cmd_asof(args) -> int:
    con = _lake(args).connect()
    if args.quarterly:
        _print(query.quarterly(con, args.ticker, args.metric, args.as_of))
    else:
        _print(query.metric_values(con, args.ticker, args.metric, args.as_of, args.period_type))
    return 0


def cmd_history(args) -> int:
    con = _lake(args).connect()
    _print(query.fact_history(con, args.ticker, args.metric, args.period_end, args.period_start))
    return 0


def cmd_changes(args) -> int:
    con = _lake(args).connect()
    rows = query.changes(
        con, args.ticker, args.since, args.classification, args.min_pct, args.limit
    )
    for r in rows:
        print(f"[{r['classification']}] {r['effective_date']} {r['ticker']}: {r['explanation']}\n")
    return 0


def cmd_corrections(args) -> int:
    from pitlake.corrections import ReviewError, approve, reject

    lake = _lake(args)
    try:
        if args.action == "list":
            _print(query.corrections(lake.connect(), None if args.status == "all" else args.status))
        elif args.action == "approve":
            c = approve(lake, args.id, args.reviewer, args.note)
            print(f"approved {c.correction_id}: {c.ticker} {c.concept} -> {c.proposed_value}")
        elif args.action == "reject":
            c = reject(lake, args.id, args.reviewer, args.note)
            print(f"rejected {c.correction_id}")
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_runs(args) -> int:
    _print(query.runs(_lake(args).connect(), args.limit))
    return 0


def cmd_study(args) -> int:
    from pitlake.research import render_markdown

    text = render_markdown(_lake(args).connect(), args.as_of)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from pitlake.api import create_app

    uvicorn.run(create_app(_lake(args).root), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="pitlake", description=__doc__)
    p.add_argument("--lake", help="lake directory (default: data/lake)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="run the pipeline")
    s.add_argument("--source", choices=["sample", "sec", "github"], default="sample")
    s.add_argument("--sec-user-agent", help="contact for SEC (default: $SEC_USER_AGENT)")
    s.add_argument("tickers", nargs="*")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("asof", help="a metric as it was known on a date")
    s.add_argument("ticker")
    s.add_argument("metric")
    s.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    s.add_argument("--period-type", choices=["quarter", "annual", "ytd", "instant"])
    s.add_argument("--quarterly", action="store_true", help="quarterly series incl. derived Q4")
    s.set_defaults(fn=cmd_asof)

    s = sub.add_parser("history", help="every version of one number")
    s.add_argument("ticker")
    s.add_argument("metric")
    s.add_argument("period_end", type=date.fromisoformat)
    s.add_argument("--period-start", type=date.fromisoformat)
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("changes", help="what changed and why")
    s.add_argument("--ticker")
    s.add_argument("--since", type=date.fromisoformat)
    s.add_argument(
        "--classification",
        choices=["restatement", "split_adjustment", "manual_correction", "data_revision"],
    )
    s.add_argument("--min-pct", type=float, default=0.0)
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_changes)

    s = sub.add_parser("corrections", help="review quarantined changes")
    s.add_argument("action", choices=["list", "approve", "reject"])
    s.add_argument("id", nargs="?")
    s.add_argument(
        "--status", default="pending", choices=["pending", "approved", "rejected", "all"]
    )
    s.add_argument("--reviewer", default="")
    s.add_argument("--note", default="")
    s.set_defaults(fn=cmd_corrections)

    s = sub.add_parser("runs", help="recent pipeline runs")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("study", help="research example: point-in-time vs latest data")
    s.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    s.add_argument("--out", help="write markdown here instead of printing it")
    s.set_defaults(fn=cmd_study)

    s = sub.add_parser("serve", help="start the API and analyst page")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    if args.cmd == "corrections" and args.action != "list" and not (args.id and args.reviewer):
        p.error("approve/reject need an id and --reviewer")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
