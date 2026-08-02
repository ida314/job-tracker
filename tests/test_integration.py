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


# -- description caching -----------------------------------------------------------
# `check` caches descriptions so every downstream pass is offline with respect to the
# ATSes. These pin the three rules that make that affordable and safe.
class _FakeFetcher:
    """Records what was requested; answers with a description and a posted date."""

    def __init__(self, fail=()):
        self.requested = []
        self.fail = set(fail)

    def fetch_job_detail(self, company, ats_job_id):
        self.requested.append(ats_job_id)
        if ats_job_id in self.fail:
            return None, None
        return f"description for {ats_job_id}", "2023-11-01T00:00:00-04:00"


def _cache(conn, fetcher, wanted, budget=100, today="2026-08-02"):
    from jobtracker.cli import _cache_descriptions

    _cache_descriptions(conn, fetcher, wanted, budget, today)


def _gh(jid, description=""):
    company = Company(name="Acme", ats="greenhouse", slug="acme")
    return company, Posting("Acme", jid, "Software Engineer", "u", description=description)


def test_bulk_descriptions_are_free_and_cost_no_request():
    """Ashby and Lever ship descriptionPlain in the list call — never refetch those."""
    conn = store.connect(":memory:")
    company, posting = _gh("1", description="already here")
    store.sync_postings(conn, "Acme", [posting], "2026-08-02")
    f = _FakeFetcher()
    _cache(conn, f, [(company, posting)])
    assert f.requested == []
    assert store.get_description(conn, "Acme", "1") == "already here"


def test_greenhouse_description_is_fetched_once_ever():
    conn = store.connect(":memory:")
    company, posting = _gh("1")
    store.sync_postings(conn, "Acme", [posting], "2026-08-02")

    f = _FakeFetcher()
    _cache(conn, f, [(company, posting)])
    assert f.requested == ["1"]
    assert store.get_description(conn, "Acme", "1") == "description for 1"
    # The detail payload is also where first_published lives — take it while there.
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] == "2023-11-01"

    # Second run: already stored, so no second request.
    f2 = _FakeFetcher()
    _cache(conn, f2, [(company, posting)])
    assert f2.requested == []


def test_budget_caps_requests_and_defers_the_rest():
    """A bad night must not turn a 30-second job into a 40-minute one."""
    conn = store.connect(":memory:")
    wanted = []
    for jid in "12345":
        company, posting = _gh(jid)
        store.sync_postings(conn, "Acme", [posting], "2026-08-02")
        wanted.append((company, posting))

    f = _FakeFetcher()
    _cache(conn, f, wanted, budget=2)
    assert len(f.requested) == 2
    stored = conn.execute(
        "SELECT COUNT(*) FROM postings WHERE description IS NOT NULL"
    ).fetchone()[0]
    assert stored == 2  # the other three are simply picked up tomorrow


def test_a_failed_description_leaves_the_row_retryable_and_raises_nothing():
    """A 500 on one job detail is not a broken board.

    It must not raise, must not mark anything unhealthy, and must leave description
    NULL — the sentinel meaning "never fetched" — so tomorrow tries again.
    """
    conn = store.connect(":memory:")
    company, posting = _gh("1")
    store.sync_postings(conn, "Acme", [posting], "2026-08-02")
    _cache(conn, _FakeFetcher(fail={"1"}), [(company, posting)])
    assert store.get_description(conn, "Acme", "1") is None
