"""The tuning surface: escaping, read-purity, and criteria validation.

No sockets. `render_tuning` is connection-in/string-out for exactly this reason —
escaping is the most security-relevant behaviour here and testing it should not
require standing up HTTP.
"""

import collections
import html
import inspect
import json
import re
from html.parser import HTMLParser

import pytest
import yaml

from jobtracker import config, curation, models, server, store
from jobtracker.criteria import load_criteria

# Titles, locations and URLs all arrive from third-party ATS APIs and are
# attacker-controllable in principle.
EVIL_TITLE = '<script>alert("pwned")</script> New Grad Engineer'
EVIL_LOCATION = '<img src=x onerror=alert(1)>'
EVIL_URL = "javascript:alert(document.cookie)"
QUOTE_TITLE = 'Engineer" onmouseover="alert(1)'


@pytest.fixture
def criteria():
    return load_criteria(config.CRITERIA_YAML)


def _db_with(title=EVIL_TITLE, location=EVIL_LOCATION, url=EVIL_URL,
             company="Stripe", job_id="X1"):
    conn = store.connect(":memory:")
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, location, url, "
        "first_seen, last_seen, closed_at) VALUES (?,?,?,?,?,?,?,NULL)",
        (company, job_id, title, location, url, "2026-07-23", "2026-07-23"),
    )
    conn.execute(
        "INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by, "
        "decided_at) VALUES (?,?,?,?,?,?)",
        (company, job_id, "match", "level:new grad+eng:generic", "rules", "2026-07-23"),
    )
    conn.commit()
    return conn


# -- escaping ----------------------------------------------------------------------
def test_script_tag_in_title_is_escaped(criteria):
    page = server.render_tuning(_db_with(), criteria)
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page


def test_event_handler_in_location_is_escaped(criteria):
    page = server.render_tuning(_db_with(), criteria)
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page


def test_javascript_url_is_neutralised(criteria):
    """A javascript: href would execute on click — _safe_url must reject the scheme."""
    page = server.render_tuning(_db_with(), criteria)
    assert 'href="javascript:' not in page


class _AttrCollector(HTMLParser):
    """Collects every attribute name the *parser* sees, i.e. real attributes only."""

    def __init__(self):
        super().__init__()
        self.attrs: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.attrs.update(name.lower() for name, _ in attrs)


def test_quote_in_title_cannot_inject_an_attribute(criteria):
    """Regression guard for interpolating values into inline handlers.

    Checked by parsing rather than substring search: an escaped `&quot;onmouseover=`
    sitting in text content is inert and *should* appear in the output. What must
    never happen is the browser parsing it as an attribute — so the assertion is
    that no element in the document carries an on* handler at all. Values reach JS
    through data-* attributes and event delegation precisely so this holds.
    """
    page = server.render_tuning(_db_with(title=QUOTE_TITLE), criteria)
    parser = _AttrCollector()
    parser.feed(page)
    handlers = {a for a in parser.attrs if a.startswith("on")}
    assert handlers == set(), f"inline event handlers rendered: {handlers}"
    assert "&quot;" in page  # the quote was escaped rather than dropped


def test_hostile_company_name_is_escaped(criteria):
    page = server.render_tuning(_db_with(company='<b>Ev"il</b>'), criteria)
    assert "<b>Ev" not in page


# -- read purity -------------------------------------------------------------------
def test_rendering_never_writes(criteria):
    """Mirrors the dashboard's purity test: opening a view must not mutate data."""
    conn = _db_with()
    before = list(conn.execute("SELECT * FROM verdicts"))
    server.render_tuning(conn, criteria)
    server.render_tuning(conn, criteria)
    after = list(conn.execute("SELECT * FROM verdicts"))
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert store.decision_count(conn) == 0
    assert store.load_overrides(conn) == {}


def test_renders_with_an_empty_database(criteria):
    """No postings, no decisions — must not raise."""
    page = server.render_tuning(store.connect(":memory:"), criteria)
    assert "Open matches (0)" in page
    assert "No judgments recorded yet" in page


# -- the firing rule is surfaced ---------------------------------------------------
def test_row_shows_the_rule_that_fired(criteria):
    """A bad match should point straight at the rule responsible."""
    page = server.render_tuning(_db_with(), criteria)
    assert "level:new grad+eng:generic" in page


def test_regressions_are_surfaced(criteria):
    conn = _db_with(title="Senior Staff Engineer")
    store.record_decision(conn, "Stripe", "X1", "Senior Staff Engineer", "match",
                          "2026-07-23")
    conn.commit()
    page = server.render_tuning(conn, criteria)
    assert "regressions 1" in page
    assert "banner bad" in page


# -- readiness ---------------------------------------------------------------------
# _readiness is the meaningful health logic (liveness is a constant), and like
# render_tuning it needs no socket: a Handler carries only the paths off .server.
class _FakeServer:
    def __init__(self, db_path, criteria_path, answers_path=None, companies_path=None):
        self.db_path = db_path
        self.criteria_path = criteria_path
        # None is the real default — `serve` only sets it when --companies was passed —
        # so reads fall back to config.COMPANIES_YAML. The add-a-company endpoint writes,
        # which needs a concrete path, so its tests pass one.
        self.companies_path = companies_path
        self.answers_path = answers_path or config.ANSWERS_YAML


def _handler_for(db_path, criteria_path, answers_path=None, companies_path=None):
    h = server.Handler.__new__(server.Handler)
    h.server = _FakeServer(db_path, criteria_path, answers_path, companies_path)
    return h


def test_readyz_ready_when_db_and_criteria_load(tmp_path):
    db = tmp_path / "state.db"
    store.connect(db).close()  # a real, openable database
    payload, status = server.Handler._readiness(_handler_for(db, config.CRITERIA_YAML))
    assert status == 200
    assert payload["status"] == "ready"
    assert payload["checks"] == {"db": "ok", "criteria": "ok"}


def test_readyz_503_when_db_cannot_be_opened(tmp_path):
    # Parent directory does not exist, so sqlite cannot open the file — the "DB not
    # mounted yet" case an orchestrator must see as not-ready, never as dead.
    bogus = tmp_path / "missing-dir" / "state.db"
    payload, status = server.Handler._readiness(_handler_for(bogus, config.CRITERIA_YAML))
    assert status == 503
    assert payload["status"] == "unready"
    assert payload["checks"]["db"].startswith("error:")
    assert payload["checks"]["criteria"] == "ok"


def test_readyz_503_when_criteria_missing(tmp_path):
    db = tmp_path / "state.db"
    store.connect(db).close()
    payload, status = server.Handler._readiness(_handler_for(db, tmp_path / "nope.yaml"))
    assert status == 503
    assert payload["checks"]["db"] == "ok"
    assert payload["checks"]["criteria"].startswith("error:")


# -- disposition endpoint ----------------------------------------------------------
# The write path behind Today's buttons. Tested through the handler method rather than
# a socket, the same way render_tuning is tested as a pure function.
def _handler_over(db_path):
    """A Handler with just enough wired up to call an api method. No socket."""
    return _handler_for(db_path, config.CRITERIA_YAML)


def _seed_ranked(db_path, jid="1", score=90.0):
    from jobtracker.tasks.judge import RankJudgment
    from jobtracker.models import Decision, Posting, Verdict

    conn = store.connect(db_path)
    store.sync_postings(conn, "Acme", [Posting("Acme", jid, "SWE", f"https://x/{jid}")],
                        "2026-08-02")
    store.record_verdict(conn, Verdict("Acme", jid, Decision.MATCH, "r", "rules"),
                         "2026-08-02")
    store.record_judgment(conn, "Acme", jid, RankJudgment("strong", "strong", "low", "w"),
                          "h", "2026-08-02")
    store.set_score(conn, "Acme", jid, score, "2026-08-02")
    conn.commit()
    conn.close()


def test_applied_records_an_application_and_shortens_the_queue(tmp_path):
    db = tmp_path / "s.db"
    _seed_ranked(db, "1", 90.0)
    _seed_ranked(db, "2", 80.0)

    res = _handler_over(db)._api_disposition(
        {"company": "Acme", "ats_job_id": "1", "action": "applied"})
    assert res["ok"] and res["detail"] == "applied"
    assert [r["ats_job_id"] for r in res["top"]] == ["2"]  # next pick handed back

    conn = store.connect(db)
    assert len(store.all_applications(conn)) == 1
    conn.close()


def test_snooze_sets_a_future_date_and_skip_does_not(tmp_path):
    db = tmp_path / "s.db"
    _seed_ranked(db, "1")
    h = _handler_over(db)

    assert h._api_disposition(
        {"company": "Acme", "ats_job_id": "1", "action": "snoozed", "days": 7})["ok"]
    conn = store.connect(db)
    row = conn.execute("SELECT kind, until FROM deferrals").fetchone()
    assert row["kind"] == "snoozed" and row["until"] > "2026-01-01"

    assert h._api_disposition(
        {"company": "Acme", "ats_job_id": "1", "action": "skipped"})["ok"]
    row = conn.execute("SELECT kind, until FROM deferrals").fetchone()
    assert row["kind"] == "skipped" and row["until"] is None
    conn.close()


@pytest.mark.parametrize("payload", [
    {"company": "Acme", "ats_job_id": "1", "action": "deleted"},   # unknown action
    {"company": "Acme", "ats_job_id": "1", "action": ""},
    {"company": "Acme", "ats_job_id": "1", "action": "snoozed", "days": 0},
    {"company": "Acme", "ats_job_id": "1", "action": "snoozed", "days": "soon"},
])
def test_a_bad_disposition_payload_is_refused_without_writing(tmp_path, payload):
    db = tmp_path / "s.db"
    _seed_ranked(db, "1")
    res = _handler_over(db)._api_disposition(payload)
    assert res["ok"] is False and res["error"]

    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM deferrals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()


def test_disposition_on_an_unknown_posting_is_refused(tmp_path):
    db = tmp_path / "s.db"
    _seed_ranked(db, "1")
    res = _handler_over(db)._api_disposition(
        {"company": "Nope", "ats_job_id": "999", "action": "skipped"})
    assert res["ok"] is False and "no such posting" in res["error"]


def test_csp_allows_the_fetch_and_the_image_the_pages_depend_on():
    """Every fetch-and-fallback trap in one header, asserted on the value not the source.

    `connect-src` and `img-src` both fall back to `default-src`, which is 'none' here,
    and both fail the same silent way: the browser blocks the request and the feature
    just does not happen. Without connect-src every write on this server is dropped;
    without img-src the apply page's preview is a broken image over a browser that is
    working perfectly.
    """
    assert "default-src 'none'" in server._CSP
    assert "connect-src 'self'" in server._CSP
    assert "img-src 'self'" in server._CSP
    # The preview is a JPEG, so it cannot go through the UTF-8 text sender.
    assert "_CSP" in inspect.getsource(server.Handler._send_bytes)


# -- settings ------------------------------------------------------------------------
# The other half of the prefill loop: prefill names a question it cannot answer, and
# this is where the answer gets written back. Same conventions as the tuning page —
# a pure-read renderer plus dict-in/dict-out write methods, both testable with no socket.
ANSWERS = """\
identity:
  first_name: Dylan
  last_name: D
  email: d@example.edu

answers:
  work_authorization: "Yes"
"""


def _answers_file(tmp_path, body=ANSWERS):
    path = tmp_path / "answers.yaml"
    path.write_text(body)
    return path


def _gap(conn, key="current_employer", ask="Who is your current employer?",
         company="Stripe", field_type="text", options=None):
    store.record_gap(conn, key, ask, field_type, company, "2026-08-02", options)
    conn.commit()


def test_settings_lists_the_open_gaps_and_the_answers_you_have(tmp_path):
    conn = store.connect(":memory:")
    _gap(conn)
    page = server.render_settings(conn, _answers_file(tmp_path))
    assert "Who is your current employer?" in page
    assert "asked by Stripe" in page
    assert "work_authorization" in page          # the answers you already wrote
    conn.close()


def test_settings_says_so_when_there_is_no_answer_bank(tmp_path):
    conn = store.connect(":memory:")
    page = server.render_settings(conn, tmp_path / "nope.yaml")
    assert "No answer bank yet" in page
    conn.close()


def test_a_broken_answer_bank_is_a_page_not_a_500(tmp_path):
    conn = store.connect(":memory:")
    page = server.render_settings(conn, _answers_file(tmp_path, "identity:\n  email: x\n"))
    assert "banner bad" in page
    assert "missing" in page
    conn.close()


def test_settings_rendering_never_writes(tmp_path):
    """Opening a view of your data must not mutate it — the dashboard's rule, here too.

    Compared by rows rather than by file bytes, the same way `test_rendering_never_writes`
    does it: the WAL checkpoints on close, so the file differs even when nothing did.
    """
    conn = store.connect(":memory:")
    _gap(conn)
    path = _answers_file(tmp_path)
    before = [tuple(r) for r in conn.execute("SELECT * FROM prefill_gaps")]
    server.render_settings(conn, path)
    server.render_settings(conn, path)
    after = [tuple(r) for r in conn.execute("SELECT * FROM prefill_gaps")]
    assert before == after
    assert path.read_text() == ANSWERS      # nor does it touch the file it renders
    conn.close()


def test_a_hostile_question_cannot_break_out_of_the_input_attribute(tmp_path):
    """Question text comes from a third-party ATS, like every title and location here."""
    conn = store.connect(":memory:")
    _gap(conn, key='x" onfocus="alert(1)', ask='<img src=x onerror=alert(1)>')
    page = server.render_settings(conn, _answers_file(tmp_path))
    assert 'onfocus="alert(1)' not in page
    assert "<img src=x" not in page
    assert "&lt;img" in page
    conn.close()


def test_answering_a_gap_writes_the_file_and_closes_the_gap(tmp_path):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _gap(conn)
    conn.close()

    path = _answers_file(tmp_path)
    res = _handler_for(db, config.CRITERIA_YAML, path)._api_answer(
        {"question_key": "current_employer", "value": "New York University"})
    assert res["ok"] and res["remaining"] == 0

    from jobtracker.answers import load_answers

    reloaded = load_answers(path)
    assert reloaded.get("current_employer") == "New York University"
    # The question text is stored as an alias, so every other company that asks it the
    # same way is answered from now on with no model call.
    assert "who is your current employer" in reloaded.by_alias

    conn = store.connect(db)
    assert store.open_gaps(conn) == []
    conn.close()


def test_answering_leaves_a_backup(tmp_path):
    db = tmp_path / "s.db"
    store.connect(db).close()
    path = _answers_file(tmp_path)
    _handler_for(db, config.CRITERIA_YAML, path)._api_answer(
        {"question_key": "why_us", "value": "Because."})
    assert (tmp_path / "answers.yaml.bak").exists()


def test_an_empty_answer_is_refused_without_writing(tmp_path):
    db = tmp_path / "s.db"
    store.connect(db).close()
    path = _answers_file(tmp_path)
    before = path.read_text()
    res = _handler_for(db, config.CRITERIA_YAML, path)._api_answer(
        {"question_key": "why_us", "value": "   "})
    assert res["ok"] is False
    assert path.read_text() == before


def test_a_write_that_would_not_parse_is_refused_and_the_file_survives(tmp_path):
    """The candidate is parsed before it replaces anything, so a bad write cannot land.

    Same guarantee criteria.yaml has had, now shared through safewrite.py — this file is
    loaded by the next prefill run, so a broken write would surface as a broken pass
    rather than as an error now.
    """
    db = tmp_path / "s.db"
    store.connect(db).close()
    path = _answers_file(tmp_path)
    before = path.read_text()

    # A key the strict loader rejects: `identity` is a closed set, and inserting into
    # `answers:` a value that re-opens the mapping would not survive validation.
    res = _handler_for(db, config.CRITERIA_YAML, path)._api_answer(
        {"question_key": "value:\n  nope", "value": "x"})
    assert res["ok"] is False
    assert path.read_text() == before
    assert not (tmp_path / "answers.yaml.candidate").exists()


def test_apply_to_refuses_an_unknown_posting(tmp_path):
    """It must not start a browser for a posting that is not there."""
    db = tmp_path / "s.db"
    store.connect(db).close()
    res = _handler_for(db, config.CRITERIA_YAML, _answers_file(tmp_path))._api_apply_to(
        {"company": "Nope", "ats_job_id": "999"})
    assert res["ok"] is False and "no such posting" in res["error"]


def test_apply_to_refuses_without_an_answer_bank(tmp_path):
    db = tmp_path / "s.db"
    _seed_ranked(db, "1")
    res = _handler_for(db, config.CRITERIA_YAML, tmp_path / "nope.yaml")._api_apply_to(
        {"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is False


def _apply_handler(tmp_path, monkeypatch):
    """A handler whose posting, company and answer bank all check out.

    Everything up to the browser is made to succeed, so the tests below are about the
    browser and nothing else.
    """
    from jobtracker.models import Company

    db = tmp_path / "s.db"
    _seed_ranked(db, "1")
    monkeypatch.setattr(
        server.config, "load_companies",
        lambda _p: [Company(name="Acme", ats="greenhouse", slug="acme", tier=1)])
    return _handler_for(db, config.CRITERIA_YAML, _answers_file(tmp_path))


def test_apply_to_says_so_when_there_is_no_browser_to_drive(tmp_path, monkeypatch):
    """The fill runs on a daemon thread, which cannot report to the click that made it.

    So anything knowable beforehand has to be answered synchronously. Without this the
    page shows "Opening…" and a browser never opens — indistinguishable from a hang.
    """
    from jobtracker import browser

    monkeypatch.setattr(browser, "unavailable_reason", lambda: browser.NO_PLAYWRIGHT)
    res = _apply_handler(tmp_path, monkeypatch)._api_apply_to(
        {"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is False
    assert "playwright is not installed" in res["error"]


def test_apply_to_refuses_a_second_window_while_one_is_open(tmp_path, monkeypatch):
    """One browser profile directory, which Chromium locks. Two at once cannot work."""
    from jobtracker import browser

    monkeypatch.setattr(browser, "unavailable_reason", lambda: None)
    handler = _apply_handler(tmp_path, monkeypatch)

    assert server._APPLY_LOCK.acquire(blocking=False)   # a window is open
    try:
        res = handler._api_apply_to({"company": "Acme", "ats_job_id": "1"})
    finally:
        server._APPLY_LOCK.release()
    assert res["ok"] is False and "already open" in res["error"]


# -- the answer bank, collected from the page ---------------------------------------
#
# The bootstrap that did not exist before: with no `answers.yaml` at all, saving your
# identity here is what creates one. The alternative was copying `answers.example.yaml`,
# which ships Ada Lovelace's name and email as documentation.

def _settings_handler(tmp_path, answers_path=None):
    return _handler_for(tmp_path / "s.db", config.CRITERIA_YAML,
                        answers_path or tmp_path / "answers.yaml")


def test_saving_your_identity_creates_the_answer_bank(tmp_path):
    h = _settings_handler(tmp_path)
    res = h._api_identity({"identity": {"first_name": "Dylan", "last_name": "D",
                                        "email": "d@nyu.edu"}})
    assert res["ok"], res
    from jobtracker.answers import load_answers
    assert load_answers(tmp_path / "answers.yaml").get("email") == "d@nyu.edu"


def test_an_incomplete_identity_is_refused_and_writes_nothing(tmp_path):
    """The loader's own message names what is missing, and safewrite means the refusal
    leaves nothing behind — not a half-written bank the next run would choke on."""
    h = _settings_handler(tmp_path)
    res = h._api_identity({"identity": {"first_name": "Dylan"}})
    assert not res["ok"]
    assert "email" in res["error"]
    assert not (tmp_path / "answers.yaml").exists()


def test_an_unknown_identity_field_is_refused(tmp_path):
    h = _settings_handler(tmp_path)
    res = h._api_identity({"identity": {"first_name": "D", "last_name": "D",
                                        "email": "d@nyu.edu", "favourite_colour": "blue"}})
    assert not res["ok"]
    assert "unknown identity field" in res["error"]


def test_a_refused_edit_leaves_the_existing_bank_untouched(tmp_path):
    """safewrite's contract, asserted at this layer: the candidate is parsed *before* it
    replaces anything, so a bad edit costs you nothing."""
    h = _settings_handler(tmp_path)
    h._api_identity({"identity": {"first_name": "Dylan", "last_name": "D",
                                  "email": "d@nyu.edu"}})
    before = (tmp_path / "answers.yaml").read_text()
    assert not h._api_identity({"identity": {"email": ""}})["ok"]
    assert (tmp_path / "answers.yaml").read_text() == before


def _bank(tmp_path):
    h = _settings_handler(tmp_path)
    h._api_identity({"identity": {"first_name": "Dylan", "last_name": "D",
                                  "email": "d@nyu.edu"}})
    return h


def _upload(h, name="resume.pdf", blob=b"%PDF-1.4 hello"):
    import base64
    return h._api_resume({"filename": name,
                          "content_b64": base64.b64encode(blob).decode()})


def test_uploading_a_resume_writes_the_file_and_points_the_key_at_it(tmp_path):
    h = _bank(tmp_path)
    res = _upload(h)
    assert res["ok"], res
    from jobtracker.answers import load_answers
    assert load_answers(tmp_path / "answers.yaml").resume == tmp_path / "resume.pdf"
    assert (tmp_path / "resume.pdf").read_bytes().startswith(b"%PDF")


def test_a_resume_upload_needs_a_bank_to_record_the_path_in(tmp_path):
    """Order is load-bearing. `load_answers` refuses a `resume:` that is not a real file,
    so there is exactly one bootstrap path: identity first, then the file."""
    res = _upload(_settings_handler(tmp_path))
    assert not res["ok"]
    assert "name and email" in res["error"]


@pytest.mark.parametrize("name,blob,message", [
    ("resume.exe", b"MZ\x90\x00", "not a resume"),
    ("resume", b"%PDF-1.4", "not a resume"),
    ("resume.pdf", b"<html>gotcha</html>", "not really"),
    ("resume.docx", b"%PDF-1.4", "not really"),
])
def test_a_resume_is_checked_by_content_not_only_by_name(tmp_path, name, blob, message):
    """The name is attacker-shaped input even when the attacker is you with a badly named
    file, and whatever lands here is attached to a real application and read by a person."""
    h = _bank(tmp_path)
    res = _upload(h, name, blob)
    assert not res["ok"]
    assert message in res["error"]
    assert not list(tmp_path.glob("resume.*"))


def test_an_empty_upload_is_refused(tmp_path):
    assert not _upload(_bank(tmp_path), blob=b"")["ok"]


def test_the_settings_page_offers_every_identity_field(tmp_path):
    """All of them, present or not: `IDENTITY_KEYS` is the whole vocabulary, so a field
    missing from this form can never be filled by anything downstream."""
    from jobtracker.answers import IDENTITY_KEYS
    conn = store.connect(":memory:")
    page = server.render_settings(conn, tmp_path / "nope.yaml")
    for key in IDENTITY_KEYS:
        assert f'data-key="{key}"' in page
    conn.close()


def test_the_settings_page_says_when_no_resume_is_attached(tmp_path):
    conn = store.connect(":memory:")
    page = server.render_settings(conn, _answers_file(tmp_path))
    assert "No resume attached" in page
    conn.close()


# -- the applications page and its endpoints -----------------------------------------
def _apps_handler(db_path):
    return _handler_for(db_path, config.CRITERIA_YAML)


def _fresh(tmp_path):
    db = tmp_path / "apps.db"
    store.connect(db).close()
    return db


def test_a_manual_application_mints_an_id_and_is_marked_manual(tmp_path):
    """The reason this page exists: a referral has no board, no verdict and no posting
    row, and every pre-existing write path refuses it."""
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    res = h._api_application({"company": "Some Startup",
                              "title": "Backend Engineer (Referral)",
                              "note": "referred by Alex"})
    assert res["ok"] is True
    assert res["ats_job_id"] == store.manual_job_id("Backend Engineer (Referral)")

    conn = store.connect(db)
    row = store.get_application(conn, "Some Startup", res["ats_job_id"])
    assert row["source"] == "manual"
    assert row["status"] == "applied"
    # Applying is the first event in the history, not a state set silently.
    assert [e["status"] for e in
            store.events_by_application(conn)[("Some Startup", res["ats_job_id"])]] \
        == ["applied"]
    conn.close()


def test_the_same_manual_title_updates_rather_than_duplicating(tmp_path):
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    h._api_application({"company": "Some Startup", "title": "Backend Engineer"})
    h._api_application({"company": "Some Startup", "title": "Backend Engineer",
                        "status": "screen"})
    conn = store.connect(db)
    assert store.application_count(conn) == 1
    assert len(store.all_applications(conn)) == 1
    conn.close()


def test_a_refused_application_writes_nothing(tmp_path):
    """A half-recorded application is worse than a refused one — you would believe it
    saved. Every one of these must leave both tables empty."""
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    refusals = [
        {"company": "", "title": "SWE"},                       # no company
        {"company": "Acme"},                                    # no title
        {"company": "Acme", "title": "SWE", "status": "ghosted"},
        {"company": "Acme", "title": "SWE", "status": "interviewing"},  # the old name
        {"company": "Acme", "title": "SWE", "next_action": "next tuesday"},
        {"company": "Acme", "title": "SWE", "url": "javascript:alert(1)"},
        {"company": "Acme", "title": "SWE", "ats_job_id": "999"},  # no such posting
    ]
    for payload in refusals:
        res = h._api_application(payload)
        assert res["ok"] is False, payload
        assert res["error"]

    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM application_events").fetchone()["n"] == 0
    conn.close()


def test_advancing_a_stage_appends_an_event_but_a_reminder_does_not(tmp_path):
    """The split that keeps the timeline readable: rescheduling a call is not a thing
    that happened to the application."""
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    jid = h._api_application({"company": "Acme", "title": "SWE"})["ats_job_id"]

    h._api_application({"company": "Acme", "ats_job_id": jid, "status": "interview",
                        "note": "round 1"})
    h._api_application({"company": "Acme", "ats_job_id": jid, "status": "interview",
                        "note": "round 2"})
    conn = store.connect(db)
    events = store.events_by_application(conn)[("Acme", jid)]
    assert [e["status"] for e in events] == ["applied", "interview", "interview"]
    conn.close()

    res = h._api_application_meta({"company": "Acme", "ats_job_id": jid,
                                   "next_action": "2026-09-01",
                                   "next_action_note": "chase recruiter"})
    assert res["ok"] is True
    conn = store.connect(db)
    assert len(store.events_by_application(conn)[("Acme", jid)]) == 3   # unchanged
    row = store.get_application(conn, "Acme", jid)
    assert row["next_action"] == "2026-09-01"
    assert row["status"] == "interview"      # a reminder does not move the stage
    conn.close()


def test_a_bad_reminder_date_changes_nothing(tmp_path):
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    jid = h._api_application({"company": "Acme", "title": "SWE",
                              "next_action": "2026-09-01"})["ats_job_id"]
    res = h._api_application_meta({"company": "Acme", "ats_job_id": jid,
                                   "next_action": "whenever"})
    assert res["ok"] is False
    conn = store.connect(db)
    assert store.get_application(conn, "Acme", jid)["next_action"] == "2026-09-01"
    conn.close()


def test_meta_on_a_missing_application_is_refused(tmp_path):
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    assert h._api_application_meta({"company": "Nope", "ats_job_id": "x"})["ok"] is False
    assert h._api_application_delete({"company": "Nope", "ats_job_id": "x"})["ok"] is False


def test_deleting_removes_the_row_and_its_history(tmp_path):
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    jid = h._api_application({"company": "Acme", "title": "SWE"})["ats_job_id"]
    h._api_application({"company": "Acme", "ats_job_id": jid, "status": "screen"})
    assert h._api_application_delete({"company": "Acme", "ats_job_id": jid})["ok"] is True
    conn = store.connect(db)
    assert store.application_count(conn) == 0
    assert store.events_by_application(conn) == {}
    conn.close()


def test_applying_from_a_pick_carries_the_posting_url_across(tmp_path):
    """The posting row is pruned when the req closes, which is exactly when 'where did
    I apply?' still needs answering."""
    db = tmp_path / "s.db"
    _seed_ranked(db)
    h = _handler_over(db)
    assert h._api_disposition({"company": "Acme", "ats_job_id": "1",
                               "action": "applied"})["ok"] is True
    conn = store.connect(db)
    row = store.get_application(conn, "Acme", "1")
    assert row["url"] == "https://x/1"
    assert row["source"] == "tracked"
    assert row["next_action"]          # a follow-up is scheduled, not left empty
    assert [e["status"] for e in store.events_by_application(conn)[("Acme", "1")]] \
        == ["applied"]
    conn.close()


def test_applications_page_renders_and_escapes(tmp_path):
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    h._api_application({"company": EVIL_TITLE, "title": QUOTE_TITLE,
                        "note": "<b>hi</b>"})
    conn = store.connect(db)
    page = server.render_applications(conn, [], "2026-08-16")
    conn.close()
    assert page.startswith("<!doctype html>")
    assert "<b>hi</b>" not in page
    assert "<script>alert" not in page
    assert page.count("<script>") == 1


def test_applications_page_opens_with_nothing_recorded(tmp_path):
    db = _fresh(tmp_path)
    conn = store.connect(db)
    page = server.render_applications(conn, [], "2026-08-16")
    conn.close()
    assert "Nothing recorded yet" in page
    assert "button class=app-add" in page or "class=app-add" in page


def test_every_button_on_the_page_has_a_handler_in_its_own_script(tmp_path):
    """The regression this repo already shipped once: a button rendered by one file
    while its handler sat in another file's script, so every click did nothing at all,
    silently. Asserting the button exists without asserting the handler is what let it
    through."""
    import re

    db = _fresh(tmp_path)
    h = _apps_handler(db)
    h._api_application({"company": "Acme", "title": "SWE"})
    conn = store.connect(db)
    page = server.render_applications(conn, [], "2026-08-16")
    conn.close()

    # A pending proposal too, so the mail controls are on the page being checked. An
    # equality that only ever sees four of six buttons is not the guard it looks like.
    conn = store.connect(db)
    conn.execute(
        "INSERT INTO mail_candidates (message_id, company, ats_job_id, choices, "
        "match_kind, subject, scanned_at) VALUES ('m1', 'Acme', '', '[]', 'company_name',"
        " 'Your application', '2026-08-16')"
    )
    store.record_mail_proposal(conn, "m1", "Acme", "", "screen", "quote", "2026-08-16")
    conn.commit()
    page = server.render_applications(conn, [], "2026-08-16")
    conn.close()

    script = page[page.rindex("<script>"):]
    classes = set(re.findall(r"class=[\"']?(app-[a-z]+)", page))
    assert classes == {"app-add", "app-save", "app-meta", "app-delete",
                       "app-accept", "app-dismiss"}
    for cls in classes:
        assert f"button.{cls}" in script, cls
    for endpoint in ("/api/application", "/api/application/meta",
                     "/api/application/delete", "/api/mail/accept",
                     "/api/mail/dismiss"):
        assert endpoint in script


def test_the_page_is_a_pure_read(tmp_path):
    db = _fresh(tmp_path)
    h = _apps_handler(db)
    h._api_application({"company": "Acme", "title": "SWE"})
    conn = store.connect(db)
    before = [dict(r) for r in store.all_applications(conn)]
    server.render_applications(conn, [], "2026-08-16")
    assert [dict(r) for r in store.all_applications(conn)] == before
    conn.close()


def test_nav_reaches_the_applications_page():
    assert 'href="/applications"' in server._NAV


# -- proposals from the mailbox -----------------------------------------------------
def _proposal(db, *, company="Stripe", job="7966029", status="screen",
              choices=("7966029",), message_id="m1", seed_app=True):
    conn = store.connect(db)
    if seed_app:
        store.record_application(conn, company, job, "Backend Engineer, New Grad",
                                 "applied", "2026-08-01T09:00:00")
    conn.execute(
        "INSERT INTO mail_candidates (message_id, company, ats_job_id, choices, "
        "match_kind, subject, from_addr, sent_on, body, snippet, scanned_at, read_at) "
        "VALUES (?, ?, ?, ?, 'sole_open', 'Your application', 'r@greenhouse.io', "
        "'2026-08-15', 'body', 'body', '2026-08-16', '2026-08-16')",
        (message_id, company, job if len(choices) == 1 else "",
         json.dumps(list(choices))),
    )
    store.record_mail_proposal(conn, message_id, company,
                               job if len(choices) == 1 else "", status,
                               "we would like to schedule a call", "2026-08-16")
    conn.commit()
    conn.close()
    return message_id


def test_accepting_is_the_only_path_from_mail_into_applications(tmp_path):
    """The task proposes and writes nothing. This is where a stage is actually set."""
    db = _fresh(tmp_path)
    mid = _proposal(db)
    h = _apps_handler(db)

    conn = store.connect(db)
    assert store.get_application(conn, "Stripe", "7966029")["status"] == "applied"
    conn.close()

    assert h._api_mail_accept({"message_id": mid})["ok"] is True

    conn = store.connect(db)
    app = store.get_application(conn, "Stripe", "7966029")
    assert app["status"] == "screen"
    events = store.events_by_application(conn)[("Stripe", "7966029")]
    # Exactly one new event (the seed used `record_application`, which logs none), and
    # its note was composed here — never by the model.
    assert [e["status"] for e in events] == ["screen"]
    assert events[-1]["note"].startswith("from mail: Your application")
    assert "schedule a call" not in events[-1]["note"]
    assert store.pending_mail_proposals(conn) == []
    conn.close()


def test_a_refused_accept_writes_nothing(tmp_path):
    """A half-recorded interview round is worse than one you type in yourself, because
    you would believe it saved."""
    db = _fresh(tmp_path)
    mid = _proposal(db)
    h = _apps_handler(db)

    conn = store.connect(db)
    before = [dict(r) for r in store.all_applications(conn)]
    events = {k: [dict(e) for e in v]
              for k, v in store.events_by_application(conn).items()}
    conn.close()

    for payload in ({}, {"message_id": ""}, {"message_id": "nope"},
                    {"message_id": mid, "ats_job_id": "not-an-application"}):
        res = h._api_mail_accept(payload)
        assert res["ok"] is False, payload
        assert res["error"]

    conn = store.connect(db)
    assert [dict(r) for r in store.all_applications(conn)] == before
    assert {k: [dict(e) for e in v]
            for k, v in store.events_by_application(conn).items()} == events
    assert len(store.pending_mail_proposals(conn)) == 1
    conn.close()


def test_an_ambiguous_proposal_cannot_be_accepted_without_choosing_a_job(tmp_path):
    db = _fresh(tmp_path)
    conn = store.connect(db)
    store.record_application(conn, "Stripe", "1", "Backend", "applied", "2026-08-01T00:00:00")
    store.record_application(conn, "Stripe", "2", "Platform", "applied", "2026-08-01T00:00:00")
    conn.commit()
    conn.close()
    mid = _proposal(db, choices=("1", "2"), seed_app=False)
    h = _apps_handler(db)

    assert h._api_mail_accept({"message_id": mid})["ok"] is False
    assert h._api_mail_accept({"message_id": mid, "ats_job_id": "2"})["ok"] is True

    conn = store.connect(db)
    assert store.get_application(conn, "Stripe", "1")["status"] == "applied"
    assert store.get_application(conn, "Stripe", "2")["status"] == "screen"
    conn.close()


def test_a_dismissed_proposal_cannot_be_accepted_afterwards(tmp_path):
    db = _fresh(tmp_path)
    mid = _proposal(db)
    h = _apps_handler(db)
    assert h._api_mail_dismiss({"message_id": mid})["ok"] is True
    res = h._api_mail_accept({"message_id": mid})
    assert res["ok"] is False and "dismissed" in res["error"]

    conn = store.connect(db)
    # Dismissed, not deleted — deleting is what would let the next scan re-propose it.
    assert store.get_mail_proposal(conn, mid)["resolution"] == "dismissed"
    assert store.get_application(conn, "Stripe", "7966029")["status"] == "applied"
    conn.close()


def test_a_subject_from_a_stranger_is_escaped_on_the_applications_page(tmp_path):
    """Unlike an ATS title, this text comes from anyone who knows your email address."""
    db = _fresh(tmp_path)
    conn = store.connect(db)
    store.record_application(conn, "Stripe", "7966029", "Backend", "applied",
                             "2026-08-01T00:00:00")
    conn.execute(
        "INSERT INTO mail_candidates (message_id, company, ats_job_id, choices, "
        "match_kind, subject, from_addr, body, snippet, scanned_at, read_at) "
        "VALUES ('m1', 'Stripe', '7966029', '[\"7966029\"]', 'sole_open', ?, ?, '', ?, "
        "'2026-08-16', '2026-08-16')",
        (EVIL_TITLE, EVIL_LOCATION, EVIL_TITLE),
    )
    store.record_mail_proposal(conn, "m1", "Stripe", "7966029", "screen",
                               EVIL_TITLE, "2026-08-16")
    conn.commit()
    page = server.render_applications(conn, [], "2026-08-16")
    conn.close()
    assert "<script>alert" not in page
    assert page.count("<script>") == 1
    # Parsed, not substring-matched: escaped text may legitimately contain the letters
    # "onerror". What must not exist is an element that actually carries a handler.
    collector = _AttrCollector()
    collector.feed(page)
    assert not {a for a in collector.attrs if a.startswith("on")}


def test_the_server_never_reads_the_maildir():
    """Walking a mailbox on a single-threaded HTTPServer would block every other
    request, and it would make a page render a writer. Scanning is the CLI's job."""
    import inspect

    src = inspect.getsource(server)
    assert "import mailbox" not in src
    assert "maildir" not in src.replace("_MAILDIR", "")


# -- a resume for one posting, and rebuilding its plan ------------------------------
PDF = b"%PDF-1.4 a real enough pdf"


def _b64(blob):
    import base64
    return base64.b64encode(blob).decode()


def _posting(conn, company="Acme", job="1", title="Backend Engineer"):
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, location, url, first_seen, "
        "last_seen) VALUES (?, ?, ?, 'NYC', 'https://x/1', '2026-08-01', '2026-08-01')",
        (company, job, title),
    )


def _own_bank(tmp_path):
    """A minimal answers.yaml with a real resume file beside it."""
    (tmp_path / "resume.pdf").write_bytes(PDF)
    path = tmp_path / "answers.yaml"
    path.write_text(
        "identity:\n  first_name: Dylan\n  last_name: D\n  email: d@example.edu\n"
        "resume: ./resume.pdf\n"
        "answers:\n  work_authorization:\n    value: \"Yes\"\n"
    )
    return path


def _form(conn, company="Acme"):
    for key, label, kind in (("first_name", "First Name", "text"),
                             ("resume", "Resume/CV", "file"),
                             ("why_acme", "Why Acme?", "textarea")):
        store.upsert_form_field(conn, company=company, form_key=key, label=label,
                                field_type=kind, now="2026-08-16", required=1,
                                options=None, question_key=None, source="dom")


def _resume_handler(tmp_path, monkeypatch):
    db = _fresh(tmp_path)
    monkeypatch.setattr(config, "RESUMES_DIR", tmp_path / "resumes")
    conn = store.connect(db)
    _posting(conn)
    _form(conn)
    conn.commit()
    conn.close()
    return db, _handler_for(db, config.CRITERIA_YAML, _own_bank(tmp_path))


def test_rebuilding_a_prefill_makes_no_model_call_and_no_network_call(tmp_path, monkeypatch):
    """The endpoint runs on a single-threaded server. Anything that could block would
    freeze every other tab, so there is no router and no fetcher in its path at all."""
    db, h = _resume_handler(tmp_path, monkeypatch)
    import socket

    def _refuse(*a, **k):
        raise AssertionError("the rebuild endpoint opened a socket")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    res = h._api_prefill({"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is True
    assert res["fields"] == 3
    conn = store.connect(db)
    assert store.get_plan(conn, "Acme", "1")["fields"] == 3
    conn.close()


def test_rebuilding_a_prefill_refuses_when_no_form_has_been_learned_yet(tmp_path, monkeypatch):
    """Zero fields is "we have never read this form", never "0/0, nothing to do".
    Absence read as success is the failure DESIGN.md §3.4 exists to prevent."""
    db = _fresh(tmp_path)
    monkeypatch.setattr(config, "RESUMES_DIR", tmp_path / "resumes")
    conn = store.connect(db)
    _posting(conn, company="Ramp")
    conn.commit()
    conn.close()
    h = _handler_for(db, config.CRITERIA_YAML, _own_bank(tmp_path))
    res = h._api_prefill({"company": "Ramp", "ats_job_id": "1"})
    assert res["ok"] is False
    assert "no application form learned" in res["error"]


def test_rebuilding_a_prefill_refuses_an_unknown_posting(tmp_path, monkeypatch):
    _db, h = _resume_handler(tmp_path, monkeypatch)
    assert h._api_prefill({"company": "Acme", "ats_job_id": "nope"})["ok"] is False
    assert h._api_prefill({"company": "Acme"})["ok"] is False


def test_rebuilding_replays_a_key_the_model_matched_once_without_asking_again(
        tmp_path, monkeypatch):
    """Every key the model ever matched was written onto form_fields.question_key, so a
    rules-only rebuild is not the downgrade it sounds like."""
    db, h = _resume_handler(tmp_path, monkeypatch)
    conn = store.connect(db)
    store.upsert_form_field(conn, company="Acme", form_key="q1",
                            label="Are you authorized to work in the US?",
                            field_type="text", now="2026-08-16", required=1,
                            options=None, question_key="work_authorization",
                            source="dom")
    conn.commit()
    conn.close()
    res = h._api_prefill({"company": "Acme", "ats_job_id": "1"})
    conn = store.connect(db)
    plan = json.loads(store.get_plan(conn, "Acme", "1")["plan"])
    conn.close()
    filled = {e["form_key"]: e["value"] for e in plan if e["value"]}
    assert filled["q1"] == "Yes"
    assert res["gaps"] < res["fields"]


def test_rebuilding_a_current_plan_still_rebuilds_it(tmp_path, monkeypatch):
    """`matches_needing_prefill` excludes a plan whose hash already matches, so routing
    the button through the queue would make it silently do nothing in exactly the case
    it exists for."""
    db, h = _resume_handler(tmp_path, monkeypatch)
    assert h._api_prefill({"company": "Acme", "ats_job_id": "1"})["ok"] is True
    conn = store.connect(db)
    first = store.get_plan(conn, "Acme", "1")["built_at"]
    conn.execute("UPDATE prefill_plans SET fields=99 WHERE company='Acme'")
    conn.commit()
    conn.close()
    assert h._api_prefill({"company": "Acme", "ats_job_id": "1"})["ok"] is True
    conn = store.connect(db)
    plan = store.get_plan(conn, "Acme", "1")
    assert plan["fields"] == 3 and plan["built_at"] == first
    conn.close()


def test_a_posting_resume_is_checked_by_content_not_only_by_name(tmp_path, monkeypatch):
    _db, h = _resume_handler(tmp_path, monkeypatch)
    ident = {"company": "Acme", "ats_job_id": "1"}
    for filename, blob, expected in (
        ("cv.exe", PDF, "not a resume"),
        ("cv.pdf", b"MZ this is a windows binary", "not really"),
        ("cv.pdf", b"", "no file"),
    ):
        res = h._api_posting_resume({**ident, "filename": filename,
                                     "content_b64": _b64(blob)})
        assert res["ok"] is False and expected in res["error"], (filename, blob)
    assert not list((tmp_path / "resumes").glob("*")) if (tmp_path / "resumes").exists() \
        else True


def test_a_posting_resume_is_stored_under_a_name_this_server_minted(tmp_path, monkeypatch):
    """A filename is attacker-shaped input even when the attacker is you with a badly
    named file, and this one names a file that goes out with a real application."""
    db, h = _resume_handler(tmp_path, monkeypatch)
    res = h._api_posting_resume({"company": "Acme", "ats_job_id": "1",
                                 "filename": "../../etc/passwd.pdf",
                                 "content_b64": _b64(PDF)})
    assert res["ok"] is True
    written = list((tmp_path / "resumes").glob("*"))
    assert len(written) == 1
    assert written[0].name == res["filename"]
    assert ".." not in written[0].name and "/" not in written[0].name
    conn = store.connect(db)
    assert store.get_posting_resume(conn, "Acme", "1")["filename"] == res["filename"]
    conn.close()


def test_a_posting_resume_leaves_the_answer_bank_and_its_hash_alone(tmp_path, monkeypatch):
    """The override is one posting's business. Folding it into `Answers.hash` would make
    every plan built with one look permanently stale."""
    from jobtracker.answers import load_answers

    db, h = _resume_handler(tmp_path, monkeypatch)
    bank_path = tmp_path / "answers.yaml"
    before_text = bank_path.read_text()
    before_hash = load_answers(bank_path).hash

    h._api_posting_resume({"company": "Acme", "ats_job_id": "1", "filename": "a.pdf",
                           "content_b64": _b64(PDF)})

    assert bank_path.read_text() == before_text
    assert load_answers(bank_path).hash == before_hash
    conn = store.connect(db)
    assert store.get_plan(conn, "Acme", "1")["answers_hash"] == before_hash
    conn.close()


def test_uploading_a_posting_resume_replans_that_posting_only(tmp_path, monkeypatch):
    db, h = _resume_handler(tmp_path, monkeypatch)
    conn = store.connect(db)
    _posting(conn, job="2", title="Platform Engineer")
    conn.commit()
    conn.close()
    h._api_prefill({"company": "Acme", "ats_job_id": "2"})

    conn = store.connect(db)
    other = dict(store.get_plan(conn, "Acme", "2"))
    conn.close()

    res = h._api_posting_resume({"company": "Acme", "ats_job_id": "1",
                                 "filename": "a.pdf", "content_b64": _b64(PDF)})
    conn = store.connect(db)
    mine = store.get_plan(conn, "Acme", "1")
    assert mine["resume_key"] == res["filename"]
    plan = json.loads(mine["plan"])
    resume_entry = [e for e in plan if e["question_key"] == "resume"][0]
    assert res["filename"] in resume_entry["value"]
    # The other posting is untouched, and still on the bank's resume.
    assert dict(store.get_plan(conn, "Acme", "2")) == other
    assert store.get_plan(conn, "Acme", "2")["resume_key"] is None
    conn.close()


def test_clearing_a_posting_resume_falls_back_to_the_bank(tmp_path, monkeypatch):
    db, h = _resume_handler(tmp_path, monkeypatch)
    res = h._api_posting_resume({"company": "Acme", "ats_job_id": "1",
                                 "filename": "a.pdf", "content_b64": _b64(PDF)})
    assert h._api_posting_resume_clear({"company": "Acme", "ats_job_id": "1"})["ok"] is True

    conn = store.connect(db)
    assert store.get_posting_resume(conn, "Acme", "1") is None
    plan = store.get_plan(conn, "Acme", "1")
    assert plan["resume_key"] is None
    resume_entry = [e for e in json.loads(plan["plan"])
                    if e["question_key"] == "resume"][0]
    assert resume_entry["value"].endswith("resume.pdf")
    conn.close()
    assert not (tmp_path / "resumes" / res["filename"]).exists()
    # Clearing something that was never set is a refusal, not a silent success.
    assert h._api_posting_resume_clear({"company": "Acme", "ats_job_id": "1"})["ok"] is False


def test_a_missing_override_file_reads_as_no_override_not_as_an_error(tmp_path, monkeypatch):
    """Raising here would surface with a browser already sitting on an open form."""
    from jobtracker import resumes

    db, h = _resume_handler(tmp_path, monkeypatch)
    res = h._api_posting_resume({"company": "Acme", "ats_job_id": "1",
                                 "filename": "a.pdf", "content_b64": _b64(PDF)})
    (tmp_path / "resumes" / res["filename"]).unlink()
    conn = store.connect(db)
    assert resumes.override_for(conn, "Acme", "1") is None
    conn.close()


def test_the_upload_cap_applies_to_every_route_that_carries_a_file():
    """A second upload route left out of the set reads its body as {} and reports "no
    file" — a correct-looking error for entirely the wrong reason."""
    assert server._UPLOAD_ROUTES == {
        "/api/resume", "/api/posting-resume", "/api/session/file",
    }


# -- the gap split ------------------------------------------------------------------
def _gaps_at(conn, key, ask, companies, first_seen="2026-08-01"):
    for company in companies:
        store.record_gap(conn, question_key=key, ask=ask, field_type="text",
                         company=company, now=first_seen)


def test_a_question_two_employers_ask_is_generic_and_one_employers_is_not(tmp_path):
    conn = store.connect(":memory:")
    _gaps_at(conn, "how_did_you_hear", "How did you hear about us?", ["Stripe", "Ramp"])
    _gaps_at(conn, "why_stripe", "Why Stripe?", ["Stripe"])
    conn.commit()
    page = server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
    assert page.index("Asked everywhere") < page.index("How did you hear about us?")
    assert page.index("Only Stripe asks") < page.index("Why Stripe?")
    assert page.index("How did you hear about us?") < page.index("Only Stripe asks")


def test_a_canonical_field_is_generic_even_at_one_employer(tmp_path):
    """A first sighting of "work authorization" is still an answer worth writing once."""
    conn = store.connect(":memory:")
    _gaps_at(conn, "work_authorization", "Authorized to work in the US?", ["Stripe"])
    conn.commit()
    page = server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
    assert "Asked everywhere" in page
    assert "Only Stripe asks" not in page


def test_generic_gaps_are_ordered_by_how_many_employers_ask_them(tmp_path):
    conn = store.connect(":memory:")
    _gaps_at(conn, "rare", "Rare question?", ["Stripe", "Ramp"])
    _gaps_at(conn, "common", "Common question?", ["Stripe", "Ramp", "Acme", "Figma"])
    conn.commit()
    page = server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
    assert page.index("Common question?") < page.index("Rare question?")
    assert "4 employers" in page


def test_every_gap_keeps_its_answer_box_and_save_button_in_both_lists(tmp_path):
    """The split decides ordering and nothing else — which is why `_JS`'s save branch
    needed no change at all."""
    conn = store.connect(":memory:")
    _gaps_at(conn, "how_did_you_hear", "How did you hear about us?", ["Stripe", "Ramp"])
    _gaps_at(conn, "why_stripe", "Why Stripe?", ["Stripe"])
    conn.commit()
    page = server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
    for key in ("how_did_you_hear", "why_stripe"):
        assert f'<input class=answer type=text placeholder=\'Your answer\' data-key="{key}"' in page
        assert f'<button class=save data-key="{key}"' in page
        # And the second way to answer one, which has to be in both lists for the same
        # reason: a question only Stripe asks is as likely to be one you have already
        # answered as a question four employers ask.
        assert f'<button class=attach data-gap="{key}">' in page


# -- attaching a question to an answer you already wrote --------------------------
# The deliberate half of what the prefill model pass did on its own until 2026-08-25.
def _attach_bank(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("identity:\n  first_name: D\n  last_name: D\n  email: e@x.edu\n"
                    "answers:\n  work_authorization: \"Yes\"\n")
    return path


def test_attaching_records_the_wording_and_closes_that_gap(tmp_path):
    """Both halves, and neither is optional.

    The alias is what recognizes this question at the next employer; `gap_key` is what
    closes the row you were actually looking at. Closing by answer key would have shut
    `work_authorization`'s row — which was never open — and left this one on the page
    while the page said it saved.
    """
    db = _fresh(tmp_path)
    path = _attach_bank(tmp_path)
    h = _handler_for(db, config.CRITERIA_YAML, answers_path=path)
    conn = store.connect(db)
    _gaps_at(conn, "are_you_authorized_to_work", "Are you authorized to work in the US?",
             ["Stripe"])
    conn.commit()
    conn.close()

    res = h._api_attach({"question_key": "work_authorization",
                         "gap_key": "are_you_authorized_to_work",
                         "alias": "Are you authorized to work in the US?"})
    assert res["ok"] and res["remaining"] == 0

    from jobtracker.answers import load_answers

    bank = load_answers(path)
    assert bank.by_alias["are you authorized to work in the us"] == "work_authorization"
    assert bank.get("work_authorization") == "Yes"       # the value is untouched


def test_attaching_to_an_answer_that_does_not_exist_is_refused(tmp_path):
    """Otherwise the question is attached to nothing and its gap closes anyway — the
    answer silently becomes blank on every form that asks it."""
    db = _fresh(tmp_path)
    path = _attach_bank(tmp_path)
    h = _handler_for(db, config.CRITERIA_YAML, answers_path=path)
    conn = store.connect(db)
    _gaps_at(conn, "q", "Some question?", ["Stripe"])
    conn.commit()
    conn.close()

    before = path.read_text()
    res = h._api_attach({"question_key": "no_such_answer", "gap_key": "q",
                         "alias": "Some question?"})
    assert not res["ok"] and "not an answer you have written" in res["error"]
    assert path.read_text() == before

    conn = store.connect(db)
    assert len(store.open_gaps(conn)) == 1
    conn.close()


def test_attaching_carries_no_value_and_cannot_edit_an_answer(tmp_path):
    """`/api/answer` edits; this only ever points. A value accepted here would be a
    second writer of the same field, and the two would disagree the first time one of
    them grew a validation rule."""
    import inspect

    src = inspect.getsource(server.Handler._api_attach)
    assert 'payload.get("value")' not in src


def test_attaching_an_identity_key_is_recorded_where_something_reads_it(tmp_path):
    """`Answers.by_alias` is built from the `answers:` block alone, so an alias of
    `email` has nowhere to live in the file. `form_fields.question_key` is the record,
    and it is what `known_question_keys` replays at every other company."""
    db = _fresh(tmp_path)
    path = _attach_bank(tmp_path)
    h = _handler_for(db, config.CRITERIA_YAML, answers_path=path)
    conn = store.connect(db)
    store.upsert_form_field(conn, company="Stripe", form_key="q7",
                            label="What is your e-mail address?", field_type="text",
                            now="2026-08-25", required=True, options=None,
                            question_key=None, source="dom")
    _gaps_at(conn, "what_is_your_e_mail_address", "What is your e-mail address?",
             ["Stripe"])
    conn.commit()
    conn.close()

    res = h._api_attach({"question_key": "email",
                         "gap_key": "what_is_your_e_mail_address",
                         "alias": "What is your e-mail address?"})
    assert res["ok"]

    conn = store.connect(db)
    assert store.known_question_keys(conn)["what is your e mail address"] == "email"
    conn.close()


def test_every_control_on_the_settings_page_has_a_handler_in_its_own_script(tmp_path):
    """The button-and-handler-in-the-same-file rule, which /settings never had one for.

    `/apply`, `/applications` and `/companies` each grew this test after a control was
    shipped whose handler lived on a page that never loaded it — a click that does
    nothing at all, silently. Settings gained `button.attach` on 2026-08-25 and would
    have been the fourth.
    """
    import re

    path = tmp_path / "answers.yaml"
    path.write_text("identity:\n  first_name: D\n  last_name: D\n  email: e@x.edu\n"
                    "answers:\n  work_authorization: \"Yes\"\n")
    conn = store.connect(":memory:")
    _gaps_at(conn, "how_did_you_hear", "How did you hear about us?", ["Stripe"])
    conn.commit()
    page = server.render_settings(conn, path)
    conn.close()

    script = page[page.rindex("<script>"):]
    classes = set(re.findall(r"class=(save|attach|save-identity|save-resume-name)[ >]",
                             page))
    assert classes == {"save", "attach", "save-identity", "save-resume-name"}
    for cls in classes:
        assert f"button.{cls}" in script, cls
    for endpoint in ("/api/answer", "/api/attach", "/api/identity", "/api/resume"):
        assert endpoint in script, endpoint
    # And the picker the attach box reads from, which is useless without its options.
    assert 'list="bankkeys"' in page
    assert '<datalist id="bankkeys">' in page
    assert '<option value="work_authorization">' in page


def test_a_gap_carries_the_question_verbatim_for_the_alias(tmp_path):
    """Saving or attaching has to record the *employer's wording*, not the slug.

    `known_question_keys` and `Answers.by_alias` are both keyed on normalized label, so
    an alias of "how_did_you_hear" would match a form asking literally that and nothing
    else. Since the model pass went, this is the only thing that makes one answer cover
    the same question at the next company.
    """
    conn = store.connect(":memory:")
    _gaps_at(conn, "how_did_you_hear", "How did you hear about us?", ["Stripe"])
    conn.commit()
    page = server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
    assert 'data-alias="How did you hear about us?"' in page


def test_a_gap_asked_by_many_names_a_few_and_counts_the_rest(tmp_path):
    """Thirty company names is the reason you cannot see the next question."""
    conn = store.connect(":memory:")
    _gaps_at(conn, "work_authorization", "Authorized to work?",
             [f"Co{i}" for i in range(9)])
    conn.commit()
    page = server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
    assert "Co0, Co1, Co2, Co3, Co4 and 4 more" in page
    assert "Co8," not in page
    assert "9 employers" in page


# -- the mirrored form ----------------------------------------------------------------
# `/apply` is where an application is actually typed now. The window still exists and is
# still the only place it can be submitted from — see the last test in this block.

def _live_session():
    from jobtracker import live
    from jobtracker.models import FormField

    s = live.start("Acme", "1", "Backend Engineer", "https://x/apply")
    found = [{"handle": "jt0"}, {"handle": "jt1"}]
    fields = [FormField(key="first_name", label="First Name", type="text",
                        required=True),
              FormField(key="why", label="Why do you want to work here?",
                        type="textarea")]
    s.absorb(live.rows_from(found, fields,
                            {"first_name": ("filled", "Dylan", None), "why": ("gap", "", None)}))
    s.set_phase("ready", "1/2 fields filled")
    return s


def test_the_apply_page_is_a_pure_read(tmp_path):
    """Opening a view of your data must not change it — the same rule the dashboard,
    the tuning page and the applications tab all follow."""
    from jobtracker import live

    db = tmp_path / "state.db"
    conn = store.connect(db)
    session = _live_session()
    before = session.snapshot()
    server.render_apply(conn, session)
    assert session.snapshot() == before
    # And nothing was written: a fresh DB has no tables' worth of rows to lose, so
    # assert on the one thing rendering could plausibly have touched.
    assert live.current() is session
    conn.close()


def test_the_apply_page_says_so_when_no_window_is_open(tmp_path):
    """Never a 500, and never a blank form implying there is one."""
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, None)
    assert "No window is open" in page
    assert 'class="lf"' not in page
    conn.close()


def test_zero_fields_renders_as_no_form_found(tmp_path):
    """Absence read as success, in the one sentence where it would happen."""
    from jobtracker import live

    conn = store.connect(tmp_path / "state.db")
    session = live.start("Acme", "1", "SWE", "https://x/apply")
    page = server.render_apply(conn, session)
    # The body, not the script: the script carries both branches of that sentence and
    # picks between them at runtime, exactly as this function does.
    body = page[:page.rindex("<script>")]
    assert "no application form found on https://x/apply" in body
    assert "nothing left to type" not in body
    conn.close()


def test_every_control_on_the_apply_page_has_a_handler_in_its_own_script(tmp_path):
    """A button whose handler is on another page is indistinguishable from a broken one.

    That is not hypothetical here: "Open prefilled" shipped with its markup in
    dashboard.py and its handler in server._JS, which the dashboard never loads, and
    every click did nothing at all with nothing logged.
    """
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()

    script = page[page.rindex("<script>"):]
    ids = set(re.findall(r'id="([a-z]+)"', page))
    assert {"pause", "reread", "reload", "preview", "closewin", "zoom", "gone"} <= ids
    for element in ("pause", "reread", "reload", "preview", "closewin", "zoom", "gone"):
        assert f"getElementById('{element}')" in script, element
    for hook in ("lf-file", "lf-detach", "tobank", "bankkey", "savebank", "bankval",
                 ".lv"):
        assert hook in script, hook
    # The picker's options come from the answer bank, so an empty list is legitimate
    # here — but the element the key box points at has to exist either way, or the
    # control silently degrades to a plain text box.
    assert 'list="bankkeys"' in page and '<datalist id="bankkeys">' in page
    for endpoint in ("/api/session", "/api/session/set", "/api/session/clear",
                     "/api/session/highlight", "/api/session/rediscover",
                     "/api/session/file", "/api/session/close", "/api/answer"):
        assert endpoint in script, endpoint


def test_saving_to_the_bank_is_offered_by_default(tmp_path):
    """Ticked from the start, because this is where the bank grows.

    The moment you know the answer to "how did you hear about us" is while you are
    typing it into a form, not on a Settings visit you make once a month. Before the
    model pass was removed the bank could afford to grow slowly, because a model was
    guessing at the questions it did not cover; now nothing is.
    """
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    assert 'class="tobank" checked' in page


def test_the_bank_is_never_written_from_the_typing_debounce(tmp_path):
    """Ticked-by-default and save-on-debounce cannot both be true.

    `bank()` used to run from the 400ms timer and untick itself after the first success.
    That was harmless while *you* did the ticking — you ticked when you had finished
    typing. Ticked from the start it stores whatever you had reached at the first pause
    and disarms, so the finished sentence never lands and the bank holds half of one.
    Blur and dropdown-change still call it: both mean "done with this field".

    Read off the source because the alternative is a browser test of a race.
    """
    js = server._APPLY_JS
    body = js[js.index("function schedule("):]
    body = body[:body.index("\n  }")]
    assert "push(card, value)" in body
    assert "bank(" not in body, "the debounce must not reach the answer bank"

    for commit in ("focusout", "change"):
        at = js.index("'" + commit + "'")
        assert "bank(" in js[at:at + 1400], commit


def test_the_bank_write_carries_the_employer_wording(tmp_path):
    """An alias keyed on our slug would match a form asking literally "how_did_you_hear".

    `Answers.by_alias` and `known_question_keys` are both keyed on the normalized label,
    so the verbatim question is the only thing that makes one answer cover the same
    question at the next company. It is what the model used to work out.
    """
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    assert "data-alias=" in page
    assert "alias: key.dataset.alias" in server._APPLY_JS


def test_the_only_way_to_submit_is_armed_and_one_shot(tmp_path):
    """The page can send an application now. Everything about how is the invariant.

    It replaces "there is no control on this page that can submit". That rule existed
    because an application is irreversible and goes out under your name — which is still
    true, and is now carried by the gate rather than by the absence of a button.

    Three properties, and all three have to hold together: it is not in the command
    vocabulary (so it cannot be reached by the channel that carries your typing), it is
    not an HTML form (so the CSP's `form-action 'none'` still means something and a
    stray Enter cannot send anything), and it is spent exactly once.
    """
    from jobtracker import live as live_mod

    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()

    # Still not a form. The page fetches; it never submits anything itself.
    assert "<form" not in page
    assert 'type="submit"' not in page

    # And still not something the queue can carry: `submit` is a session-level gate, so
    # nothing that reaches `Session.submit` can activate a control.
    assert live_mod.VOCABULARY == {"set", "clear", "rediscover", "shoot", "highlight"}
    assert "submit" not in live_mod.VOCABULARY
    session = live_mod.current()
    assert session.submit(live_mod.Command(kind="submit", handle="jt0")) is False


def test_a_submit_is_refused_until_every_required_field_is_filled(tmp_path):
    """And the refusal names them. "Not ready" is not something you can act on."""
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    session.mark("jt0", live_mod.GAP, "")   # First Name is the required one

    res = h._api_session_submit({"epoch": session.epoch, "confirm": "Acme"})
    assert res["ok"] is False
    assert "First Name" in res["error"]
    assert session.submit_requested() is False

    session.mark("jt0", live_mod.FILLED, "Dylan")
    assert h._api_session_submit(
        {"epoch": session.epoch, "confirm": "Acme"})["ok"] is True
    assert session.submit_requested() is True


def test_deleting_a_required_answer_puts_it_back_in_the_way_of_the_button(tmp_path):
    """The reason `cleared` is a status and not "filled with nothing"."""
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    session.mark("jt0", live_mod.FILLED, "Dylan")
    assert h._api_session_submit(
        {"epoch": session.epoch, "confirm": "Acme"})["ok"] is True

    session.mark("jt0", live_mod.CLEARED, "")
    res = h._api_session_submit({"epoch": session.epoch, "confirm": "Acme"})
    assert res["ok"] is False
    assert "First Name" in res["error"]


def test_the_typed_confirmation_has_to_match_the_company(tmp_path):
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    session.mark("jt0", live_mod.FILLED, "Dylan")

    for typed in ("", "acme corp", "Stripe", "  "):
        res = h._api_session_submit({"epoch": session.epoch, "confirm": typed})
        assert res["ok"] is False, typed
        assert "type Acme" in res["error"]
    assert session.submit_requested() is False

    # Case and surrounding space are not the point of the check.
    assert h._api_session_submit(
        {"epoch": session.epoch, "confirm": " acme "})["ok"] is True


def test_a_form_with_no_submit_control_says_so_rather_than_offering_a_dead_button(
        tmp_path):
    """Zero candidates is a finding, one control along from zero fields discovered.

    A button that looks like it would work if you filled one more field, over a page with
    nothing on it to press, is absence read as success in the place it costs most.
    """
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.mark("jt0", live_mod.FILLED, "Dylan")
    session.set_submit_control(None)

    res = h._api_session_submit({"epoch": session.epoch, "confirm": "Acme"})
    assert res["ok"] is False
    assert "no submit button" in res["error"]

    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, session)
    conn.close()
    body = page[:page.rindex("<script>")]
    assert "No submit button was found" in body
    assert 'id="submitbtn"' not in body, "a dead button was rendered anyway"


def test_a_submit_written_against_a_form_that_has_moved_is_refused(tmp_path):
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    session.mark("jt0", live_mod.FILLED, "Dylan")

    res = h._api_session_submit({"epoch": session.epoch - 1, "confirm": "Acme"})
    assert res["ok"] is False
    assert "changed shape" in res["error"]
    assert h._api_session_submit({"epoch": "soon", "confirm": "Acme"})["ok"] is False
    assert session.submit_requested() is False


def test_an_application_can_only_be_sent_once(tmp_path):
    """Two reads of the armed flag must not become two applications."""
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    session.mark("jt0", live_mod.FILLED, "Dylan")
    assert h._api_session_submit(
        {"epoch": session.epoch, "confirm": "Acme"})["ok"] is True

    assert session.claim_submit() is True
    assert session.claim_submit() is False, "the one submit was spent twice"
    assert session.submit_requested() is False

    res = h._api_session_submit({"epoch": session.epoch, "confirm": "Acme"})
    assert res["ok"] is False
    assert "already been submitted" in res["error"]


def test_the_form_is_not_sendable_before_it_has_settled(tmp_path):
    """`opening` and `filling` are still writing answers into it."""
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    session.mark("jt0", live_mod.FILLED, "Dylan")

    for phase in (live_mod.OPENING, live_mod.FILLING, live_mod.CLOSED):
        session.set_phase(phase)
        assert h._api_session_submit(
            {"epoch": session.epoch, "confirm": "Acme"})["ok"] is False, phase


def test_a_submit_nobody_can_vouch_for_is_offered_rather_than_recorded(tmp_path):
    """"applied" is the status that stops a job coming back round.

    Written on a guess, a failed send goes quiet in exactly the way a successful one
    does — and this is the one table whose job is to remember. So a page that did not
    move offers the write instead of making it, the shape `inbox` uses for proposals.
    """
    conn = store.connect(tmp_path / "state.db")
    session = _live_session()
    session.claim_submit()
    session.finish_submit({"changed": False, "url_before": "https://x/apply",
                           "url_after": "https://x/apply", "fields_before": 2,
                           "fields_after": 2, "note": "nothing changed"})
    page = server.render_apply(conn, session)
    conn.close()

    assert "has not been recorded as applied" in page
    assert 'id="recordit"' in page
    script = page[page.rindex("<script>"):]
    assert "getElementById('recordit')" in script
    assert "/api/application" in script


def test_a_submit_that_landed_is_recorded_without_being_asked(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    session = _live_session()
    session.claim_submit()
    session.finish_submit({"changed": True, "url_before": "https://x/apply",
                           "url_after": "https://x/thanks", "fields_before": 2,
                           "fields_after": 0, "note": "the page went to https://x/thanks"})
    page = server.render_apply(conn, session)
    conn.close()

    assert "Recorded in Applications." in page
    assert 'id="recordit"' not in page, "it asked about something it already did"


def test_the_submit_controls_have_their_handlers_on_the_page_that_renders_them(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    session = _live_session()
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    page = server.render_apply(conn, session)
    conn.close()
    script = page[page.rindex("<script>"):]

    for element in ("armtext", "submitbtn", "sendmsg", "blockers"):
        assert f'id="{element}"' in page, element
        assert f"getElementById('{element}')" in script, element
    assert "/api/session/submit" in script
    # The second of the three gates, and the one you can misclick past.
    arm = script[script.index("submitbtn.addEventListener"):]
    assert arm.index("confirm(") < arm.index("/api/session/submit")


def test_what_happened_after_the_click_is_reported_rather_than_assumed(tmp_path):
    """Nothing on this side can prove an employer received an application.

    So a page that changed and a page that did not are two different readings, and
    neither of them is the sentence "submitted successfully".
    """
    conn = store.connect(tmp_path / "state.db")
    session = _live_session()
    session.claim_submit()
    session.finish_submit({
        "changed": False, "url_before": "https://x/apply", "url_after": "https://x/apply",
        "fields_before": 2, "fields_after": 2,
        "note": "clicked 'Submit application' and nothing on the page changed — "
                "read the preview before assuming it went",
    })
    page = server.render_apply(conn, session)
    conn.close()

    assert "nothing on the page changed" in page
    assert "Read the preview before assuming it went" in page
    assert "successfully" not in page
    # And the controls are gone: there is nothing left to arm.
    assert 'id="submitbtn"' not in page[:page.rindex("<script>")]


def test_the_apply_page_lands_the_closed_state_rather_than_looking_alive(tmp_path):
    """A page that still looks live over a browser that has gone is the same defect the
    phase exists to prevent — one layer up.

    Observed 2026-08-19: Done closed the window (the log says so, the browser was gone,
    the lock was released) and the page never said. The button sat on "closing…", the
    fields still took typing, and every push queued into a closed session — which reads
    as the feature hanging, not as the window having closed.
    """
    from jobtracker import live as live_mod

    conn = store.connect(tmp_path / "state.db")
    session = _live_session()
    session.set_phase(live_mod.CLOSED)
    page = server.render_apply(conn, session)
    conn.close()

    # From <body>, so the stylesheet's own `button[disabled]` rule is not read as markup.
    body = page[page.index("<body"):page.rindex("<script>")]
    script = page[page.rindex("<script>"):]

    # The banner is out of hiding as rendered, so a reload is honest with no script.
    assert '<p class="banner bad" id="gone">' in body
    # And nothing on it takes input any more.
    assert body.count("disabled") == 2                    # one control per mirrored field
    assert '<input class="lv" type="text" value="Dylan" disabled>' in body
    # The script lands the same state from the poll, and stops polling once it has.
    assert "s.phase === 'closed'" in script
    assert "if (!stopped) setTimeout(tick, POLL_MS)" in script


def test_an_open_session_leaves_the_form_usable(tmp_path):
    """The other half of the assertion above: `disabled` must not leak into a live page."""
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    body = page[page.index("<body"):page.rindex("<script>")]
    assert "disabled" not in body
    assert 'id="gone" hidden>' in body


def test_closing_the_window_is_confirmed_first(tmp_path):
    """It discards the fill. No ATS keeps a draft for an anonymous candidate — that is
    the same fact that makes this a browser rather than a link — so the window is the
    only place the work exists and one misclick is all of it."""
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    script = page[page.rindex("<script>"):]
    close_handler = script[script.index("closewin.addEventListener"):]
    assert "confirm(" in close_handler[:600]
    assert close_handler.index("confirm(") < close_handler.index("/api/session/close")


def test_pausing_stops_the_work_rather_than_hiding_it(tmp_path):
    """Pause suppressed the `<img>` src and nothing else.

    The poll kept refreshing the watch window, so the browser thread kept rendering a
    full-page JPEG every four seconds for a picture nobody was going to look at — on the
    box that also runs the nightly pipeline. `watching()` is the only thing that stops
    the work, so pausing has to withhold the claim, not discard the bytes.
    """
    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    assert not session.watching()

    assert h._api_session()["ok"] is True
    assert session.watching(), "an ordinary poll is what says somebody is looking"

    session.watch_until = 0.0
    assert h._api_session(idle=True)["ok"] is True
    assert not session.watching(), "a paused poll still claimed a watcher"


def test_the_paused_poll_carries_the_flag_the_server_reads(tmp_path):
    """The two halves of Pause are in different files, so assert they agree."""
    conn = store.connect(tmp_path / "state.db")
    script = server.render_apply(conn, _live_session())
    conn.close()
    script = script[script.rindex("<script>"):]
    assert "'/api/session' + (paused ? '?idle=1' : '')" in script
    assert 'idle="idle=1" in self.path' in inspect.getsource(server.Handler.do_GET)


def test_the_preview_says_how_old_it_is(tmp_path):
    """It said "refreshed just now" forever, including when it had stopped refreshing.

    That was survivable while the window was reachable and this was a second opinion. It
    is the only view of the form now, so how far behind it is has to be on the page.
    """
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    script = page[page.rindex("<script>"):]
    assert "function age(then)" in script
    assert "ago.textContent = age(s.shot_at)" in script
    assert "'s ago'" in script and "'m ago'" in script


def test_the_poll_puts_back_a_value_the_fill_landed_after_the_page_did(tmp_path):
    """The fill takes seconds and the page renders immediately, so every prefilled answer
    arrived on the real form and nowhere on the page mirroring it."""
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    script = page[page.rindex("<script>"):]

    assert "input.value = f.value" in script
    assert "input.checked = f.status === 'filled'" in script
    # Still only text, classes and values — the rows are the server's, not the script's.
    for writer in ("innerHTML", "insertAdjacentHTML", "createElement"):
        assert writer not in script, writer


def test_the_page_and_its_script_call_a_status_the_same_thing(tmp_path):
    """Two copies of one table: `_STATUS_WORD` renders it, `paint` repaints it on a poll.

    Nothing bound them, so a status added to one showed the word on first load and the
    raw enum after the first poll — or the reverse. Both are the kind of drift that reads
    as a bug in the browser rather than in a lookup table.
    """
    script = server._APPLY_JS
    for status, word in server._STATUS_WORD.items():
        assert f"{status}: '{word}'" in script, f"{status} is not painted the same way"

    # And every status `live` defines has a word at all — an unlisted one falls through
    # to the raw enum on the page, which is legible to nobody.
    from jobtracker import live as live_mod
    for status in (live_mod.FILLED, live_mod.GAP, live_mod.REFUSED,
                   live_mod.PENDING, live_mod.CLEARED):
        assert status in server._STATUS_WORD


def test_deleting_a_value_is_sent_as_a_clear_rather_than_an_empty_set(tmp_path):
    """The endpoint refuses an empty `set`, so the page has to know the difference.

    If it did not, emptying a text box would come back "refused" over a field that is in
    fact still holding the old answer — the mirror disagreeing with the form.
    """
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _live_session())
    conn.close()
    script = page[page.rindex("<script>"):]

    assert "var url = '/api/session/clear';" in script
    assert "if (value) { url = '/api/session/set'; body.value = value; }" in script


def test_an_empty_set_is_refused_and_names_the_endpoint_that_does_it(tmp_path):
    """Refusing rather than guessing, because for a file row the value is a path on this
    machine — so "" means no file there and no text everywhere else."""
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()

    refused = h._api_session_set({"handle": "jt0", "value": "",
                                  "epoch": session.epoch})
    assert refused["ok"] is False
    assert "clear" in refused["error"]
    assert session.commands.empty()

    assert h._api_session_clear({"handle": "jt0", "epoch": session.epoch})["ok"] is True
    command = session.commands.get_nowait()
    assert (command.kind, command.handle, command.value) == (
        live_mod.CLEAR, "jt0", "")


def test_a_clear_with_an_unreadable_epoch_is_refused_rather_than_guessed(tmp_path):
    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    assert h._api_session_clear({"handle": "jt0", "epoch": "soon"})["ok"] is False
    assert h._api_session_clear({"epoch": session.epoch})["ok"] is False
    assert session.commands.empty()


def test_a_hostile_label_cannot_break_out_of_the_mirrored_form(tmp_path):
    """Labels and options come from a third-party ATS, like every posting title."""
    from jobtracker import live
    from jobtracker.models import FormField

    conn = store.connect(tmp_path / "state.db")
    s = live.start("Acme", "1", "SWE", "https://x/apply")
    s.absorb(live.rows_from(
        [{"handle": "jt0"}],
        [FormField(key="k", label='"><script>alert(1)</script>', type="select",
                   options=('"><img onerror=alert(1)>',))],
    ))
    page = server.render_apply(conn, s)
    conn.close()
    # Parsed, not grepped. A substring assertion would fail on the *escaped* form,
    # which is the outcome we want — what matters is whether the browser ends up with
    # an element it did not get from us.
    body = page[:page.rindex("<script>")]

    class _Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.names = []
            self.attrs = []

        def handle_starttag(self, tag, attrs):
            self.names.append(tag)
            self.attrs.extend(name for name, _ in attrs)

    tags = _Tags()
    tags.feed(body)
    # No element the page did not write itself, and no event handler on any of them.
    # (`img` is here legitimately — it is the preview.)
    assert "script" not in tags.names
    assert tags.names.count("img") == 1
    assert not [a for a in tags.attrs if a.startswith("on")]
    # The label and the option are still present, as inert text.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_the_session_endpoints_refuse_when_no_window_is_open(tmp_path):
    from jobtracker import live

    live.CURRENT = None
    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    assert h._api_session()["ok"] is False
    assert h._api_session_set({"handle": "jt0", "value": "x", "epoch": 0})["ok"] is False
    assert h._api_session_command(live.REDISCOVER)["ok"] is False


def test_setting_a_field_queues_it_rather_than_touching_a_browser(tmp_path):
    """The handler thread may never call into Playwright — that is the whole reason the
    queue exists. So this returns before anything has happened to the page."""
    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    res = h._api_session_set({"handle": "jt1", "value": "Because scale.",
                              "epoch": session.epoch})
    assert res["ok"] is True
    command = session.commands.get_nowait()
    assert (command.kind, command.handle, command.value) == (
        "set", "jt1", "Because scale.")
    assert command.epoch == session.epoch
    # Untouched: only the browser thread may mark a row.
    assert session.snapshot()["fields"][1]["status"] == "gap"


def test_the_window_can_be_closed_from_the_page(tmp_path):
    """The one ending a headless host can reach.

    The browser opens on the machine running `serve`, so on a box whose screen you
    cannot see there was no way to end a session at all: the window stayed up, the
    one-window lock stayed held, and every later "Open prefilled" answered *"a prefilled
    window is already open"* until `serve` was restarted.

    Like every other write on this page it queues rather than acts — the handler thread
    may not touch a browser — and it is deliberately not a `live.Command`: the vocabulary
    is what a request may do to the *form*, and this does nothing to the form.
    """
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()

    assert h._api_session_close()["ok"] is True
    assert session.close_requested() is True
    assert session.commands.empty()
    assert live_mod.VOCABULARY == {"set", "clear", "rediscover", "shoot", "highlight"}


def test_closing_works_from_any_phase(tmp_path):
    """A session stuck part-way through filling is exactly when you need this, and it is
    also the state that would otherwise hold the lock for the life of the process."""
    from jobtracker import live as live_mod

    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()
    session.set_phase(live_mod.FILLING)
    assert h._api_session_close()["ok"] is True
    assert session.close_requested() is True

    live_mod.CURRENT = None
    assert h._api_session_close()["ok"] is False


def test_a_refused_open_says_where_the_way_out_is(tmp_path):
    """"Close it first" was advice with no way to take it on a headless host."""
    source = inspect.getsource(server.Handler._api_apply_to)
    start = source.index("a prefilled window is already open")
    assert "Fill in page" in source[start:start + 200]


def test_a_set_with_an_unreadable_epoch_is_refused_rather_than_guessed(tmp_path):
    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    _live_session()
    assert h._api_session_set({"handle": "jt0", "value": "x", "epoch": "soon"})[
        "ok"] is False
    assert h._api_session_set({"value": "x", "epoch": 0})["ok"] is False


def test_an_upload_for_the_live_form_is_validated_before_it_is_stored(tmp_path,
                                                                     monkeypatch):
    """Same validator, same naming, same atomic write as the per-posting resume — there
    is no second way a file gets onto this box."""
    import base64

    from jobtracker import live, resumes

    monkeypatch.setattr(config, "RESUMES_DIR", tmp_path / "resumes")
    monkeypatch.setattr(resumes.config, "RESUMES_DIR", tmp_path / "resumes")
    h = _handler_for(tmp_path / "state.db", tmp_path / "criteria.yaml")
    session = _live_session()

    bad = h._api_session_file({
        "handle": "jt0", "epoch": session.epoch, "filename": "resume.exe",
        "content": base64.b64encode(b"MZ").decode(),
    })
    assert bad["ok"] is False
    assert session.commands.empty()

    good = h._api_session_file({
        "handle": "jt0", "epoch": session.epoch, "filename": "cv.pdf",
        "content": base64.b64encode(b"%PDF-1.4 real").decode(),
    })
    assert good["ok"] is True
    command = session.commands.get_nowait()
    assert command.kind == live.SET
    assert command.value.endswith(good["filename"])
    assert good["filename"].endswith(".pdf")


def test_opening_a_prefilled_window_creates_the_session_before_it_answers(tmp_path,
                                                                         monkeypatch):
    """The click navigates straight to /apply, so the session has to exist by the time
    the response is written — one created on the worker leaves the page reporting "no
    window is open" for the first second of every session."""
    from jobtracker import browser, live

    live.CURRENT = None
    h = _apply_handler(tmp_path, monkeypatch)
    # The thread is where the browser would be; it is not what this is about.
    monkeypatch.setattr(server.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(browser, "unavailable_reason", lambda: None)
    res = h._api_apply_to({"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is True
    assert res["href"] == "/apply"
    assert live.current() is not None
    assert live.current().company == "Acme"


# ---------------------------------------------------------------------------------
# The companies page: the tracked list, and the form that writes companies.yaml.
#
# This is the one endpoint on this server that opens a socket, so several of these
# tests are about the socket NOT being opened — for a manual entry, for a forced save,
# and for the page itself.

_CO_YAML = """- name: Stripe
  ats: greenhouse
  slug: stripe
  tier: 1
  check_method: api
  notes: A note long enough that a PyYAML round trip would refold it across two lines,
    which is the diff noise the line-oriented writer exists to prevent.
  expected_board_name: Stripe
- name: Bloomberg
  ats: bespoke
  tier: 3
  check_method: manual
  expected_board_name: null
"""


def _companies_handler(tmp_path):
    from jobtracker.migrate import _HEADER

    path = tmp_path / "companies.yaml"
    path.write_text(_HEADER + _CO_YAML)
    h = _handler_for(tmp_path / "c.db", config.CRITERIA_YAML, companies_path=path)
    return path, h


def _no_sockets(monkeypatch):
    import socket

    def _refuse(*a, **k):
        raise AssertionError("this path opened a socket")

    monkeypatch.setattr(socket.socket, "connect", _refuse)


def _page(tmp_path, companies=None):
    conn = store.connect(tmp_path / "c.db")
    try:
        return server.render_companies(
            conn, config.load_companies() if companies is None else companies
        )
    finally:
        conn.close()


def test_nav_reaches_the_companies_page():
    assert 'href="/companies"' in server._NAV


def test_the_companies_page_lists_every_tracked_company_grouped_by_tier(tmp_path):
    companies = config.load_companies()
    page = _page(tmp_path, companies)
    for c in companies:
        assert html.escape(c.name) in page
    # Tiers ascend down the page, the order companies.yaml itself is kept in.
    tiers = re.findall(r">T(\d|\?)<", page)
    numeric = [int(t) for t in tiers if t != "?"]
    assert numeric == sorted(numeric)


def test_the_companies_page_says_so_when_companies_yaml_will_not_load(tmp_path):
    """Unlike /applications, an unreadable file may not degrade to an empty list here —
    "nothing tracked" over a broken file is absence read as success."""
    page = _page_with_error(tmp_path, "mapping values are not allowed here")
    assert "companies.yaml did not load" in page
    assert "mapping values are not allowed here" in page


def _page_with_error(tmp_path, error):
    conn = store.connect(tmp_path / "c.db")
    try:
        return server.render_companies(conn, [], error)
    finally:
        conn.close()


def test_the_companies_page_is_a_pure_read(tmp_path, monkeypatch):
    path, _ = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    before = path.read_text()
    conn = store.connect(tmp_path / "c.db")
    try:
        server.render_companies(conn, config.load_companies(path))
    finally:
        conn.close()
    assert path.read_text() == before


def test_the_add_form_is_not_a_form_element(tmp_path):
    """The CSP is `form-action 'none'`, so a real <form> would submit into a wall. Every
    page here collects data-key inputs and POSTs JSON instead."""
    page = _page(tmp_path)
    assert "<form" not in page


def test_every_button_on_the_companies_page_has_a_handler_in_its_own_script(tmp_path):
    """Both buttons are rendered unconditionally — `co-force` is `hidden` until the
    script reveals it. A button the JS creates on the fly is one this equality cannot
    see, which would make the guard look like it was working."""
    page = _page(tmp_path)
    script = page[page.rindex("<script>"):]
    classes = set(re.findall(r"class=[\"']?(co-[a-z]+)", page))
    assert classes == {"co-save", "co-force"}
    for cls in classes:
        assert f"button.{cls}" in script, cls
    assert "/api/company" in script
    assert page.count("<script>") == 1


def test_a_manual_company_is_never_fetched(tmp_path, monkeypatch):
    """Older than this page: a bespoke portal or Workday tenant has no keyless board, and
    surfacing it for a human to check is correct where pretending to have checked it is
    not."""
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    res = h._api_company({"name": "Acme", "ats": "workday", "check_method": "manual",
                          "tier": "4"})
    assert res["ok"] and res["saved"]
    assert "never fetched" in res["skipped_because"]
    assert "- name: Acme\n" in path.read_text()


def test_saving_anyway_opens_no_socket_and_writes_a_null_board_name(tmp_path, monkeypatch):
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    res = h._api_company({"name": "Acme", "ats": "greenhouse", "slug": "acme",
                          "check_method": "api", "tier": "2",
                          "expected_board_name": "Acme Inc", "force": True})
    assert res["ok"] and res["saved"]
    entry = [e for e in yaml.safe_load(path.read_text()) if e["name"] == "Acme"][0]
    assert entry["expected_board_name"] is None


def test_an_unverified_entry_never_stores_the_typed_board_name(tmp_path, monkeypatch):
    """Writing it would make the first nightly run either alert on a name nobody checked
    or — because identity_matches returns True when either side is empty — quietly pass."""
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    h._api_company({"name": "Manual Co", "ats": "bespoke", "check_method": "manual",
                    "expected_board_name": "Something Made Up"})
    assert "Something Made Up" not in path.read_text()


def _fake_verify(monkeypatch, verification):
    monkeypatch.setattr(
        server.Handler, "_verify_board", lambda self, company: (verification, "")
    )


def test_a_board_that_belongs_to_someone_else_blocks_the_save(tmp_path, monkeypatch):
    """The ashby/cedar rule, at this door. The save is refused, the evidence comes back,
    and nothing is written."""
    from jobtracker.repair import Verification

    path, h = _companies_handler(tmp_path)
    before = path.read_text()
    _fake_verify(monkeypatch, Verification(
        False, "wrong_company", job_count=12, board_name="Someone Else",
        sample_titles=("Mortgage Analyst",)))
    res = h._api_company({"name": "Cedar", "ats": "greenhouse", "slug": "cedar",
                          "check_method": "api", "tier": "3"})
    # ok:true — the REQUEST was fine and the BOARD was not. Returning ok:false would make
    # every handler's `if (!res.ok) alert()` swallow the escape hatch.
    assert res["ok"] is True and res["saved"] is False
    assert res["verification"]["reason"] == "wrong_company"
    assert res["verification"]["sample_titles"] == ["Mortgage Analyst"]
    assert path.read_text() == before


def test_an_empty_board_blocks_the_save(tmp_path, monkeypatch):
    """The greenhouse/hubspot rule. A real-but-dead board answers 200 with an empty array
    forever, and a candidate has no history to tell that from "emptied on Tuesday"."""
    from jobtracker.repair import Verification

    path, h = _companies_handler(tmp_path)
    before = path.read_text()
    _fake_verify(monkeypatch, Verification(False, "zero_jobs", board_name="HubSpot Product"))
    res = h._api_company({"name": "HubSpot", "ats": "greenhouse", "slug": "hubspot",
                          "check_method": "api", "tier": "2"})
    assert res["saved"] is False and res["verification"]["reason"] == "zero_jobs"
    assert path.read_text() == before


def test_a_verified_save_seeds_the_board_name_the_ats_returned(tmp_path, monkeypatch):
    """Not the typed one. The fuzzy comparison then happens exactly once, under human
    eyes; every nightly check afterwards is against the exact string the ATS gave us."""
    from jobtracker.repair import Verification

    path, h = _companies_handler(tmp_path)
    _fake_verify(monkeypatch, Verification(
        True, "ok", evidence_kind="identity", job_count=40, board_name="Duolingo, Inc."))
    res = h._api_company({"name": "Duolingo", "ats": "greenhouse", "slug": "duolingo",
                          "check_method": "api", "tier": "2"})
    assert res["saved"] is True
    entry = [e for e in yaml.safe_load(path.read_text()) if e["name"] == "Duolingo"][0]
    assert entry["expected_board_name"] == "Duolingo, Inc."


def test_the_diff_returned_is_the_diff_that_was_applied(tmp_path, monkeypatch):
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    before = path.read_text()
    res = h._api_company({"name": "Acme", "ats": "bespoke", "check_method": "manual",
                          "tier": "3"})
    assert res["diff"] == curation.diff(str(path), before, path.read_text())
    assert res["backup"].endswith(".bak")


def test_adding_a_company_reflows_nothing_else(tmp_path, monkeypatch):
    """The property the whole writer exists for, asserted through the endpoint."""
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    before = path.read_text().splitlines()
    h._api_company({"name": "Acme", "ats": "bespoke", "check_method": "manual", "tier": "2"})
    after = path.read_text().splitlines()
    assert not (collections.Counter(before) - collections.Counter(after))


@pytest.mark.parametrize("payload, fragment", [
    ({"ats": "greenhouse"}, "name is required"),
    ({"name": "Stripe", "ats": "greenhouse", "slug": "s2"}, "already tracked"),
    ({"name": "X", "ats": "workday", "slug": "x", "check_method": "api"}, "adapter"),
    ({"name": "X", "ats": "greenhouse", "check_method": "api"}, "needs a slug"),
    ({"name": "X", "ats": "bespoke", "tier": "nine"}, "tier must be"),
    ({"name": "X", "ats": "bespoke", "careers_page": "javascript:alert(1)"}, "http(s) URL"),
])
def test_a_refused_company_writes_nothing(tmp_path, monkeypatch, payload, fragment):
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    before = path.read_text()
    res = h._api_company(payload)
    assert res["ok"] is False and fragment in res["error"]
    assert path.read_text() == before


def test_validation_failures_get_no_escape_hatch(tmp_path, monkeypatch):
    """`force` skips verification, never validation. An incoherent entry stays refused —
    "add anyway" is about whether the world agrees, not about whether the entry is
    coherent."""
    path, h = _companies_handler(tmp_path)
    _no_sockets(monkeypatch)
    res = h._api_company({"name": "X", "ats": "workday", "slug": "x",
                          "check_method": "api", "force": True})
    assert res["ok"] is False


def test_the_inline_verification_is_bounded(tmp_path, monkeypatch):
    """This server handles one request at a time, so the one place it opens a socket is
    capped: one attempt on an 8-second timeout, against the nightly Fetcher's three
    attempts on twenty. Widening it freezes every other tab for as long as you widen it.

    `min_interval` is deliberately NOT overridden — per-host pacing costs 0.34s across
    two requests and is not something to skip because a page is waiting.
    """
    from jobtracker import fetch as fetch_mod

    seen = {}
    real = fetch_mod.Fetcher.__init__

    def spy(self, *a, **kw):
        seen.update(kw)
        return real(self, *a, **kw)

    monkeypatch.setattr(fetch_mod.Fetcher, "__init__", spy)
    monkeypatch.setattr(fetch_mod.Fetcher, "fetch_company",
                        lambda self, c: models.FetchResult(c.name, c.ats, c.slug, ok=True,
                                                           status_code=200))
    _, h = _companies_handler(tmp_path)
    h._api_company({"name": "X", "ats": "greenhouse", "slug": "x",
                    "check_method": "api", "tier": "2"})
    assert seen["max_retries"] == 1
    assert seen["timeout"] <= 8
    assert "min_interval" not in seen


def test_a_second_verification_is_refused_rather_than_queued(tmp_path):
    """Queuing would stack a second multi-second freeze behind the first on a server that
    handles one request at a time."""
    _, h = _companies_handler(tmp_path)
    assert server._VERIFY_LOCK.acquire(blocking=False)
    try:
        res = h._api_company({"name": "X", "ats": "greenhouse", "slug": "x",
                              "check_method": "api", "tier": "2"})
        assert res["ok"] is False and "already running" in res["error"]
    finally:
        server._VERIFY_LOCK.release()


def test_a_typed_slug_is_never_labelled_provenance(tmp_path, monkeypatch):
    """`provenance` means the link was read off the company's own careers page. Nothing
    served a slug somebody typed, so claiming it would be a claim nobody made — about the
    one thing DESIGN.md §7.2 asks a human to check by hand."""
    from jobtracker import fetch as fetch_mod

    posting = models.Posting("X", "1", "Backend Engineer", "https://x.invalid", "NY", None)
    monkeypatch.setattr(
        fetch_mod.Fetcher, "fetch_company",
        lambda self, c: models.FetchResult(c.name, c.ats, c.slug, ok=True, status_code=200,
                                           observed_board_name="x", postings=[posting]),
    )
    _, h = _companies_handler(tmp_path)
    res = h._api_company({"name": "X", "ats": "ashby", "slug": "x",
                          "check_method": "api", "tier": "2"})
    assert res["saved"] is True
    assert res["verification"]["evidence_kind"] == "reachable"


def test_the_page_warns_that_a_reachable_board_was_not_identity_checked(tmp_path):
    """repair.render says it every time for the same reason; so does this page."""
    page = _page(tmp_path)
    script = page[page.rindex("<script>"):]
    assert "reachable" in script
    assert "NOT an identity check" in script


# ---------------------------------------------------------------------------
# The rules editor on /tuning. `role_type_exclude` is the point of it: it is the
# only list checked *before* the level gate, so it is the only one that can clear a
# non-engineering title out of UNCERTAIN — a title naming no level can never be
# rejected by `exclude_titles`, however many tokens that list grows.


def _suggestible_db():
    """A corpus with a rejected title, so `suggest_rules` has something to propose and
    the suggestion controls actually render."""
    conn = _db_with()
    store.record_decision(conn, "Stripe", "R1", "Deployment Strategist, Public Sector",
                          "reject", "2026-07-23")
    store.record_decision(conn, "Stripe", "R2", "Deployment Strategist, Commercial",
                          "reject", "2026-07-23")
    store.record_decision(conn, "Stripe", "R3", "Deployment Strategist, Intel",
                          "reject", "2026-07-23")
    conn.commit()
    return conn


def test_every_gating_list_is_reachable_from_the_tuning_page(criteria):
    page = server.render_tuning(_db_with(), criteria)
    for key, _kind, _why in server._GATING_LISTS:
        assert f"<code>{key}</code>" in page, f"{key} is not on the page"
        assert f'data-list="{key}"' in page, f"{key} has no add control"


def test_the_rules_editor_offers_no_location_list(criteria):
    """Location lists rank, they never gate. Heading them 'rules' next to the gating
    lists would advertise a filter the user explicitly ruled out in 2026-07-22."""
    page = server.render_tuning(_db_with(), criteria)
    for key in ("locations_nyc", "locations_us", "locations_non_us"):
        assert f"<code>{key}</code>" not in page


def test_current_tokens_are_shown_not_just_an_empty_box(criteria):
    """An add box over an invisible list is how you add a token that is already there,
    or miss that the one you need is absent. The section is a reading first."""
    page = server.render_tuning(_db_with(), criteria)
    for token in criteria.role_type_exclude[:5]:
        assert f"<span class=chip>{token}</span>" in page


def test_a_suggestion_may_only_target_a_reject_list(criteria):
    """Suggestions are phrases mined from titles you REJECTED. Letting one land in
    `engineering_terms` or `role_type_include` would widen matching on exactly the
    titles the suggestion exists to remove — one click, evidence inverted."""
    assert set(server._SUGGEST_TARGETS) <= {"exclude_titles", "role_type_exclude"}
    picker = server._list_picker("role_type_exclude")
    assert '<option value="role_type_exclude" selected>' in picker
    for key in ("level_include", "role_type_include", "engineering_terms"):
        assert key not in picker


def test_rule_controls_have_handlers_in_the_file_that_renders_them(criteria):
    """The 'Open prefilled' rule: markup in one file and its handler in another is how
    a control ships dead and silent. Both rule controls are emitted by render_tuning,
    so both handlers belong in server._JS."""
    page = server.render_tuning(_suggestible_db(), criteria)
    for cls in ("add-token", "add-rule"):
        assert f"class={cls}" in page, f"{cls} is not rendered"
        assert f"button.{cls}" in server._JS, f"{cls} has markup but no handler"


def test_the_add_rule_button_no_longer_hardcodes_a_list():
    """It used to post `list: 'exclude_titles'` regardless of what the picker said.
    The picker is only real if the handler reads it."""
    add_branch = server._JS.split("button.add-rule")[1].split("button.add-token")[0]
    assert "select.sugg-list" in add_branch
    # The picked value is what travels; `exclude_titles` survives only as the fallback
    # for a suggestion rendered without a picker.
    assert "sel.value" in add_branch.split("addRule(")[1]


def test_the_rules_editor_has_no_delete_control(criteria):
    """Removing a token silently re-admits every posting it was rejecting. That needs
    an edit plus `jobtracker eval`, not a button that skips the regression replay this
    whole page exists to run."""
    page = server.render_tuning(_db_with(), criteria)
    section = page.split("<h2>Rules</h2>")[1]
    assert "del-token" not in section
    assert "remove" not in section.lower()


def test_adding_a_token_to_role_type_exclude_writes_it(tmp_path, criteria):
    """End to end through the endpoint the buttons post to."""
    path = tmp_path / "criteria.yaml"
    path.write_text(config.CRITERIA_YAML.read_text())
    h = _handler_for(store.connect(":memory:") and (tmp_path / "s.db"), path)
    store.connect(tmp_path / "s.db").close()
    res = h._api_rule({"phrase": "counsel", "list": "role_type_exclude"})
    assert res["ok"] is True and res["list"] == "role_type_exclude"
    assert "counsel" in load_criteria(path).role_type_exclude
    assert "counsel" not in load_criteria(path).exclude_titles


def test_an_unknown_list_is_refused(tmp_path):
    path = tmp_path / "criteria.yaml"
    path.write_text(config.CRITERIA_YAML.read_text())
    store.connect(tmp_path / "s.db").close()
    h = _handler_for(tmp_path / "s.db", path)
    res = h._api_rule({"phrase": "x", "list": "role_type_exclud"})
    assert res["ok"] is False and "unknown criteria list" in res["error"]


# -- the answer behind a field, and the way to change it ---------------------------------
def _mirror(rows):
    """A session holding exactly these rows. `rows` are `(FormField, status, value,
    question_key)` — the shape `browser.fill_application` publishes."""
    from jobtracker import live
    from jobtracker.models import FormField  # noqa: F401 — used by callers

    session = live.start("Acme", "1", "Backend Engineer", "https://x/apply")
    found = [{"handle": f"jt{i}"} for i in range(len(rows))]
    fields = [r[0] for r in rows]
    carried = {r[0].key: (r[1], r[2], r[3]) for r in rows}
    session.absorb(live.rows_from(found, fields, carried))
    session.set_phase("ready", "x")
    return session


def test_a_field_says_which_answer_filled_it_and_lets_you_change_it(tmp_path):
    """The bank block used to appear only on questions the fill could not answer.

    So the bank was writable exactly once per question — the first time it was asked —
    and a field holding "New York, New York" under the label "Country" gave no sign that
    the value had come from the bank at all, let alone which key held it. The only way to
    fix a wrong answer already going into real applications was to edit the file.
    """
    from jobtracker import live
    from jobtracker.models import FormField

    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _mirror([
        (FormField(key="country", label="Country*", type="combobox", required=True),
         live.FILLED, "New York, New York", "location"),
        (FormField(key="why", label="Why us?", type="textarea"), live.GAP, "", None),
    ]))
    conn.close()

    assert "from your answer bank as <code>location</code>" in page
    assert 'class="bankval" type="text" value="New York, New York"' in page
    assert 'class="savebank" data-key="location"' in page
    # An unanswered field still offers to teach the bank, under a key you can edit.
    assert 'class="tobank"' in page and 'value="why_us"' in page


def test_a_checkbox_set_renders_as_one_question_with_its_choices(tmp_path):
    """Nine boxes under one head, not nine questions named after their own answers."""
    from jobtracker import live
    from jobtracker.models import FormField

    members = [
        FormField(key=f"q[]::{n.lower()}", label="How did you hear about us?",
                  type="checkbox", required=True, group="How did you hear about us?",
                  option=n, options=("LinkedIn", "Glassdoor"))
        for n in ("LinkedIn", "Glassdoor")
    ]
    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(
        conn, _mirror([(m, live.GAP, "", None) for m in members])
    )
    conn.close()

    assert page.count('class="lfg"') == 1
    assert page.count("How did you hear about us?") >= 1
    assert "> LinkedIn</label>" in page and "> Glassdoor</label>" in page
    # One bank control for the question, not one per box, and each box knows the choice
    # it sends — 'yes' compared against "LinkedIn" would refuse every tick.
    assert page.count('class="tobank"') == 1
    assert 'data-option="LinkedIn"' in page


def test_a_dropdown_renders_as_a_dropdown_when_its_options_are_known(tmp_path):
    """And as a text box, with a note, when they are not — because a `<select>` holding
    only "— choose —" is worse than either."""
    from jobtracker import live
    from jobtracker.models import FormField

    conn = store.connect(tmp_path / "state.db")
    page = server.render_apply(conn, _mirror([
        (FormField(key="q1", label="Work auth?", type="combobox", required=True,
                   options=("Yes", "No")), live.GAP, "", None),
        (FormField(key="country", label="Country*", type="combobox", required=True),
         live.GAP, "", None),
    ]))
    conn.close()

    assert '<option value="Yes">Yes</option>' in page
    assert "a menu on the real form" in page
    assert page.count("<select class=\"lv\"") == 1


def test_the_filename_an_employer_sees_is_a_setting(tmp_path):
    """Disk names are minted for collision safety — `twilio_7816159_1f4c9a02.pdf` — and
    a person at the other end opens whatever the upload was called."""
    answers = tmp_path / "answers.yaml"
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 x")
    answers.write_text("identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n"
                       "resume: ./resume.pdf\n")

    handler = _handler_for(tmp_path / "state.db", config.CRITERIA_YAML,
                           answers_path=answers)
    assert handler._api_resume_name({"name": "Dylan_Dodds_Resume.pdf"})["ok"] is True
    assert "Dylan_Dodds_Resume.pdf" in answers.read_text()
    # A path is refused: this names a file, it does not choose where one goes.
    assert handler._api_resume_name({"name": "../etc/passwd"})["ok"] is False


def test_changing_an_identity_answer_reaches_identity_not_answers(tmp_path):
    """`Answers.get` reads `identity:` first, so an `answers:` entry of the same name is
    a write nothing ever loads — saved, validated, and silently inert."""
    import yaml as _yaml

    answers = tmp_path / "answers.yaml"
    answers.write_text("identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n"
                       "  location: New York, New York\n")

    handler = _handler_for(tmp_path / "state.db", config.CRITERIA_YAML,
                           answers_path=answers)
    assert handler._api_answer({"question_key": "location", "value": "Brooklyn, NY"})["ok"]

    parsed = _yaml.safe_load(answers.read_text())
    assert parsed["identity"]["location"] == "Brooklyn, NY"
    assert "location" not in (parsed.get("answers") or {})
