"""End-to-end wiring of the run loop against an in-memory DB (no network).

Mirrors exactly what cmd_check does per company, then renders the report — so the
health -> store -> match -> report glue is covered without hitting live boards.
"""

from jobtracker import config, report, store
from jobtracker.criteria import load_criteria
from jobtracker.health import evaluate
from jobtracker.match import match
from jobtracker.models import Company, FetchResult, Posting


def _run(conn, company, result, criteria, today):
    prior = store.get_health(conn, company.name)
    ever = store.ever_nonempty(conn, company.name)
    health = evaluate(company, result, prior, today, ever)
    store.upsert_health(conn, health, today)
    if health.status.value == "ok":
        store.sync_postings(conn, company.name, result.postings, today)
        for p in result.postings:
            store.record_verdict(conn, match(p, criteria), today)
    conn.commit()


def test_full_loop_report():
    conn = store.connect(":memory:")
    criteria = load_criteria(config.CRITERIA_YAML)
    companies = [
        Company("Acme", "greenhouse", "acme", tier=1, check_method="api"),
        Company("DeadCo", "greenhouse", "dead", tier=2, check_method="api"),
        Company("HandCo", "workday", "", tier=3, check_method="manual",
                notes="check the workday tenant"),
    ]

    acme = FetchResult("Acme", "greenhouse", "acme", ok=True, status_code=200,
                       observed_board_name="Acme",
                       postings=[
                           Posting("Acme", "1", "Software Engineer, New Grad", "u1", "NYC"),
                           Posting("Acme", "2", "Senior Staff Engineer", "u2", "NYC"),
                           Posting("Acme", "3", "Platform Engineer", "u3", "NYC"),
                       ])
    dead = FetchResult("DeadCo", "greenhouse", "dead", ok=False, status_code=404,
                       error="HTTP 404")

    _run(conn, companies[0], acme, criteria, "2026-07-20")
    _run(conn, companies[1], dead, criteria, "2026-07-20")

    text = report.build_report(conn, companies, "2026-07-20", since="2026-07-20")

    assert "## New matches (1)" in text
    assert "Software Engineer, New Grad" in text
    assert "Senior Staff Engineer" not in text  # rejected
    assert "## Uncertain — needs a human (1)" in text
    assert "Platform Engineer" in text
    assert "`fetch_failed`" in text and "DeadCo" in text
    assert "HandCo" in text  # manual, surfaced

    counts = store.counts_by_verdict(conn)
    assert counts == {"match": 1, "reject": 1, "uncertain": 1}
