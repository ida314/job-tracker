"""Store semantics: first_seen stability, repost reopen, closure on disappearance."""

from jobtracker import store
from jobtracker.models import Decision, Posting, Verdict


def _conn():
    return store.connect(":memory:")


def _p(jid, title="Software Engineer"):
    return Posting("Acme", jid, title, f"https://x/{jid}", "NYC")


def test_new_then_stable_first_seen():
    conn = _conn()
    new, closed = store.sync_postings(conn, "Acme", [_p("1"), _p("2")], "2026-07-01")
    assert {p.ats_job_id for p in new} == {"1", "2"} and closed == []

    # Next day: id 1 remains, id 2 gone, id 3 new.
    new, closed = store.sync_postings(conn, "Acme", [_p("1"), _p("3")], "2026-07-02")
    assert [p.ats_job_id for p in new] == ["3"]
    assert closed == ["2"]

    rows = {r["ats_job_id"]: r for r in conn.execute("SELECT * FROM postings")}
    assert rows["1"]["first_seen"] == "2026-07-01"  # unchanged
    assert rows["1"]["last_seen"] == "2026-07-02"  # advanced
    assert rows["2"]["closed_at"] == "2026-07-02"  # closed
    assert rows["3"]["first_seen"] == "2026-07-02"


def test_repost_reopens_closed():
    conn = _conn()
    store.sync_postings(conn, "Acme", [_p("1")], "2026-07-01")
    store.sync_postings(conn, "Acme", [], "2026-07-02")  # 1 disappears -> closed
    assert conn.execute("SELECT closed_at FROM postings").fetchone()[0] == "2026-07-02"
    new, closed = store.sync_postings(conn, "Acme", [_p("1")], "2026-07-03")  # returns
    assert new == [] and closed == []
    row = conn.execute("SELECT * FROM postings").fetchone()
    assert row["closed_at"] is None  # reopened
    assert row["first_seen"] == "2026-07-01"  # original observation preserved


def test_a_later_fetch_without_a_date_does_not_erase_the_one_we_have():
    """posted_on is written with COALESCE, and that is load-bearing.

    Greenhouse's true posted date (`first_published`) only exists on the per-posting
    detail payload, so the nightly bulk pass supplies posted_on=None for every one of
    the 47 Greenhouse boards. A plain assignment would blank the date every night and
    the recency term would go permanently neutral without anything looking broken.
    """
    conn = _conn()
    dated = Posting("Acme", "1", "SWE", "u", posted_on="2026-07-31")
    store.sync_postings(conn, "Acme", [dated], "2026-08-01")
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] == "2026-07-31"

    undated = Posting("Acme", "1", "SWE", "u", posted_on=None)
    store.sync_postings(conn, "Acme", [undated], "2026-08-02")
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] == "2026-07-31"

    # A better date later — first_published arriving with the description — does win.
    better = Posting("Acme", "1", "SWE", "u", posted_on="2026-03-14")
    store.sync_postings(conn, "Acme", [better], "2026-08-03")
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] == "2026-03-14"


def test_backfill_normalizes_stored_rows_and_then_stops():
    conn = _conn()
    from jobtracker.sources import get_source

    store.sync_postings(
        conn, "Acme", [Posting("Acme", "1", "SWE", "u", posted_at="1785533737281")],
        "2026-08-01",
    )
    sources = {"Acme": get_source("lever")}
    assert store.backfill_posted_on(conn, sources, "2026-08-02") == 1
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] == "2026-07-31"
    # Self-draining: it only looks at posted_on IS NULL.
    assert store.backfill_posted_on(conn, sources, "2026-08-02") == 0


def test_backfill_leaves_unknown_companies_alone():
    """A company dropped from companies.yaml has no adapter — guessing its format is
    how you get a Lever epoch string parsed as an ISO year."""
    conn = _conn()
    store.sync_postings(
        conn, "Gone", [Posting("Gone", "1", "SWE", "u", posted_at="1785533737281")],
        "2026-08-01",
    )
    assert store.backfill_posted_on(conn, {}, "2026-08-02") == 0
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] is None


def test_ever_nonempty():
    conn = _conn()
    assert store.ever_nonempty(conn, "Acme") is False
    store.sync_postings(conn, "Acme", [_p("1")], "2026-07-01")
    assert store.ever_nonempty(conn, "Acme") is True


def test_verdict_upsert_and_query():
    conn = _conn()
    store.sync_postings(conn, "Acme", [_p("1", "Software Engineer, New Grad")], "2026-07-01")
    store.record_verdict(
        conn, Verdict("Acme", "1", Decision.MATCH, "level:new grad", "rules"), "2026-07-01"
    )
    rows = store.postings_with_decision(conn, "match", "2026-07-01")
    assert len(rows) == 1 and rows[0]["title"] == "Software Engineer, New Grad"
    # Older `since` still includes it; a future `since` excludes it.
    assert store.postings_with_decision(conn, "match", "2026-07-02") == []


def test_closed_posting_excluded_from_report():
    conn = _conn()
    store.sync_postings(conn, "Acme", [_p("1")], "2026-07-01")
    store.record_verdict(
        conn, Verdict("Acme", "1", Decision.MATCH, "x", "rules"), "2026-07-01"
    )
    store.sync_postings(conn, "Acme", [], "2026-07-02")  # closes id 1
    assert store.postings_with_decision(conn, "match", "2026-07-01") == []


def test_application_advances_but_keeps_applied_at():
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE, New Grad", "applied", "2026-07-01")
    store.record_application(
        conn, "Acme", "1", "SWE, New Grad", "interviewing", "2026-07-10", note="phone screen"
    )
    rows = store.all_applications(conn)
    assert len(rows) == 1  # same posting, advanced in place
    assert rows[0]["status"] == "interviewing"
    assert rows[0]["applied_at"] == "2026-07-01"  # set once, preserved on update
    assert rows[0]["updated_at"] == "2026-07-10"  # moves on every change
    assert rows[0]["note"] == "phone screen"
    assert store.application_count(conn) == 1


def test_application_rejects_unknown_status():
    conn = _conn()
    try:
        store.record_application(conn, "Acme", "1", "SWE", "ghosted", "2026-07-01")
        assert False, "expected ValueError"
    except ValueError:
        pass


# -- board health: the failure streak the repair detector reads -------------------------
def test_consecutive_failures_round_trips():
    from jobtracker.models import BoardHealth, HealthStatus

    conn = _conn()
    store.upsert_health(
        conn,
        BoardHealth("Acme", HealthStatus.FETCH_FAILED, consecutive_failures=3),
        "2026-08-03",
    )
    assert store.get_health(conn, "Acme").consecutive_failures == 3

    # And it must come back DOWN on update, not just up. Leaving a repaired board
    # carrying its old streak would keep the detector firing on a healthy board.
    store.upsert_health(
        conn, BoardHealth("Acme", HealthStatus.OK, consecutive_failures=0), "2026-08-04"
    )
    assert store.get_health(conn, "Acme").consecutive_failures == 0


def test_unhealthy_health_returns_typed_rows_with_the_streak():
    from jobtracker.models import BoardHealth, HealthStatus

    conn = _conn()
    store.upsert_health(conn, BoardHealth("Ok", HealthStatus.OK), "d")
    store.upsert_health(
        conn, BoardHealth("Bad", HealthStatus.FETCH_FAILED, consecutive_failures=4), "d"
    )
    got = store.unhealthy_health(conn)
    assert [h.company for h in got] == ["Bad"]
    assert got[0].consecutive_failures == 4


# -- repair proposals ------------------------------------------------------------------
class _Proposal:
    company = "HubSpot"
    from_ats, from_slug = "greenhouse", "hubspot"
    to_ats, to_slug = "greenhouse", "hubspotjobs"
    board_name = "HubSpot"
    job_count = 214
    sample_titles = ("Backend Engineer", "SRE")
    evidence_kind = "identity"
    found_by = "regex"
    evidence = "for=hubspotjobs"
    trigger = "suspect_empty"


def test_a_proposal_is_upserted_not_appended():
    """A board broken for a week re-derives the same conclusion nightly. One row."""
    conn = _conn()
    store.record_proposal(conn, _Proposal(), "2026-08-03")
    store.record_proposal(conn, _Proposal(), "2026-08-04")
    rows = store.open_proposals(conn)
    assert len(rows) == 1
    assert rows[0]["verified_at"] == "2026-08-04"
    assert rows[0]["sample_titles"] == "Backend Engineer · SRE"


def test_applied_proposals_leave_the_queue():
    conn = _conn()
    store.record_proposal(conn, _Proposal(), "2026-08-03")
    store.mark_proposal_applied(conn, "HubSpot", "2026-08-03")
    assert store.open_proposals(conn) == []


def test_re_proposing_after_an_apply_reopens_the_claim():
    """A board that broke again has a new, unreviewed claim against it. Carrying the
    old applied_at forward would hide it from the queue."""
    conn = _conn()
    store.record_proposal(conn, _Proposal(), "2026-08-03")
    store.mark_proposal_applied(conn, "HubSpot", "2026-08-03")
    store.record_proposal(conn, _Proposal(), "2026-09-01")
    assert [r["company"] for r in store.open_proposals(conn)] == ["HubSpot"]
