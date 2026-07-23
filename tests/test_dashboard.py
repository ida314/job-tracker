"""Dashboard rendering: escaping, band mapping, read-only-ness, filter data attributes.

The valuable assertions here are the ones a browser would not obviously fail on. HTML
that renders fine can still be silently wrong — an unescaped title, a javascript: href,
a row that carries the wrong tier and so hides under the wrong filter chip.
"""

from jobtracker import dashboard, store
from jobtracker.models import Company, Decision, Posting, Verdict


def _setup(postings_and_verdicts, companies):
    conn = store.connect(":memory:")
    for company, posting, decision in postings_and_verdicts:
        store.sync_postings(conn, company, [posting], "2026-07-01")
        store.record_verdict(
            conn, Verdict(company, posting.ats_job_id, decision, "why", "rules"), "2026-07-01"
        )
    return conn, companies


def _company(name, tier, ats="greenhouse"):
    return Company(name=name, ats=ats, slug=name.lower(), tier=tier, check_method="api")


def test_escapes_hostile_title_and_company():
    """Titles come from third-party APIs. A <script> in one must not become markup."""
    evil = Posting("Acme", "1", '<script>alert("xss")</script>', "https://x/1", "NYC")
    conn, companies = _setup([("Acme", evil, Decision.MATCH)], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert "<script>alert" not in doc
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in doc
    # The page's own trailing <script> block is the only real one.
    assert doc.count("<script>") == 1


def test_rejects_non_http_url_scheme():
    bad = Posting("Acme", "1", "Engineer", "javascript:alert(1)", "NYC")
    conn, companies = _setup([("Acme", bad, Decision.MATCH)], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert "javascript:" not in doc
    assert 'href="#"' in doc


def test_band_mapping_matches_strategy_grouping():
    assert dashboard._band_var(1) == "--band-anchor"
    assert dashboard._band_var(2) == "--band-anchor"
    assert dashboard._band_var(3) == "--band-applied"
    assert dashboard._band_var(5) == "--band-applied"
    assert dashboard._band_var(6) == "--band-research"
    assert dashboard._band_var(7) == "--band-research"
    assert dashboard._band_var("—") == "--band-none"  # untiered


def test_every_band_var_is_defined_in_the_css():
    """A typo'd var name renders as a transparent bar rather than an error."""
    for tier in (1, 3, 7, "—"):
        var = dashboard._band_var(tier)
        assert f"{var}:" in dashboard._CSS
        assert f"{var}-ink:" in dashboard._CSS


def test_rows_carry_filter_attributes():
    conn, companies = _setup(
        [
            ("Acme", Posting("Acme", "1", "Backend Engineer", "https://x/1", "NYC"),
             Decision.MATCH),
            ("Zeta", Posting("Zeta", "9", "Data Engineer", "https://x/9", "Remote"),
             Decision.UNCERTAIN),
        ],
        [_company("Acme", 1), _company("Zeta", 6, ats="ashby")],
    )
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert 'data-tier="1"' in doc and 'data-ats="greenhouse"' in doc
    assert 'data-tier="6"' in doc and 'data-ats="ashby"' in doc
    # The search blob is lowercased so the JS can do a case-insensitive substring test.
    assert "backend engineer" in doc
    # Both verdict buckets are rendered, in their own tables.
    assert "Open matches" in doc and "Uncertain" in doc


def test_dashboard_never_writes_to_the_database():
    """`report` marks manual companies as surfaced; opening a view must not."""
    conn, companies = _setup(
        [("Acme", Posting("Acme", "1", "Engineer", "https://x/1", "NYC"), Decision.MATCH)],
        [_company("Acme", 1), Company(name="Manual Co", ats="workday", slug="",
                                      tier=2, check_method="manual")],
    )
    before = conn.execute("SELECT count(*) FROM manual_checks").fetchone()[0]
    dashboard.build_dashboard(conn, companies, "2026-07-22")
    after = conn.execute("SELECT count(*) FROM manual_checks").fetchone()[0]
    assert before == after == 0
    # ...and the manual company is still surfaced in the page itself.
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "Manual Co" in doc


def test_empty_database_still_renders():
    conn = store.connect(":memory:")
    doc = dashboard.build_dashboard(conn, [], "2026-07-22")
    assert doc.startswith("<!doctype html>")
    assert "No run recorded yet" in doc
    assert doc.rstrip().endswith("</html>")


def test_closed_postings_are_excluded():
    conn = store.connect(":memory:")
    p = Posting("Acme", "1", "Backend Engineer", "https://x/1", "NYC")
    store.sync_postings(conn, "Acme", [p], "2026-07-01")
    store.record_verdict(
        conn, Verdict("Acme", "1", Decision.MATCH, "why", "rules"), "2026-07-01"
    )
    store.sync_postings(conn, "Acme", [], "2026-07-02")  # posting disappears -> closed
    doc = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22")
    assert "Backend Engineer" not in doc
