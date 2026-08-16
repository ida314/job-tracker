"""The derived half of the outer loop: staleness, urgency, grouping, response rate.

Everything here is arithmetic over rows, so these run against plain dicts rather than a
database — the same reason `match.py` is tested without one.
"""

from jobtracker import applications as A


def _app(**kw):
    row = {
        "company": "Acme", "ats_job_id": "1", "title": "SWE", "status": "applied",
        "note": "", "applied_at": "2026-08-01T09:00:00",
        "updated_at": "2026-08-01T09:00:00", "url": None, "location": None,
        "source": "tracked", "next_action": None, "next_action_note": None,
    }
    row.update(kw)
    return row


def _event(status, at, note=""):
    return {"company": "Acme", "ats_job_id": "1", "status": status, "note": note,
            "at": at}


TODAY = "2026-08-16"


# -- dates ---------------------------------------------------------------------------
def test_day_of_handles_both_stored_shapes():
    """applied_at/updated_at are full timestamps, next_action is a plain day.
    date.fromisoformat rejects the former outright, and the failure is silent — the row
    simply reports no age at all."""
    assert A.day_of("2026-08-01T09:00:00") == "2026-08-01"
    assert A.day_of("2026-08-01") == "2026-08-01"
    assert A.day_of(None) is None
    assert A.day_of("") is None


def test_days_since_reads_a_timestamp_not_just_a_day():
    assert A.days_since("2026-08-01T09:00:00", TODAY) == 15
    assert A.days_since("2026-08-16T23:59:59", TODAY) == 0


def test_an_unreadable_date_is_unknown_and_never_zero():
    """None means 'cannot tell'. Returning 0 would render a corrupt row as 'touched
    today', which is the one reading that hides it."""
    assert A.days_since("not a date", TODAY) is None
    assert A.days_since(None, TODAY) is None
    assert A.days_until("nonsense", TODAY) is None


def test_parse_day_refuses_anything_it_cannot_store():
    assert A.parse_day("2026-08-20") == "2026-08-20"
    assert A.parse_day(" 2026-08-20 ") == "2026-08-20"
    assert A.parse_day("next tuesday") is None
    assert A.parse_day("08/20/2026") is None
    assert A.parse_day("") is None


# -- staleness ------------------------------------------------------------------------
def test_stale_is_derived_from_movement_not_from_a_status():
    quiet = _app(updated_at="2026-06-01T09:00:00")
    assert A.is_stale(quiet, TODAY) is True
    assert A.is_stale(_app(updated_at="2026-08-14T09:00:00"), TODAY) is False


def test_a_closed_application_is_never_stale():
    """There is nothing left to chase, so an old rejection must not sit in the section
    that means 'do something about this'."""
    old = _app(status="rejected", updated_at="2026-01-01T09:00:00")
    assert A.is_stale(old, TODAY) is False
    assert A.needs_action(old, TODAY) is False


# -- next action ----------------------------------------------------------------------
def test_action_state_bands():
    assert A.action_state(_app(next_action="2026-08-10"), TODAY) == "overdue"
    assert A.action_state(_app(next_action="2026-08-16"), TODAY) == "today"
    assert A.action_state(_app(next_action="2026-08-18"), TODAY) == "soon"
    assert A.action_state(_app(next_action="2026-09-30"), TODAY) == "later"


def test_no_next_action_is_not_overdue():
    """NULL is the normal state for most rows. Reading absence as urgency would put the
    entire tracker in the Needs action pile on day one."""
    assert A.action_state(_app(), TODAY) is None
    assert A.needs_action(_app(), TODAY) is False


def test_a_closed_application_has_no_pending_action():
    assert A.action_state(_app(status="offer", next_action="2026-08-01"), TODAY) is None


# -- rounds ---------------------------------------------------------------------------
def test_round_counts_drive_the_interview_badge():
    events = [_event("applied", "2026-08-01"), _event("oa", "2026-08-04"),
              _event("interview", "2026-08-11"), _event("interview", "2026-08-14"),
              _event("interview", "2026-08-15")]
    assert A.round_counts(events) == {"applied": 1, "oa": 1, "interview": 3}
    assert A.round_counts([]) == {}
    assert A.round_counts(None) == {}


# -- response rate --------------------------------------------------------------------
def test_a_rejection_after_interviews_still_counts_as_a_response():
    """Read from the log, not the current status: by the time it says `rejected`, the
    status alone no longer records that anyone ever replied."""
    app = _app(status="rejected")
    events = [_event("applied", "2026-08-01"), _event("interview", "2026-08-10"),
              _event("rejected", "2026-08-15")]
    assert A.has_responded(app, events) is True


def test_silence_is_not_a_response():
    assert A.has_responded(_app(), [_event("applied", "2026-08-01")]) is False


def test_withdrawing_is_not_a_response():
    """You caused it. Counting it would let giving up look like traction."""
    app = _app(status="withdrawn")
    assert A.has_responded(app, [_event("applied", "2026-08-01"),
                                 _event("withdrawn", "2026-08-09")]) is False


def test_an_application_entered_at_a_later_stage_counts_without_a_log():
    """Typed in by hand as 'screen' — there is no earlier history to read."""
    assert A.has_responded(_app(status="screen"), []) is True


def test_summary_of_an_empty_tracker_does_not_divide_by_zero():
    assert A.summary([], {})["response_rate"] == 0


def test_summary_counts():
    apps = [_app(ats_job_id="1", status="applied"),
            _app(ats_job_id="2", status="interview"),
            _app(ats_job_id="3", status="offer"),
            _app(ats_job_id="4", status="rejected")]
    events = {("Acme", "2"): [_event("interview", "2026-08-10")],
              ("Acme", "3"): [_event("offer", "2026-08-10")],
              ("Acme", "4"): [_event("rejected", "2026-08-10")]}
    got = A.summary(apps, events)
    assert got["total"] == 4
    assert got["active"] == 2          # applied + interview
    assert got["interviewing"] == 1
    assert got["offers"] == 1
    assert got["responded"] == 3
    assert got["response_rate"] == 75


# -- grouping -------------------------------------------------------------------------
def test_group_splits_and_orders_by_urgency():
    overdue = _app(ats_job_id="a", next_action="2026-08-10")
    soon = _app(ats_job_id="b", next_action="2026-08-17")
    quiet = _app(ats_job_id="c", updated_at="2026-05-01T09:00:00")
    fine = _app(ats_job_id="d", updated_at="2026-08-15T09:00:00")
    done = _app(ats_job_id="e", status="offer")
    got = A.group([fine, done, soon, quiet, overdue], {}, TODAY)

    # Due dates first, soonest first; the stalled one trails them but is still urgent.
    assert [a["ats_job_id"] for a in got["needs_action"]] == ["a", "b", "c"]
    assert [a["ats_job_id"] for a in got["active"]] == ["d"]
    assert [a["ats_job_id"] for a in got["closed"]] == ["e"]


def test_active_rows_sort_most_stalled_first():
    a = _app(ats_job_id="a", updated_at="2026-08-15T09:00:00")
    b = _app(ats_job_id="b", updated_at="2026-08-02T09:00:00")
    c = _app(ats_job_id="c", updated_at="2026-08-10T09:00:00")
    got = A.group([a, b, c], {}, TODAY)
    assert [x["ats_job_id"] for x in got["active"]] == ["b", "c", "a"]


def test_an_unreadable_updated_at_sorts_last_not_first():
    """A bad timestamp must not masquerade as the most urgent thing on the page."""
    bad = _app(ats_job_id="bad", updated_at="garbage")
    ok = _app(ats_job_id="ok", updated_at="2026-08-10T09:00:00")
    got = A.group([bad, ok], {}, TODAY)
    assert [x["ats_job_id"] for x in got["active"]] == ["ok", "bad"]
