"""The job boards page: one list across every feed, each row saying where it came from.

The split this file guards is `postings.origin`. A row with no origin came from a
curated company board and belongs on the company pages; a row with one came from a job
board. The two must *partition* the corpus — a row on both pages, or on neither, is the
failure worth catching, and neither would look like anything from the outside.

The second thing guarded here is what replaced closing duplicates: a job board's copy of
a req is kept, and says what it is. "On company page" when the employer's own board has
it, "applied" when you have applied to it by any road.
"""

import pytest

from jobtracker import config, dashboard, server, store
from jobtracker.criteria import load_criteria
from jobtracker.models import Company, Decision, Posting, Verdict

TODAY = "2026-09-15"
LEVER_URL = "https://jobs.lever.co/acme/xyz"


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


def _company(name="Acme", tier=1, **kw):
    base = dict(name=name, ats="greenhouse", slug=name.lower(), tier=tier,
                check_method="api")
    base.update(kw)
    return Company(**base)


def _verdict(conn, company, jid, decision=Decision.MATCH, reason="r"):
    store.record_verdict(conn, Verdict(company, jid, decision, reason, "rules"), TODAY)


def _board_row(conn, jid="b1", title="Klaviyo — Backend Engineer", url="https://x/b1",
               employer="Klaviyo", origin="simplify", group="Simplify New-Grad-Positions",
               decision=Decision.MATCH):
    store.append_postings(
        conn, group,
        [Posting(group, jid, title, url, "New York, NY", employer=employer,
                 description="d")],
        TODAY, origin=origin,
    )
    _verdict(conn, group, jid, decision)


def _company_row(conn, company="Acme", jid="1", title="Backend Engineer",
                 url="https://acme.example/jobs/1", decision=Decision.MATCH):
    store.sync_postings(conn, company, [Posting(company, jid, title, url, "New York, NY")],
                        TODAY)
    _verdict(conn, company, jid, decision)


def _markup(page):
    """A served page without its script.

    Every one of these pages ships `dashboard._JS`, which necessarily contains the
    strings its handlers write — "+ tracker" among them, restored after a failed POST.
    An assertion about what the page *renders* that reads the script too is asserting
    the opposite of what it says.
    """
    return page[:page.rindex("<script>")] if "<script>" in page else page


def _panel(doc, name):
    order = ("today", "applications", "all", "jobboards", "boards")
    start = doc.index(f'data-panel-body="{name}"')
    after = order[order.index(name) + 1:]
    ends = [doc.index(f'data-panel-body="{n}"') for n in after
            if f'data-panel-body="{n}"' in doc]
    return doc[start:min(ends)] if ends else doc[start:]


# -- the split ----------------------------------------------------------------------
def test_the_two_panels_partition_the_corpus(criteria):
    """Neither page may drop a row, and neither may show the other's."""
    conn = store.connect(":memory:")
    _company_row(conn)
    _board_row(conn)
    doc = dashboard.build_dashboard(conn, [_company()], TODAY, criteria)

    company_panel, board_panel = _panel(doc, "all"), _panel(doc, "jobboards")
    assert "Backend Engineer" in company_panel
    assert "Simplify" not in company_panel
    assert "Klaviyo" in board_panel
    assert "Acme" not in board_panel


def test_a_row_wears_the_board_that_carried_it(criteria):
    """One list holds every board's rows, so each row has to say which board that was —
    a tier chip would say "T—", which answers nothing about a feed."""
    conn = store.connect(":memory:")
    _board_row(conn, jid="s1", origin="simplify")
    _board_row(conn, jid="d1", origin="discord", group="Discord: #jobs",
               title="Artera — Platform Engineer", employer="Artera")
    panel = _panel(dashboard.build_dashboard(conn, [], TODAY, criteria), "jobboards")

    assert '<span class="srctag"' in panel
    assert ">simplify</span>" in panel and ">discord</span>" in panel
    assert "T—" not in panel


def test_the_board_list_is_flat_and_the_employer_is_a_column(criteria):
    """Grouping by company is right where the group is the employer. Here it would be
    the feed: one heading over thousands of rows from hundreds of employers."""
    conn = store.connect(":memory:")
    _board_row(conn)
    panel = _panel(dashboard.build_dashboard(conn, [], TODAY, criteria), "jobboards")

    assert "cohead" not in panel and "cotoggle" not in panel
    assert '<td class="emp">Klaviyo</td>' in panel
    # The employer leads the stored title and is not repeated inside the role cell.
    assert ">Backend Engineer</a>" in panel


def test_both_panels_filter_independently(criteria):
    conn = store.connect(":memory:")
    _company_row(conn)
    _board_row(conn)
    doc = dashboard.build_dashboard(conn, [_company()], TODAY, criteria)

    assert 'data-panel-body="all" data-filter-scope' in doc
    assert 'data-panel-body="jobboards" data-filter-scope' in doc
    assert doc.count('id="q"') == 0  # scoped by data-f, never by a global id
    board_panel = _panel(doc, "jobboards")
    assert 'data-attr="src"' in board_panel        # which board
    assert 'data-attr="tier"' not in board_panel   # a feed has no tier to filter on
    assert 'data-f="ats"' not in board_panel


# -- duplicates are flagged, never hidden --------------------------------------------
def test_a_req_on_both_pages_is_shown_twice_and_the_copy_says_so(criteria):
    """The old behaviour closed the job board's row. Both are kept: the company row is
    the better one to read, and the chip is how you get to it."""
    conn = store.connect(":memory:")
    _company_row(conn, url=LEVER_URL)
    _board_row(conn, url=LEVER_URL)
    doc = dashboard.build_dashboard(conn, [_company()], TODAY, criteria)

    assert "Backend Engineer" in _panel(doc, "all")
    board_panel = _panel(doc, "jobboards")
    assert "Klaviyo" in board_panel
    assert 'class="dupchip"' in board_panel
    assert "on company page" in board_panel


def test_a_req_only_a_job_board_has_carries_no_duplicate_chip(criteria):
    conn = store.connect(":memory:")
    _board_row(conn, url=LEVER_URL)
    panel = _panel(dashboard.build_dashboard(conn, [], TODAY, criteria), "jobboards")
    assert "dupchip" not in panel


def test_applying_by_one_road_flags_the_row_on_the_other(criteria):
    """You apply to a req once. The static file has no actions cell, so the fact goes on
    the row itself — it is the thing you most need from a list you read offline."""
    conn = store.connect(":memory:")
    _company_row(conn, url=LEVER_URL)
    _board_row(conn, url=LEVER_URL)
    store.record_application(conn, "Acme", "1", "Backend Engineer", "applied", TODAY,
                             url=LEVER_URL)

    panel = _panel(dashboard.build_dashboard(conn, [_company()], TODAY, criteria),
                   "jobboards")
    assert 'class="appliedchip"' in panel
    assert "applied · applied" in panel


def test_under_serve_the_applied_row_offers_a_state_not_a_button(criteria):
    """A live-looking control over a job you already applied to would record a second
    application to one req."""
    conn = store.connect(":memory:")
    _company_row(conn, url=LEVER_URL)
    _board_row(conn, url=LEVER_URL)
    store.record_application(conn, "Acme", "1", "Backend Engineer", "applied", TODAY,
                             url=LEVER_URL)

    markup = _markup(server.render_job_boards(conn, [_company()], TODAY, criteria))
    assert "applied · applied" in markup
    assert "+ tracker" not in markup
    assert 'class="track"' not in markup


def test_an_untouched_board_row_still_offers_the_tracker_button(criteria):
    conn = store.connect(":memory:")
    _board_row(conn)
    markup = _markup(server.render_job_boards(conn, [_company()], TODAY, criteria))
    assert 'class="track"' in markup and "+ tracker" in markup


# -- two pages, not one ---------------------------------------------------------------
def test_the_served_dashboard_has_no_job_boards_tab(criteria):
    """"Two entirely separate pages" is the requirement; under `serve` the boards get a
    URL of their own, and the nav is how you reach it."""
    conn = store.connect(":memory:")
    _board_row(conn)
    served = dashboard.build_dashboard(conn, [], TODAY, criteria, interactive=True,
                                       include_job_boards=False)
    assert 'data-panel="jobboards"' not in served
    assert "Klaviyo" not in served
    assert '<a href="/jobboards">Job boards</a>' in server._NAV


def test_the_static_file_keeps_both_because_a_mailed_file_has_nowhere_else(criteria):
    conn = store.connect(":memory:")
    _board_row(conn)
    doc = dashboard.build_dashboard(conn, [], TODAY, criteria)
    assert 'data-panel="jobboards"' in doc and 'data-panel-body="jobboards"' in doc


def test_the_served_page_is_one_filter_scope_and_carries_the_dashboard_script(criteria):
    conn = store.connect(":memory:")
    _board_row(conn)
    page = server.render_job_boards(conn, [_company()], TODAY, criteria)
    assert page.count("<section data-filter-scope>") == 1
    assert page.count("<script>") == 1
    assert "Job boards" in page


def test_every_button_on_the_served_page_has_a_handler_in_its_own_script(criteria):
    """The rule that keeps a button from being dead: its handler ships in the file that
    renders it. These rows are rendered by `dashboard`, so its script is the one here."""
    import re

    conn = store.connect(":memory:")
    _board_row(conn)
    page = server.render_job_boards(conn, [_company()], TODAY, criteria)
    script = page[page.rindex("<script>"):]
    for cls in set(re.findall(r'<button class="([a-z-]+)"', page)):
        assert cls in script, cls


def test_the_static_page_carries_no_controls_it_cannot_answer(criteria):
    """The filter chips are buttons and stay — they need no server. What may not appear
    is anything that POSTs: a dead control in a mailed file is worse than none."""
    conn = store.connect(":memory:")
    _board_row(conn)
    panel = _panel(dashboard.build_dashboard(conn, [], TODAY, criteria), "jobboards")
    assert "data-act" not in panel
    assert 'class="track"' not in panel
    assert '<td class="act">' not in panel
    assert "tailor-build" not in panel and "letter-build" not in panel


# -- what cannot be read automatically stays visible -----------------------------------
def test_a_board_nobody_can_read_is_listed_for_a_human(criteria):
    """Wellfound: Cloudflare on the first request, 403 from /graphql, a login past page
    one. Surfacing it is honest; pretending to have checked it would not be."""
    conn = store.connect(":memory:")
    wellfound = Company(name="Wellfound", ats="aggregator", slug="", tier=None,
                        check_method="manual", careers_page="https://wellfound.com/jobs",
                        notes="Cloudflare challenge; no keyless listing.")
    doc = dashboard.build_dashboard(conn, [_company(), wellfound], TODAY, criteria)

    board_panel = _panel(doc, "jobboards")
    assert "Wellfound" in board_panel and "Check by hand" in board_panel
    # And not in the companies list, where it would read as an employer.
    assert "Wellfound" not in _panel(doc, "boards")


def test_an_empty_board_page_says_how_to_switch_one_on(criteria):
    """Boards ship off, so "nothing here" is the state on install, and it must not read
    as "no job boards exist"."""
    conn = store.connect(":memory:")
    panel = _panel(dashboard.build_dashboard(conn, [], TODAY, criteria), "jobboards")
    assert "plugins enable" in panel


def test_a_feed_that_did_not_read_cleanly_is_named_on_its_own_page(criteria):
    """A feed's group is never in companies.yaml, which is exactly the test for "this
    health row is a board rather than a company"."""
    from jobtracker.models import BoardHealth, HealthStatus

    conn = store.connect(":memory:")
    _board_row(conn)
    store.upsert_health(
        conn,
        BoardHealth("Simplify New-Grad-Positions", HealthStatus.FETCH_FAILED,
                    detail="HTTP 404", alerting=True),
        TODAY,
    )
    panel = _panel(dashboard.build_dashboard(conn, [_company()], TODAY, criteria),
                   "jobboards")
    assert "Boards needing attention" in panel
    assert "fetch_failed" in panel
