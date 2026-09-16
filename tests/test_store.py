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


# -- un-learning a bad match ------------------------------------------------------------
def _seed_bad_alias(conn):
    for company in ("Twilio", "Asana"):
        store.upsert_form_field(
            conn, company=company, form_key="country", label="Country*",
            field_type="text", now="2026-08-20", required=True,
            question_key="location", source="dom",
        )
    store.record_gap(conn, question_key="country", ask="Country*", field_type="text",
                     company="Twilio", now="2026-08-20")
    store.resolve_gap(conn, "country", "2026-08-20")
    store.record_plan(conn, company="Twilio", ats_job_id="1", plan_json="[]",
                      fields=1, gaps=0, answers_hash="abc", now="2026-08-20")


def test_forgetting_a_question_is_dry_until_you_ask_for_it(tmp_path):
    """`repair`'s shape, for `repair`'s reason: this rewrites something a run decided,
    and the list of what it would rewrite is worth reading first."""
    conn = store.connect(tmp_path / "state.db")
    _seed_bad_alias(conn)

    rows = store.forget_question(conn, "Country*")
    assert sorted(r["company"] for r in rows) == ["Asana", "Twilio"]
    assert store.known_question_keys(conn)["country"] == "location"
    conn.close()


def test_forgetting_a_question_clears_the_alias_the_gap_and_the_plans(tmp_path):
    """Three things move together, and leaving any of them behind only looks fixed.

    `known_question_keys` replays every resolved label as a deterministic alias at every
    company, so "Country*" meant `location` everywhere with no model call — but a stored
    plan also beats a fresh `resolve_field` in `browser._plan_index`, so a plan built
    with the bad answer keeps carrying it until it is rebuilt.
    """
    conn = store.connect(tmp_path / "state.db")
    _seed_bad_alias(conn)

    store.forget_question(conn, "Country*", write=True)

    assert store.known_question_keys(conn) == {}
    assert [g["question_key"] for g in store.open_gaps(conn)] == ["country"]
    plan = conn.execute("SELECT answers_hash FROM prefill_plans").fetchone()
    assert plan["answers_hash"] == "", "the plan still holds the value it was told"
    conn.close()


def test_a_dom_reading_does_not_erase_options_the_ats_published(tmp_path):
    """A combobox never carries its own options; Greenhouse's API publishes all of them.

    Overwriting with NULL meant one browser visit erased the vocabulary, and the field
    went back to being a text box with nothing for `match_option` to check against. Same
    rule as `sync_postings` and `posted_on`: a pass that does not know a value must not
    erase one that does.
    """
    import json as _json

    conn = store.connect(tmp_path / "state.db")
    store.upsert_form_field(
        conn, company="Twilio", form_key="question_65614029", label="Work auth?",
        field_type="select", now="2026-08-20", required=True,
        options=_json.dumps(["Yes", "No"]), source="greenhouse-api",
    )
    store.upsert_form_field(
        conn, company="Twilio", form_key="question_65614029", label="Work auth?",
        field_type="combobox", now="2026-08-23", required=True,
        options=None, source="dom",
    )

    assert store.known_options(conn, "Twilio") == {"question_65614029": ["Yes", "No"]}
    # The rest of the row is still overwritten — only the absence is refused.
    row = conn.execute("SELECT type, source FROM form_fields").fetchone()
    assert (row["type"], row["source"]) == ("combobox", "dom")
    conn.close()


# -- dedupe: one identity for one req, however it arrived ---------------------------
def _dp(company, jid, url, check_method="api"):
    """A posting plus the check_method its company would carry in companies.yaml."""
    return Posting(company, jid, "Software Engineer", url, "NYC")


def test_an_existing_database_gains_the_dedupe_index_without_a_rebuild(tmp_path):
    """The index is applied AFTER the column migrations, and that ordering is the test.

    `connect` runs `executescript(_SCHEMA)` first, so an index declared there would
    raise "no such column: dedupe_key" against any database that predates the column —
    and would pass against every freshly-built one, which is every other database in
    this suite. The bug would be invisible until it reached the only database that
    matters. So: build a connection, drop the column back off, reconnect.
    """
    db = tmp_path / "old.db"
    conn = store.connect(db)
    store.sync_postings(conn, "Acme", [_p("1")], "2026-07-01")
    conn.commit()
    conn.execute("DROP INDEX IF EXISTS idx_postings_dedupe_key")
    conn.execute("ALTER TABLE postings DROP COLUMN dedupe_key")
    conn.commit()
    conn.close()

    reopened = store.connect(db)  # must not raise
    cols = {r["name"] for r in reopened.execute("PRAGMA table_info(postings)")}
    assert "dedupe_key" in cols
    indexes = {
        r[0] for r in reopened.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_postings_dedupe_key" in indexes
    assert reopened.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1


def test_an_api_row_keys_on_curated_identity_not_on_whatever_url_its_board_returned():
    """25 of 45 Greenhouse boards return a careers-site absolute_url. Keyed off that,
    the row would sit where no feed link could reach it."""
    conn = _conn()
    store.sync_postings(
        conn, "Stripe",
        [Posting("Stripe", "4567", "SWE", "https://stripe.com/jobs/search?gh_jid=4567")],
        "2026-07-01", identity=("greenhouse", "stripe"),
    )
    key = conn.execute("SELECT dedupe_key FROM postings").fetchone()[0]
    assert key == "greenhouse:stripe:4567"


def test_a_feed_row_with_no_identity_keys_on_its_url():
    conn = _conn()
    store.sync_postings(
        conn, "Simplify",
        [Posting("Simplify", "abc", "X — SWE", "https://jobs.lever.co/artera-2/eae")],
        "2026-07-01",
    )
    assert conn.execute("SELECT dedupe_key FROM postings").fetchone()[0] == \
        "lever:artera-2:eae"


def test_a_url_that_moves_takes_its_key_with_it():
    """Plain assignment, not COALESCE like posted_on next door: a Greenhouse board
    migrating its absolute_url to a careers page is a documented, observed event."""
    conn = _conn()
    store.sync_postings(
        conn, "Acme", [Posting("Acme", "1", "SWE", "https://a.example/jobs/1")], "2026-07-01"
    )
    store.sync_postings(
        conn, "Acme", [Posting("Acme", "1", "SWE", "https://b.example/jobs/1")], "2026-07-02"
    )
    assert conn.execute("SELECT dedupe_key FROM postings").fetchone()[0] == \
        "url:b.example/jobs/1"


def test_a_feed_row_that_mirrors_a_board_row_is_kept_open_beside_it():
    """The old behaviour closed the feed row. Both pages now show their own copy, so the
    only thing the shared key does here is exist — it is read at render time, to say "on
    company page" on one row and "applied" on the other."""
    conn = _conn()
    url = "https://jobs.lever.co/acme/xyz"
    store.sync_postings(conn, "Stripe", [Posting("Stripe", "xyz", "SWE", url)], "2026-07-01",
                        identity=("lever", "acme"))
    store.sync_postings(conn, "Simplify", [Posting("Simplify", "2", "SWE", url)], "2026-07-01")

    rows = list(conn.execute(
        "SELECT company, closed_at, closed_reason, dedupe_key FROM postings ORDER BY company"))
    assert [r["closed_at"] for r in rows] == [None, None]
    assert [r["closed_reason"] for r in rows] == [None, None]
    assert len({r["dedupe_key"] for r in rows}) == 1


def test_duplicate_closures_from_the_old_behaviour_are_reopened_once():
    """A migration, and it has to be one: those rows were closed by a rule that no longer
    exists, and nothing else would ever reopen them — `sync_postings` only reopens a
    closure that came from absence, which is exactly what this was not."""
    conn = _conn()
    url = "https://jobs.lever.co/acme/xyz"
    store.sync_postings(conn, "Simplify", [Posting("Simplify", "2", "SWE", url)], "2026-07-01")
    conn.execute(
        "UPDATE postings SET closed_at='2026-07-01', closed_reason='duplicate', "
        "duplicate_of_url=?", (url,)
    )

    assert store._apply_data_migrations(conn) >= 1

    row = conn.execute("SELECT * FROM postings").fetchone()
    assert row["closed_at"] is None
    assert row["closed_reason"] is None and row["duplicate_of_url"] is None
    assert store._apply_data_migrations(conn) == 0  # self-draining


def test_a_posting_closed_by_absence_still_reopens_when_it_comes_back():
    """The guard above must not break the original behaviour: closure by absence has a
    NULL reason and is exactly the kind that should reopen."""
    conn = _conn()
    store.sync_postings(conn, "Acme", [_p("1")], "2026-07-01")
    store.sync_postings(conn, "Acme", [], "2026-07-02")
    store.sync_postings(conn, "Acme", [_p("1")], "2026-07-03")
    row = conn.execute("SELECT closed_at, closed_reason FROM postings").fetchone()
    assert row["closed_at"] is None and row["closed_reason"] is None


def test_two_board_rows_sharing_a_key_are_reported_and_neither_is_touched():
    conn = _conn()
    url = "https://acme.example/careers/search"
    store.sync_postings(conn, "Acme", [Posting("Acme", "1", "A", url),
                                       Posting("Acme", "2", "B", url)], "2026-07-01")
    conflicts = store.board_key_conflicts(conn, {"Acme": "api"})
    assert len(conflicts) == 1 and len(conflicts[0]) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM postings WHERE closed_at IS NULL"
    ).fetchone()[0] == 2


def test_a_job_boards_rows_are_known_by_origin_not_by_the_companies_map():
    """A plugin group is deliberately never in companies.yaml, so looking one up there
    returns "unknown" — and unknown is not a feed, by the rule above. The first real run
    showed the cost: a listings board whose employers link to one careers page had seven
    live reqs on one key, and every night would have reported them at WARNING as though
    two curated boards had collided. `origin` is the stored answer to that question.
    """
    conn = _conn()
    url = "https://acme.example/careers/search"
    store.append_postings(
        conn, "Simplify New-Grad-Positions",
        [Posting("Simplify New-Grad-Positions", "a", "A — SWE", url),
         Posting("Simplify New-Grad-Positions", "b", "B — SWE", url)],
        "2026-09-15", origin="simplify",
    )
    # The map has no entry for the group, exactly as `cmd_check` builds it.
    assert store.board_key_conflicts(conn, {"Acme": "api"}) == []


def test_a_board_row_and_a_feed_row_on_one_key_are_not_reported():
    """The ordinary case, and the one the two pages are built around."""
    conn = _conn()
    url = "https://jobs.lever.co/acme/xyz"
    store.sync_postings(conn, "Stripe", [Posting("Stripe", "xyz", "SWE", url)], "2026-07-01",
                        identity=("lever", "acme"))
    store.sync_postings(conn, "Simplify", [Posting("Simplify", "2", "SWE", url)], "2026-07-01")
    assert store.board_key_conflicts(
        conn, {"Stripe": "api", "Simplify": "aggregator"}) == []


def test_the_backfill_is_self_draining_and_a_second_run_is_a_no_op():
    conn = _conn()
    store.sync_postings(conn, "Acme", [_p("1"), _p("2")], "2026-07-01")
    conn.execute("UPDATE postings SET dedupe_key=NULL")
    assert store.backfill_dedupe_key(conn) == 2
    assert store.backfill_dedupe_key(conn) == 0


def test_the_backfill_prefers_curated_identity_when_it_has_it():
    conn = _conn()
    store.sync_postings(
        conn, "Stripe",
        [Posting("Stripe", "4567", "SWE", "https://stripe.com/jobs/search")], "2026-07-01"
    )
    conn.execute("UPDATE postings SET dedupe_key=NULL")
    store.backfill_dedupe_key(conn, {"Stripe": ("greenhouse", "stripe")})
    assert conn.execute("SELECT dedupe_key FROM postings").fetchone()[0] == \
        "greenhouse:stripe:4567"


def test_a_company_nobody_curates_is_reported_rather_than_read_as_a_feed():
    """Dropping an entry from companies.yaml must not quietly reclassify its rows.

    Found by running the real pass against a deliberately empty companies.yaml: every
    company lost both its identity key and its rank, and 795 Databricks postings sharing
    one careers URL closed each other. Nothing closes now, but the same misreading would
    hide a genuine collision between two live reqs."""
    conn = _conn()
    url = "https://jobs.lever.co/acme/xyz"
    store.sync_postings(conn, "Tracked", [Posting("Tracked", "xyz", "SWE", url)], "2026-07-01",
                        identity=("lever", "acme"))
    store.sync_postings(conn, "Forgotten", [Posting("Forgotten", "2", "SWE", url)], "2026-07-01")
    conflicts = store.board_key_conflicts(conn, {"Tracked": "api"})
    assert len(conflicts) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM postings WHERE closed_at IS NULL"
    ).fetchone()[0] == 2


def test_a_board_that_links_every_req_to_one_careers_page_does_not_collapse():
    """The live shape this protects: Databricks, Stripe and MongoDB all do exactly this,
    covering 2,805 open postings between them and ten other boards."""
    conn = _conn()
    postings = [
        Posting("Betterment", str(i), f"Role {i}",
                f"https://www.betterment.com/careers/current-openings/job?gh_jid={i}")
        for i in (7184616, 7187115, 7220419)
    ]
    store.sync_postings(conn, "Betterment", postings, "2026-07-01")
    keys = {r[0] for r in conn.execute("SELECT dedupe_key FROM postings")}
    assert len(keys) == 3
    assert store.board_key_conflicts(conn, {"Betterment": "api"}) == []


# -- two pages: where a row came from, and whether you already applied ---------------
def test_a_legacy_feed_row_learns_its_origin_on_the_next_connect():
    """`origin` is the split between the two postings pages, and every row that predates
    it was identified by its group name — the coupling the column exists to remove. The
    backfill holds the last copy of that knowledge, so it has to actually fire.
    """
    conn = _conn()
    store.append_postings(
        conn, "Simplify New-Grad-Positions",
        [Posting("Simplify New-Grad-Positions", "a", "X — SWE", "https://x/a")],
        "2026-09-01",
    )
    store.append_postings(
        conn, "Discord: #jobs",
        [Posting("Discord: #jobs", "b", "Y — SWE", "https://y/b")], "2026-09-01",
    )
    store.sync_postings(conn, "Acme", [_p("1")], "2026-09-01")
    conn.execute("UPDATE postings SET origin=NULL")  # the world before the column

    store._apply_data_migrations(conn)

    got = {r["company"]: r["origin"]
           for r in conn.execute("SELECT company, origin FROM postings")}
    assert got["Simplify New-Grad-Positions"] == "simplify"
    assert got["Discord: #jobs"] == "discord"
    assert got["Acme"] is None  # a curated board is the absence, never a value


def test_a_feed_row_records_the_board_that_imported_it_and_the_employer_it_named():
    conn = _conn()
    store.append_postings(
        conn, "Simplify New-Grad-Positions",
        [Posting("Simplify New-Grad-Positions", "a", "Klaviyo — Software Engineer 1",
                 "https://x/a", employer="Klaviyo")],
        "2026-09-01", origin="simplify",
    )
    row = conn.execute("SELECT origin, employer FROM postings").fetchone()
    assert (row["origin"], row["employer"]) == ("simplify", "Klaviyo")


def test_a_later_read_fills_in_what_an_earlier_one_could_not_say_and_blanks_nothing():
    """Rows imported before either column existed are re-seen every night, and COALESCE
    is what lets that fill them in — while a feed that names no employer, like Discord's
    generic format, never blanks one that another read supplied."""
    conn = _conn()
    bare = Posting("Simplify New-Grad-Positions", "a", "Klaviyo — SWE", "https://x/a")
    store.append_postings(conn, "Simplify New-Grad-Positions", [bare], "2026-09-01")

    named = Posting("Simplify New-Grad-Positions", "a", "Klaviyo — SWE", "https://x/a",
                    employer="Klaviyo")
    store.append_postings(conn, "Simplify New-Grad-Positions", [named], "2026-09-02",
                          origin="simplify")
    row = conn.execute("SELECT origin, employer FROM postings").fetchone()
    assert (row["origin"], row["employer"]) == ("simplify", "Klaviyo")

    store.append_postings(conn, "Simplify New-Grad-Positions", [bare], "2026-09-03")
    row = conn.execute("SELECT origin, employer FROM postings").fetchone()
    assert (row["origin"], row["employer"]) == ("simplify", "Klaviyo")


def test_an_application_carries_the_key_of_the_link_you_applied_at():
    """"Have I applied to this?" is a question about a req, not about one row: the same
    job arrives from a board and from two feeds, and you apply to it once."""
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-09-01",
                             url="https://jobs.lever.co/artera-2/eae")
    assert conn.execute("SELECT dedupe_key FROM applications").fetchone()[0] == \
        "lever:artera-2:eae"


def test_the_application_key_follows_its_url_including_being_cleared():
    """Same rule as `url` itself — None leaves it alone, "" clears it. A key outliving
    the URL it came from would keep flagging rows as applied on a link you removed."""
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-09-01",
                             url="https://jobs.lever.co/artera-2/eae")
    store.record_application(conn, "Acme", "1", "SWE", "interview", "2026-09-02")
    assert conn.execute("SELECT dedupe_key FROM applications").fetchone()[0] == \
        "lever:artera-2:eae"

    store.record_application(conn, "Acme", "1", "SWE", "interview", "2026-09-03", url="")
    assert conn.execute("SELECT dedupe_key FROM applications").fetchone()[0] == ""


def test_a_url_no_key_can_be_derived_from_stores_an_empty_key_not_a_stale_one():
    conn = _conn()
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-09-01",
                             url="https://jobs.lever.co/artera-2/eae")
    store.record_application(conn, "Acme", "1", "SWE", "applied", "2026-09-02",
                             url="mailto:jobs@acme.example")
    assert conn.execute("SELECT dedupe_key FROM applications").fetchone()[0] == ""


def test_the_application_key_is_indexed_because_every_rendered_row_asks_it():
    conn = _conn()
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_applications_dedupe_key" in indexes


def test_all_applications_carries_a_postings_employer_and_keeps_manual_rows(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, url, first_seen, last_seen, "
        "employer) VALUES ('Feed', '1', 'Airbnb — SWE', 'https://x/1', '2026-08-01', "
        "'2026-08-01', 'Airbnb')")
    store.record_application(conn, "Feed", "1", "Airbnb — SWE", "applied", "2026-08-02")
    store.record_application(conn, "Hand", "manual:x", "SWE", "applied", "2026-08-03")
    rows = {r["company"]: r for r in store.all_applications(conn)}
    assert rows["Feed"]["employer"] == "Airbnb"
    assert rows["Hand"]["employer"] is None
    assert rows["Hand"]["source"] == "tracked"
    conn.close()
