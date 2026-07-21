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
