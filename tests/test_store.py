"""Store semantics: first_seen stability, repost reopen, closure on disappearance."""

import pytest

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
        conn, "Acme", "1", "SWE, New Grad", "screen", "2026-07-10", note="phone screen"
    )
    rows = store.all_applications(conn)
    assert len(rows) == 1  # same posting, advanced in place
    assert rows[0]["status"] == "screen"
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


# -- applications: the outer loop ----------------------------------------------------
def test_all_seven_statuses_are_accepted_and_others_are_not():
    conn = _conn()
    for status in store.APPLICATION_STATUSES:
        store.record_application(conn, "Acme", status, "SWE", status, "2026-08-01")
    assert store.application_count(conn) == len(store.APPLICATION_STATUSES)
    # `interviewing` was the old name for what is now three separate stages. It must
    # fail like any other unknown word rather than being quietly accepted.
    for bad in ("interviewing", "ghosted", ""):
        try:
            store.record_application(conn, "Acme", "x", "SWE", bad, "2026-08-01")
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_optional_fields_survive_a_later_status_only_write():
    """The COALESCE rule. `jobtracker apply` passes none of the new fields, so without
    it a status change would blank a URL set from the web page."""
    conn = _conn()
    store.record_application(
        conn, "Acme", "1", "SWE", "applied", "2026-08-01",
        url="https://acme.example/jobs/1", location="NYC", source="manual",
        next_action="2026-08-10", next_action_note="follow up",
    )
    store.record_application(conn, "Acme", "1", "SWE", "screen", "2026-08-05")
    row = store.get_application(conn, "Acme", "1")
    assert row["status"] == "screen"
    assert row["url"] == "https://acme.example/jobs/1"
    assert row["location"] == "NYC"
    assert row["source"] == "manual"          # not reset to 'tracked'
    assert row["next_action"] == "2026-08-10"
    assert row["next_action_note"] == "follow up"


def test_an_empty_string_clears_a_field_but_none_leaves_it():
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-08-01",
                             next_action="2026-08-10")
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-08-02",
                             next_action="")
    assert store.get_application(conn, "Acme", "1")["next_action"] == ""


def test_source_defaults_to_tracked_when_never_set():
    """Rows written before manual entry existed all came from the pipeline, and the
    column is nullable, so NULL has to read as 'tracked' rather than as None."""
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-08-01")
    assert store.get_application(conn, "Acme", "1")["source"] == "tracked"


def test_repeated_interviews_are_separate_events_on_one_application():
    conn = _conn()
    store.advance_application(conn, "Acme", "1", "SWE", "applied", "2026-08-01")
    store.advance_application(conn, "Acme", "1", "SWE", "oa", "2026-08-04", note="HR")
    store.advance_application(conn, "Acme", "1", "SWE", "interview", "2026-08-11",
                              note="round 1")
    store.advance_application(conn, "Acme", "1", "SWE", "interview", "2026-08-18",
                              note="round 2")
    assert store.application_count(conn) == 1
    events = store.events_by_application(conn)[("Acme", "1")]
    assert [e["status"] for e in events] == ["applied", "oa", "interview", "interview"]
    assert [e["note"] for e in events] == ["", "HR", "round 1", "round 2"]
    assert store.get_application(conn, "Acme", "1")["applied_at"] == "2026-08-01"


def test_record_application_logs_nothing_on_its_own():
    """The split that makes a repeated interview legible: editing a note must not append
    an event, or the history fills with entries recording that you edited a note."""
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-08-01")
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-08-02",
                             note="edited")
    assert store.events_by_application(conn) == {}


def test_manual_ids_are_deterministic_and_namespaced():
    a = store.manual_job_id("Backend Engineer, New Grad")
    assert a == store.manual_job_id("  Backend Engineer, New Grad  ")
    assert a.startswith(store.MANUAL_PREFIX) and store.is_manual(a)
    assert a != store.manual_job_id("Backend Engineer, Senior")
    # A title of pure punctuation still has to produce a distinguishing id, or every
    # such entry at one company would collapse onto the same row.
    assert store.manual_job_id("!!!") != store.manual_job_id("???")


def test_deleting_an_application_takes_its_history_with_it():
    """Orphaned events would reattach the moment the same title was entered again,
    because manual_job_id mints the identical key."""
    conn = _conn()
    store.advance_application(conn, "Acme", "1", "SWE", "applied", "2026-08-01")
    store.advance_application(conn, "Acme", "1", "SWE", "screen", "2026-08-05")
    store.delete_application(conn, "Acme", "1")
    assert store.application_count(conn) == 0
    assert store.events_by_application(conn) == {}


# -- a resume for one posting -------------------------------------------------------
def test_a_posting_resume_is_replaced_rather_than_duplicated():
    conn = _conn()
    store.set_posting_resume(conn, "Acme", "1", "first.pdf", 10, "2026-08-16")
    store.set_posting_resume(conn, "Acme", "1", "second.pdf", 20, "2026-08-17")
    row = store.get_posting_resume(conn, "Acme", "1")
    assert (row["filename"], row["bytes"]) == ("second.pdf", 20)
    assert len(store.posting_resumes(conn)) == 1
    conn.close()


def test_clearing_a_posting_resume_leaves_the_others_alone():
    conn = _conn()
    store.set_posting_resume(conn, "Acme", "1", "a.pdf", 10, "2026-08-16")
    store.set_posting_resume(conn, "Acme", "2", "b.pdf", 10, "2026-08-16")
    store.clear_posting_resume(conn, "Acme", "1")
    assert store.get_posting_resume(conn, "Acme", "1") is None
    assert store.get_posting_resume(conn, "Acme", "2")["filename"] == "b.pdf"
    conn.close()


# -- mail ---------------------------------------------------------------------------
def test_a_proposal_must_name_a_status_that_exists():
    """A status outside the enum would render as a pill nothing styles and, worse, be
    written into an application on accept."""
    conn = _conn()
    with pytest.raises(ValueError):
        store.record_mail_proposal(conn, "m1", "Acme", "1", "hired", "q", "2026-08-16")
    with pytest.raises(ValueError):
        store.resolve_mail_proposal(conn, "m1", "maybe", "2026-08-16")
    conn.close()


def test_a_gap_seen_on_list_has_one_definition_of_its_delimiter():
    conn = _conn()
    store.record_gap(conn, "k", "ask?", "text", "Acme", "2026-08-16")
    store.record_gap(conn, "k", "ask?", "text", "Zeta", "2026-08-16")
    row = store.open_gaps(conn)[0]
    assert store.gap_companies(row) == ["Acme", "Zeta"]
    assert store.gap_companies("") == []
    assert store.gap_companies(None) == []
    conn.close()
