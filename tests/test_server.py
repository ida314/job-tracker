"""The tuning surface: escaping, read-purity, and criteria validation.

No sockets. `render_tuning` is connection-in/string-out for exactly this reason —
escaping is the most security-relevant behaviour here and testing it should not
require standing up HTTP.
"""

from html.parser import HTMLParser

import pytest

from jobtracker import config, server, store
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
    def __init__(self, db_path, criteria_path, answers_path=None):
        self.db_path = db_path
        self.criteria_path = criteria_path
        self.companies_path = None
        self.answers_path = answers_path or config.ANSWERS_YAML


def _handler_for(db_path, criteria_path, answers_path=None):
    h = server.Handler.__new__(server.Handler)
    h.server = _FakeServer(db_path, criteria_path, answers_path)
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


def test_csp_allows_the_fetch_the_buttons_depend_on():
    """connect-src falls back to default-src, which is 'none' here.

    Without an explicit connect-src every write on this server — the tuning page's
    reject button included — is blocked by the browser and silently does nothing.
    """
    import inspect

    src = inspect.getsource(server.Handler._send)
    assert "connect-src 'self'" in src
    assert "default-src 'none'" in src


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

    script = page[page.rindex("<script>"):]
    classes = set(re.findall(r"class=[\"']?(app-[a-z]+)", page))
    assert classes == {"app-add", "app-save", "app-meta", "app-delete"}
    for cls in classes:
        assert f"button.{cls}" in script, cls
    for endpoint in ("/api/application", "/api/application/meta",
                     "/api/application/delete"):
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
