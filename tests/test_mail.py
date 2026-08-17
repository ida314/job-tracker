"""Reading the mailbox: parsing, narrowing, the task, and accepting a proposal.

Two rules carry most of the weight here and both are tested from more than one angle:
nothing may write to the maildir, and nothing but an explicit accept may write to
`applications`. Everything else — a name that matches half a word, a message with no
Message-ID, a quote the model invented — is a way for a wrong entry to end up in a history
you rely on.

The pure half needs no mailbox at all: `parse` takes bytes and `narrow` takes dicts, so
most of this file never touches the disk.
"""

import asyncio
import json
import mailbox
import os

from jobtracker import mail, maildir, store
from jobtracker.tasks import TaskContext, get_task
from jobtracker.tasks.inbox import parse_reading


def _msg(subject="Your application", frm="Recruiting <no-reply@greenhouse.io>",
         body="Thanks for applying.", date="Fri, 15 Aug 2026 10:04:00 -0400",
         mid="<abc@example>"):
    head = [f"From: {frm}", f"Subject: {subject}", f"Date: {date}"]
    if mid:
        head.append(f"Message-ID: {mid}")
    return ("\r\n".join(head) + "\r\n\r\n" + body).encode()


def _app(company="Stripe", job="7966029", title="Backend Engineer, New Grad",
         url="https://boards.greenhouse.io/stripe/jobs/7966029", status="applied",
         applied_at="2026-08-01T09:00:00", updated_at="2026-08-01T09:00:00"):
    return {"company": company, "ats_job_id": job, "title": title, "url": url,
            "status": status, "applied_at": applied_at, "updated_at": updated_at}


def _index(*apps):
    return mail.build_index(list(apps) or [_app()])


# -- parsing ------------------------------------------------------------------------
def test_a_message_with_no_message_id_gets_a_stable_synthetic_one():
    """Absent must not mean "new every night". The maildir filename cannot stand in —
    a client renames it the moment you open the message."""
    raw = _msg(mid=None)
    first, second = mail.parse(raw), mail.parse(raw)
    assert first.message_id.startswith("synth:")
    assert first.message_id == second.message_id


def test_an_unparseable_date_is_admitted_rather_than_dropped():
    """Dropping mail over a bad header is failure-as-absence in the one feature whose job
    is to catch what you missed."""
    message = mail.parse(_msg(date="not a date at all"))
    assert message is not None
    assert message.sent_on is None
    assert message.sent_at == "not a date at all"


def test_html_only_mail_is_stripped_before_anything_reads_it():
    raw = (b"From: a@b.co\r\nSubject: s\r\nMessage-ID: <x>\r\n"
           b"Content-Type: text/html; charset=utf-8\r\n\r\n"
           b"<html><body><p>We would like to <b>schedule</b> a call.</p></body></html>")
    message = mail.parse(raw)
    assert "<p>" not in message.body
    assert "schedule" in message.body


def test_garbage_never_raises_out_of_the_parser():
    """A scan that dies on message 3,000 of 5,000 has silently reported the other 2,000
    as the whole story. Whatever comes back, it must come back."""
    for raw in (b"", b"\xff\xfe\x00not mail at all", b"From: \r\n\r\n",
                b"Subject: " + b"x" * 5000):
        parsed = mail.parse(raw)          # the assertion is that this returns at all
        assert parsed is None or isinstance(parsed.message_id, str)


def test_a_file_with_no_headers_at_all_is_not_a_message():
    """`email` is tolerant to a fault: it turns any byte string into a header-less
    Message rather than raising. Accepting that would mean the scan can never report
    itself degraded, because nothing would ever fail to parse."""
    assert mail.parse(b"garbage, not a message") is None
    assert mail.parse(b"") is None
    # One real header is enough to be worth reading.
    assert mail.parse(b"Subject: hello\r\n\r\nbody") is not None


# -- narrowing ----------------------------------------------------------------------
def test_a_message_is_never_a_candidate_for_a_company_you_never_applied_to():
    """Structural, not a rule: every index is built from `applications`, so there is
    nothing for an unknown company to match against."""
    idx = _index(_app(company="Stripe"))
    message = mail.parse(_msg(frm="Ramp Recruiting <careers@ramp.com>",
                              subject="Your application to Ramp"))
    assert mail.narrow(message, idx) is None


def test_an_ats_relay_domain_identifies_the_ats_and_never_the_company():
    """`no-reply@greenhouse.io` proves a hosted board sent it and nothing about whose."""
    idx = _index(_app())
    named = mail.parse(_msg(frm="Stripe Recruiting <no-reply@greenhouse.io>"))
    assert mail.narrow(named, idx).company == "Stripe"

    bare = mail.parse(_msg(frm="<no-reply@greenhouse.io>", subject="An update",
                           body="hello"))
    assert mail.narrow(bare, idx) is None


def test_a_company_name_in_the_body_alone_is_not_a_match():
    """Every newsletter in the world mentions Stripe."""
    idx = _index(_app())
    message = mail.parse(_msg(
        frm="Weekly Digest <news@example.org>", subject="This week in tech",
        body="Stripe raised a round and is hiring for a position or two."))
    assert mail.narrow(message, idx) is None


def test_a_digest_naming_your_employer_in_the_subject_is_not_application_mail():
    idx = _index(_app())
    message = mail.parse(_msg(
        frm="Weekly Digest <news@example.org>",
        subject="This week in tech: Stripe, Ramp and more",
        body="Stripe raised a round. Ramp shipped a thing."))
    assert mail.narrow(message, idx) is None


def test_a_name_matches_on_a_whole_token_so_ramp_is_not_rampart():
    idx = _index(_app(company="Ramp", job="abc123", url=""))
    message = mail.parse(_msg(frm="Rampart Security <sales@rampart.example>",
                              subject="Rampart pricing"))
    assert mail.narrow(message, idx) is None


def test_the_url_you_applied_at_resolves_the_job_and_not_just_the_company():
    idx = _index(_app(job="7966029"), _app(job="7966030", title="Platform Engineer",
                                           url="https://boards.greenhouse.io/stripe/jobs/7966030"))
    message = mail.parse(_msg(
        frm="Stripe <no-reply@greenhouse.io>",
        body="See https://boards.greenhouse.io/stripe/jobs/7966030 for details."))
    cand = mail.narrow(message, idx)
    assert (cand.company, cand.ats_job_id, cand.match_kind) == (
        "Stripe", "7966030", "job_url")


def test_two_applications_at_one_company_leave_the_job_unresolved_rather_than_guessing():
    """A wrong binding puts a stage on the wrong job. Asking costs one dropdown."""
    idx = _index(_app(job="7966029", url=""),
                 _app(job="7966030", title="Platform Engineer", url=""))
    message = mail.parse(_msg(frm="Stripe Recruiting <no-reply@greenhouse.io>"))
    cand = mail.narrow(message, idx)
    assert cand.ats_job_id == ""
    assert set(cand.choices) == {"7966029", "7966030"}


def test_a_sole_application_at_that_company_is_attached_without_a_model():
    idx = _index(_app(url=""))
    cand = mail.narrow(mail.parse(_msg(frm="Stripe Recruiting <x@greenhouse.io>")), idx)
    assert (cand.ats_job_id, cand.match_kind) == ("7966029", "sole_open")


def test_a_title_in_the_message_picks_the_right_one_of_several():
    idx = _index(_app(job="7966029", url=""),
                 _app(job="7966030", title="Platform Engineer", url=""))
    message = mail.parse(_msg(
        frm="Stripe Recruiting <x@greenhouse.io>",
        body="About your Platform Engineer application: we would like to talk."))
    assert mail.narrow(message, idx).ats_job_id == "7966030"


def test_a_message_that_predates_every_application_is_not_read():
    idx = _index(_app(applied_at="2026-08-01T09:00:00"))
    old = mail.parse(_msg(frm="Stripe Recruiting <x@greenhouse.io>",
                          date="Tue, 01 Jul 2026 10:00:00 -0400"))
    assert mail.narrow(old, idx) is None


# -- reading the maildir ------------------------------------------------------------
def _maildir(tmp_path, messages):
    box = mailbox.Maildir(str(tmp_path / "Mail"), create=True)
    for raw in messages:
        box.add(raw)
    return tmp_path / "Mail"


def _snapshot(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_nothing_in_the_mail_reader_can_write_to_the_maildir(tmp_path):
    """Two assertions, and both are needed. The source check states the intent; the
    snapshot is what still holds if the stdlib changes under us."""
    import inspect

    src = inspect.getsource(maildir)
    for forbidden in (".add(", ".remove(", ".discard(", ".flush(", ".lock(",
                      ".clean(", "create=True", "__setitem__"):
        assert forbidden not in src, forbidden

    path = _maildir(tmp_path, [_msg(), _msg(mid="<two@example>")])
    before = _snapshot(path)
    assert len(list(maildir.read_messages(path))) == 2
    assert _snapshot(path) == before


def test_a_typo_in_the_maildir_path_creates_nothing(tmp_path):
    """`mailbox.Maildir` defaults `create=True`, which would make directories inside
    someone's mail store on a mistyped environment variable."""
    missing = tmp_path / "not-here"
    assert not maildir.is_maildir(missing)
    try:
        list(maildir.read_messages(missing))
    except Exception:  # noqa: BLE001 — the point is that it did not create anything
        pass
    assert not missing.exists()


def test_the_maildir_filename_is_never_the_identity(tmp_path):
    """A client renames `1234.host` to `1234.host:2,S` when you read the message. Keyed
    on the filename, the whole inbox would be re-proposed every time you opened it."""
    raw = _msg()
    path = _maildir(tmp_path, [])
    (path / "new" / "1234.host").write_bytes(raw)
    (path / "cur" / "5678.host:2,S").write_bytes(raw)
    ids = {mail.parse(m.raw).message_id for m in maildir.read_messages(path) if m.ok}
    assert len(ids) == 1


# -- the store's idempotency --------------------------------------------------------
def _seed(conn, apps=(("Stripe", "7966029", "Backend Engineer, New Grad"),)):
    for company, job, title in apps:
        store.record_application(conn, company, job, title, "applied",
                                 "2026-08-01T09:00:00")
    conn.commit()


def _record(conn, message=None, company="Stripe", job="7966029", choices=("7966029",)):
    cand = mail.Candidate(
        message=message or mail.parse(_msg()), company=company, ats_job_id=job,
        choices=tuple(choices), match_kind="sole_open", evidence="stripe",
    )
    return store.record_mail_candidate(conn, cand, "2026-08-16")


def test_a_message_is_never_recorded_twice():
    conn = store.connect(":memory:")
    _seed(conn)
    assert _record(conn) is True
    assert _record(conn) is False
    assert len(store.unread_mail_candidates(conn)) == 1
    conn.close()


def test_marking_a_candidate_read_is_not_the_same_as_proposing_something():
    """`read_at` set with no proposal means "read, and it is not application news" —
    the NULL-vs-'' distinction `postings.description` draws."""
    conn = store.connect(":memory:")
    _seed(conn)
    _record(conn)
    store.mark_mail_read(conn, "abc@example", "2026-08-16")
    assert store.unread_mail_candidates(conn) == []
    assert store.pending_mail_proposals(conn) == []
    conn.close()


# -- the task -----------------------------------------------------------------------
class _Client:
    """A router that answers whatever it was handed. No mock library, per the house
    style — a stub you can read beats a mock you have to decode."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _ctx(maildir_path="/tmp"):
    return TaskContext(today="2026-08-16", maildir=maildir_path)


def _unit(conn):
    return get_task("inbox").pending(conn, _ctx())[0]


def test_inbox_reports_itself_unavailable_rather_than_idle_with_no_maildir():
    """Unavailable and nothing-to-do are different states, and only one of them is
    something to go and fix."""
    task = get_task("inbox")
    assert "maildir" in task.unavailable_reason(TaskContext(today="2026-08-16"))
    assert "not a maildir" in task.unavailable_reason(_ctx("/definitely/not/here"))


def test_a_candidate_whose_application_was_deleted_is_not_pending():
    conn = store.connect(":memory:")
    _seed(conn)
    _record(conn)
    store.delete_application(conn, "Stripe", "7966029")
    conn.commit()
    assert get_task("inbox").pending(conn, _ctx()) == []
    conn.close()


def test_two_messages_at_one_company_never_share_an_idempotency_key():
    """Both carry ats_job_id='' when the narrower cannot tell which job. Without the
    message id in `unit_key` they share an `ident`, one message's failures are charged to
    the other, and the router collapses two questions onto one answer."""
    conn = store.connect(":memory:")
    _seed(conn, [("Stripe", "1", "A"), ("Stripe", "2", "B")])
    _record(conn, mail.parse(_msg(mid="<one@x>")), job="", choices=("1", "2"))
    _record(conn, mail.parse(_msg(mid="<two@x>")), job="", choices=("1", "2"))
    units = get_task("inbox").pending(conn, _ctx())
    assert len({u.ident for u in units}) == 2
    assert len({u.idempotency_key() for u in units}) == 2
    conn.close()


def test_a_quote_the_model_could_not_ground_in_the_message_is_no_answer():
    """The quote is the only free text the model produces here. Grounding it is what
    keeps a fabricated rejection off the review list."""
    body = "We would like to schedule a call."
    good = json.dumps({"status": "screen", "application": "1", "quote": body})
    bad = json.dumps({"status": "rejected", "application": "1",
                      "quote": "We are moving forward with other candidates."})
    assert parse_reading(good, ["1"], body) is not None
    assert parse_reading(bad, ["1"], body) is None


def test_the_model_may_only_point_at_an_application_the_narrower_offered():
    body = "We would like to schedule a call."
    invented = json.dumps({"status": "screen", "application": "9999", "quote": body})
    assert parse_reading(invented, ["1"], body) is None


def test_an_unparseable_answer_is_no_answer():
    for text in (None, "", "not json", "[]", json.dumps({"status": "hired"})):
        assert parse_reading(text, ["1"], "body") is None


def test_a_message_read_as_nothing_is_marked_read_and_never_asked_again():
    """Deliberately unlike `level`'s `unclear`, which returns None and is retried three
    times. Here that would spend three calls on every newsletter, and nothing else will
    ever resolve a message that is simply not about an application."""
    conn = store.connect(":memory:")
    _seed(conn)
    _record(conn)
    task, ctx = get_task("inbox"), _ctx()
    unit = _unit(conn)
    reading = parse_reading(json.dumps(
        {"status": "none", "application": "none", "quote": ""}), ["7966029"], "x")
    assert task.apply(conn, unit, reading, ctx) == "nothing"
    conn.commit()
    assert store.unread_mail_candidates(conn) == []
    assert store.pending_mail_proposals(conn) == []
    conn.close()


def test_a_transport_failure_leaves_the_candidate_unread():
    """Failure stays absence: the message comes back on the next run rather than being
    silently consumed."""
    conn = store.connect(":memory:")
    _seed(conn)
    _record(conn)
    task, unit = get_task("inbox"), _unit(conn)
    result = asyncio.run(task.run(unit, _Client("not json at all"), _ctx()))
    assert result is None
    assert len(store.unread_mail_candidates(conn)) == 1
    conn.close()


def test_the_task_never_writes_an_application():
    """A proposal is a claim. Accepting it is a separate, human action."""
    conn = store.connect(":memory:")
    _seed(conn)
    _record(conn)
    before = [dict(r) for r in store.all_applications(conn)]
    events = {k: [dict(e) for e in v] for k, v in store.events_by_application(conn).items()}

    task, ctx = get_task("inbox"), _ctx()
    unit = _unit(conn)
    reading = parse_reading(
        json.dumps({"status": "screen", "application": "7966029",
                    "quote": "Thanks for applying."}),
        ["7966029"], unit.payload["body"])
    task.apply(conn, unit, reading, ctx)
    conn.commit()

    assert [dict(r) for r in store.all_applications(conn)] == before
    assert {k: [dict(e) for e in v]
            for k, v in store.events_by_application(conn).items()} == events
    assert len(store.pending_mail_proposals(conn)) == 1
    conn.close()


def test_a_proposal_that_repeats_the_current_status_is_not_proposed():
    conn = store.connect(":memory:")
    _seed(conn)
    _record(conn)
    task, ctx = get_task("inbox"), _ctx()
    unit = _unit(conn)
    reading = parse_reading(
        json.dumps({"status": "applied", "application": "7966029",
                    "quote": "Thanks for applying."}),
        ["7966029"], unit.payload["body"])
    assert task.apply(conn, unit, reading, ctx) == "already"
    conn.commit()
    assert store.pending_mail_proposals(conn) == []
    conn.close()


def test_a_second_interview_invitation_is_proposed_because_interview_repeats():
    """`interview` is one repeatable status by design. Suppressing a second round is the
    mistake folding the event append into the upsert would make."""
    conn = store.connect(":memory:")
    store.record_application(conn, "Stripe", "7966029", "Backend Engineer", "interview",
                             "2026-08-01T09:00:00")
    conn.commit()
    _record(conn)
    task, ctx = get_task("inbox"), _ctx()
    unit = _unit(conn)
    reading = parse_reading(
        json.dumps({"status": "interview", "application": "7966029",
                    "quote": "Thanks for applying."}),
        ["7966029"], unit.payload["body"])
    assert task.apply(conn, unit, reading, ctx).startswith("proposes")
    conn.close()
