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
    def __init__(self, db_path, criteria_path):
        self.db_path = db_path
        self.criteria_path = criteria_path
        self.companies_path = None


def _handler_for(db_path, criteria_path):
    h = server.Handler.__new__(server.Handler)
    h.server = _FakeServer(db_path, criteria_path)
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
    from jobtracker.llm.client import RankJudgment
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
