"""The mirrored form: the state, the vocabulary, and the epoch.

Everything here runs with no browser and no socket, which is the point of `live.py`
being pure. The half that needs a real browser is in `test_browser.py`.
"""

import queue

from jobtracker import live
from jobtracker.models import FormField


def _found(*handles):
    return [{"handle": h} for h in handles]


def _fields(*specs):
    return [FormField(key=k, label=lab, type=t) for k, lab, t in specs]


def _session(*specs, handles=None):
    session = live.start("Acme", "1", "Backend Engineer", "https://x/apply")
    handles = handles or [f"jt{i}" for i in range(len(specs))]
    session.absorb(live.rows_from(_found(*handles), _fields(*specs)))
    session.set_phase(live.READY)
    return session


# -- the invariant that matters most ---------------------------------------------------
def test_the_command_vocabulary_cannot_activate_anything():
    """A web request may make the browser do exactly five things, none of them a click.

    This is `browser.py`'s no-submit rule carried across the new channel. That rule is
    enforced against the module's own source, which says nothing about commands arriving
    from outside it — so the bound has to be restated here, on the vocabulary itself.

    `clear` is in and `submit` is not, and the line between them is what this asserts:
    emptying a field is the inverse of filling one and reaches nothing a fill does not,
    whereas submitting activates a control and is therefore a session-level act with its
    own gate, not a name a queued request can reach.
    """
    assert live.VOCABULARY == {"set", "clear", "rediscover", "shoot", "highlight"}
    assert "submit" not in live.VOCABULARY

    # And a command carries a handle and a value — never a selector, never an
    # expression, never anything the browser thread would evaluate.
    assert set(live.Command.__dataclass_fields__) == {"kind", "handle", "value", "epoch"}


def test_a_command_outside_the_vocabulary_is_refused():
    session = _session(("first_name", "First Name", "text"))
    assert session.submit(live.Command(kind="click", handle="jt0")) is False
    assert session.submit(live.Command(kind="evaluate", value="alert(1)")) is False
    assert session.commands.empty()

    assert session.submit(live.Command(kind=live.SET, handle="jt0", value="x")) is True


def test_nothing_queues_once_the_window_is_gone():
    """A closed session accepting writes is a page that looks like it still works."""
    session = _session(("first_name", "First Name", "text"))
    session.set_phase(live.CLOSED)
    assert session.submit(live.Command(kind=live.SET, handle="jt0", value="x")) is False


# -- the epoch -------------------------------------------------------------------------
def test_reading_the_same_form_again_does_not_move_the_handles():
    """The common case, and the reason the epoch is not bumped on every write.

    A successful write re-reads the form. If that always invalidated the handles, the
    second field you typed would be refused because the first one succeeded — every
    edit poisoning the next.
    """
    session = _session(("first_name", "First Name", "text"),
                       ("email", "Email", "text"))
    before = session.epoch

    moved = session.absorb(live.rows_from(
        _found("jt0", "jt1"),
        _fields(("first_name", "First Name", "text"), ("email", "Email", "text")),
        session.carried(),
    ))
    assert moved is False
    assert session.epoch == before


def test_a_question_appearing_mid_form_moves_every_handle_after_it():
    """The case the epoch exists for.

    `_DISCOVER_JS` numbers as it walks the DOM, so a question revealed by an answer
    shifts every handle below it by one. `jt1` named Email a moment ago and names the
    new question now — and a queued write for `jt1` would land on the wrong field.
    """
    session = _session(("first_name", "First Name", "text"),
                       ("email", "Email", "text"))
    before = session.epoch

    moved = session.absorb(live.rows_from(
        _found("jt0", "jt1", "jt2"),
        _fields(("first_name", "First Name", "text"),
                ("sponsorship", "Will you need sponsorship?", "select"),
                ("email", "Email", "text")),
        session.carried(),
    ))
    assert moved is True
    assert session.epoch == before + 1


def test_an_answer_already_given_survives_the_form_changing_shape():
    """Carried by key, not by handle — which is exactly why the handle moving is safe."""
    session = _session(("first_name", "First Name", "text"),
                       ("email", "Email", "text"))
    session.mark("jt0", live.FILLED, "Dylan")

    session.absorb(live.rows_from(
        _found("jt0", "jt1", "jt2"),
        _fields(("sponsorship", "Will you need sponsorship?", "select"),
                ("first_name", "First Name", "text"),
                ("email", "Email", "text")),
        session.carried(),
    ))
    by_key = {r["key"]: r for r in session.snapshot()["fields"]}
    # It moved from jt0 to jt1 and kept its answer.
    assert by_key["first_name"]["handle"] == "jt1"
    assert by_key["first_name"]["value"] == "Dylan"
    assert by_key["first_name"]["status"] == live.FILLED
    # And the new question arrives as something you have not answered, not as blank-filled.
    assert by_key["sponsorship"]["status"] == live.PENDING


# -- watching --------------------------------------------------------------------------
def test_nobody_watching_means_no_screenshots():
    """The pause button, and the closed-tab case, are the same mechanism.

    Taking JPEGs of a browser nobody is looking at is pure waste on a machine that is
    also running the nightly pipeline.
    """
    session = _session(("first_name", "First Name", "text"))
    assert session.watching(now=100.0) is False

    session.watch(now=100.0)
    assert session.watching(now=101.0) is True
    assert session.watching(now=100.0 + live.WATCH_WINDOW_S + 1) is False


def test_the_shot_cadence_is_measured_from_the_last_shot():
    session = _session(("first_name", "First Name", "text"))
    assert session.due_for_shot(2.0, now=1000.0) is True
    session.store_shot(b"jpeg", now=1000.0)
    assert session.due_for_shot(2.0, now=1001.0) is False
    assert session.due_for_shot(2.0, now=1002.5) is True


# -- what the page is told -------------------------------------------------------------
def test_zero_fields_is_reported_as_no_form_not_as_nothing_left_to_do():
    """Absence read as success is the failure this project exists to avoid.

    A form that had not rendered, a page that only links to the real application, and a
    login wall all produce zero fields. "0/0 · nothing left to type" would send you off
    to do nothing about any of them.
    """
    session = live.start("Acme", "1", "Backend Engineer", "https://x/apply")
    snap = session.snapshot()
    assert snap["discovered"] == 0
    assert live.summary(snap) == "no application form found on https://x/apply"
    assert "nothing left to type" not in live.summary(snap)


def test_the_summary_counts_only_what_still_needs_you():
    session = _session(("a", "A", "text"), ("b", "B", "text"), ("c", "C", "select"))
    session.mark("jt0", live.FILLED, "x")
    session.mark("jt1", live.GAP, "")
    session.mark("jt2", live.REFUSED, "")
    snap = session.snapshot()
    assert live.summary(snap) == "1/3 fields filled · 2 need you"


def test_a_field_you_emptied_is_counted_as_needing_you():
    """Clearing is not filling with nothing, and the count is where the difference shows.

    An empty value written as `filled` reads as done and drops out of "need you" — the
    reading `answers.py` refuses for the identical reason ("an empty answer is
    indistinguishable from a missing one"), here on a form about to be submitted.
    """
    session = _session(("a", "A", "text"), ("b", "B", "text"))
    session.mark("jt0", live.FILLED, "x")
    session.mark("jt1", live.FILLED, "y")
    assert live.summary(session.snapshot()) == "2/2 fields filled · nothing left to type"

    session.mark("jt1", live.CLEARED, "")
    snap = session.snapshot()
    assert snap["need"] == 1
    assert live.summary(snap) == "1/2 fields filled · 1 need you"


def test_a_snapshot_carries_no_bytes_and_no_locks():
    """It becomes JSON, and it is built while holding the lock the browser thread wants."""
    session = _session(("a", "A", "text"))
    session.store_shot(b"\xff\xd8jpeg")
    snap = session.snapshot()
    assert snap["has_shot"] is True
    assert b"\xff\xd8jpeg" not in repr(snap).encode()
    import json

    json.dumps(snap)  # raises if anything unserializable leaked in


def test_a_snapshot_is_a_copy_so_the_page_cannot_edit_the_session():
    session = _session(("a", "A", "text"))
    snap = session.snapshot()
    snap["fields"][0]["value"] = "tampered"
    assert session.snapshot()["fields"][0]["value"] == ""


def test_the_queue_is_the_only_channel():
    """No method on a session touches a page, which is what keeps it thread-safe."""
    session = _session(("a", "A", "text"))
    assert isinstance(session.commands, queue.Queue)
    session.submit(live.Command(kind=live.SET, handle="jt0", value="x", epoch=1))
    assert session.commands.get_nowait().value == "x"


# -- ending the session ----------------------------------------------------------------
def test_closing_is_asked_for_outside_the_vocabulary_and_survives_a_closed_session():
    """The vocabulary is what a request may do to the *form*. This ends the session.

    It also has to work when nothing is being accepted any more — a session part-way
    through filling, or one whose phase has already moved on, is exactly when you want
    the window gone. Routing it through `submit` would mean the one action that frees the
    one-window lock is refused in the states that most often hold it.
    """
    session = _session(("first_name", "First Name", "text"))
    assert session.close_requested() is False
    assert "close" not in live.VOCABULARY

    session.set_phase(live.CLOSED)
    assert session.submit(live.Command(kind=live.SET, handle="jt0", value="x")) is False
    session.request_close()
    assert session.close_requested() is True
    assert session.snapshot()["closing"] is True


def test_the_session_names_the_page_the_browser_actually_opened():
    """It is created from the posting's URL — the page has to render before the fill
    starts — and the browser then goes somewhere else entirely: Greenhouse is redirected
    at the form, which is not even the same host. *"No application form found on …"* is
    the sentence that sends you to look, so it has to name the page that was looked at."""
    session = _session(("first_name", "First Name", "text"))
    session.retarget("https://job-boards.greenhouse.io/embed/job_app?for=a&token=1")
    snap = session.snapshot()
    assert snap["url"] == "https://job-boards.greenhouse.io/embed/job_app?for=a&token=1"
    assert live.summary({**snap, "discovered": 0}).endswith(snap["url"])
