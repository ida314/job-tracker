"""Command-line entry point.

Subcommands:
  migrate       backend-newgrad-2027-tracker.md -> companies.yaml (one-time)
  check         the daily pipeline: fetch -> health -> store -> match -> report
  verify-slugs  fetch each API board's identity; --write seeds expected_board_name
  report        re-render the latest state from state.db without fetching
  add-company   append a curated entry to companies.yaml

Only `check` touches the network in the normal daily path; `report` is offline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from . import config, report as report_mod, store
from .criteria import load_criteria
from .fetch import Fetcher
from .health import evaluate
from .match import match
from .migrate import migrate as run_migrate
from .models import Company
from .sources import get_source


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# -- migrate -----------------------------------------------------------------------
def cmd_migrate(args: argparse.Namespace) -> int:
    entries = run_migrate(args.markdown, args.out)
    tiers: dict[object, int] = {}
    methods: dict[str, int] = {}
    for e in entries:
        tiers[e.get("tier", "—")] = tiers.get(e.get("tier", "—"), 0) + 1
        methods[e.get("check_method", "manual")] = (
            methods.get(e.get("check_method", "manual"), 0) + 1
        )
    print(f"Wrote {len(entries)} companies to {args.out}")
    print(f"  by check_method: {dict(sorted(methods.items()))}")
    print(f"  by tier:         {dict(sorted(tiers.items(), key=lambda kv: str(kv[0])))}")
    return 0


# -- check -------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    companies = config.load_companies(args.companies)
    criteria = load_criteria(args.criteria)
    today = args.since or _today()
    started = _now()

    api = [c for c in companies if c.check_method == "api" and get_source(c.ats)]
    skipped = [c for c in companies if c.check_method == "api" and not get_source(c.ats)]
    for c in skipped:
        print(f"warning: no adapter for {c.name} (ats={c.ats}) — skipping", file=sys.stderr)

    fetcher = Fetcher()
    try:
        results = fetcher.fetch_all(api)
    finally:
        fetcher.close()

    config.ensure_data_dir()
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    by_name = {c.name: c for c in api}

    stats = {"companies": len(api), "ok": 0, "failed": 0, "new_postings": 0, "matches": 0}
    for res in results:
        company = by_name[res.company]
        prior = store.get_health(conn, company.name)
        ever = store.ever_nonempty(conn, company.name)
        health = evaluate(company, res, prior, today, ever)
        store.upsert_health(conn, health, today)

        if health.status.value == "ok":
            stats["ok"] += 1
            new_postings, _ = store.sync_postings(conn, company.name, res.postings, today)
            stats["new_postings"] += len(new_postings)
            for posting in res.postings:
                verdict = match(posting, criteria)
                store.record_verdict(conn, verdict, today)
                if verdict.decision.value == "match" and posting in new_postings:
                    stats["matches"] += 1
        else:
            stats["failed"] += 1

    store.record_run(conn, started, _now(), stats)
    conn.commit()

    text = report_mod.build_report(conn, companies, today, since=today, mark_manual=True)
    conn.commit()
    conn.close()

    if args.output:
        Path(args.output).write_text(text)
        print(f"report written to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


# -- verify-slugs ------------------------------------------------------------------
def cmd_verify_slugs(args: argparse.Namespace) -> int:
    companies = config.load_companies(args.companies)
    api = [c for c in companies if c.check_method == "api" and get_source(c.ats)]
    fetcher = Fetcher()
    observed: dict[str, str] = {}
    try:
        for c in api:
            res = fetcher.fetch_company(c)
            status = (
                f"FAIL {res.error}"
                if res.error
                else f"{len(res.postings):>4} jobs  board={res.observed_board_name!r}"
            )
            drift = ""
            if c.expected_board_name and res.observed_board_name:
                from .health import identity_matches

                if not identity_matches(c.expected_board_name, res.observed_board_name):
                    drift = "  <-- DRIFT vs expected " + repr(c.expected_board_name)
            print(f"{c.name:24} {c.ats}/{c.slug:22} {status}{drift}")
            if res.observed_board_name:
                observed[c.name] = res.observed_board_name
    finally:
        fetcher.close()

    if args.write:
        _write_expected_board_names(args.companies or config.COMPANIES_YAML, observed)
        print(f"\nSeeded expected_board_name for {len(observed)} companies.")
    return 0


def _write_expected_board_names(path: str | Path, observed: dict[str, str]) -> None:
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    for entry in data:
        name = entry.get("name")
        if name in observed:
            entry["expected_board_name"] = observed[name]
    from .migrate import _HEADER

    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(_HEADER + body)


# -- rematch -----------------------------------------------------------------------
def cmd_rematch(args: argparse.Namespace) -> int:
    """Re-apply the criteria to every OPEN stored posting. No network.

    Use after tuning criteria.yaml to refresh verdicts without re-fetching boards.
    """
    from .models import Posting

    criteria = load_criteria(args.criteria)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()
    rows = conn.execute(
        "SELECT company, ats_job_id, title, location, url, posted_at "
        "FROM postings WHERE closed_at IS NULL"
    ).fetchall()
    for r in rows:
        posting = Posting(
            company=r["company"],
            ats_job_id=r["ats_job_id"],
            title=r["title"],
            url=r["url"],
            location=r["location"] or "",
            posted_at=r["posted_at"],
        )
        store.record_verdict(conn, match(posting, criteria), today)
    conn.commit()
    counts = store.counts_by_verdict(conn)
    conn.close()
    print(f"rematched {len(rows)} open postings")
    print(f"  verdicts: {counts}")
    return 0


# -- report ------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    companies = config.load_companies(args.companies)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    since = args.since or _today()
    text = report_mod.build_report(conn, companies, _today(), since=since, mark_manual=False)
    conn.close()
    print(text)
    return 0


# -- add-company -------------------------------------------------------------------
def cmd_add_company(args: argparse.Namespace) -> int:
    path = Path(args.companies or config.COMPANIES_YAML)
    data = yaml.safe_load(path.read_text()) if path.exists() else []
    if any(e.get("name") == args.name for e in data):
        print(f"error: {args.name!r} already present", file=sys.stderr)
        return 1
    entry = {"name": args.name, "ats": args.ats}
    if args.slug:
        entry["slug"] = args.slug
    if args.tier is not None:
        entry["tier"] = args.tier
    if args.category:
        entry["category"] = args.category
    entry["check_method"] = args.check_method
    if args.notes:
        entry["notes"] = args.notes
    entry["expected_board_name"] = None
    data.append(entry)
    from .migrate import _HEADER

    path.write_text(_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    print(f"added {args.name} to {path}")
    return 0


# -- arg parsing -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobtracker", description=__doc__)
    p.add_argument("--companies", help="path to companies.yaml (default: repo root)")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="markdown -> companies.yaml")
    m.add_argument("--markdown", default="backend-newgrad-2027-tracker.md")
    m.add_argument("--out", default=str(config.COMPANIES_YAML))
    m.set_defaults(func=cmd_migrate)

    c = sub.add_parser("check", help="run the daily pipeline")
    c.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    c.add_argument("--db", default=None)
    c.add_argument("--since", default=None, help="override today's date (YYYY-MM-DD)")
    c.add_argument("--output", default=None, help="write report to a file instead of stdout")
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("verify-slugs", help="fetch board identities; --write seeds them")
    v.add_argument("--write", action="store_true")
    v.set_defaults(func=cmd_verify_slugs)

    rm = sub.add_parser("rematch", help="re-apply criteria to stored postings (no network)")
    rm.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    rm.add_argument("--db", default=None)
    rm.add_argument("--since", default=None)
    rm.set_defaults(func=cmd_rematch)

    r = sub.add_parser("report", help="re-render state.db without fetching")
    r.add_argument("--db", default=None)
    r.add_argument("--since", default=None)
    r.set_defaults(func=cmd_report)

    a = sub.add_parser("add-company", help="append a curated entry")
    a.add_argument("--name", required=True)
    a.add_argument("--ats", required=True)
    a.add_argument("--slug", default="")
    a.add_argument("--tier", type=int, default=None)
    a.add_argument("--category", default="")
    a.add_argument("--check-method", dest="check_method", default="manual")
    a.add_argument("--notes", default="")
    a.set_defaults(func=cmd_add_company)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
