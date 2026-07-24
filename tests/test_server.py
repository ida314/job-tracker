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
