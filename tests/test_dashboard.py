"""Dashboard rendering: escaping, band mapping, read-only-ness, filter data attributes.

The valuable assertions here are the ones a browser would not obviously fail on. HTML
that renders fine can still be silently wrong — an unescaped title, a javascript: href,
a row that carries the wrong tier and so hides under the wrong filter chip.
"""

from jobtracker import dashboard, store
from jobtracker.models import Company, Decision, Posting, Verdict


def _setup(postings_and_verdicts, companies):
    conn = store.connect(":memory:")
    for company, posting, decision in postings_and_verdicts:
        store.sync_postings(conn, company, [posting], "2026-07-01")
        store.record_verdict(
            conn, Verdict(company, posting.ats_job_id, decision, "why", "rules"), "2026-07-01"
        )
    return conn, companies


def _company(name, tier, ats="greenhouse"):
    return Company(name=name, ats=ats, slug=name.lower(), tier=tier, check_method="api")


def test_escapes_hostile_title_and_company():
    """Titles come from third-party APIs. A <script> in one must not become markup."""
    evil = Posting("Acme", "1", '<script>alert("xss")</script>', "https://x/1", "NYC")
    conn, companies = _setup([("Acme", evil, Decision.MATCH)], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert "<script>alert" not in doc
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in doc
    # The page's own trailing <script> block is the only real one.
    assert doc.count("<script>") == 1


def test_rejects_non_http_url_scheme():
    bad = Posting("Acme", "1", "Engineer", "javascript:alert(1)", "NYC")
    conn, companies = _setup([("Acme", bad, Decision.MATCH)], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert "javascript:" not in doc
    assert 'href="#"' in doc


def test_band_mapping_matches_strategy_grouping():
    assert dashboard._band_var(1) == "--band-anchor"
    assert dashboard._band_var(2) == "--band-anchor"
    assert dashboard._band_var(3) == "--band-applied"
    assert dashboard._band_var(5) == "--band-applied"
    assert dashboard._band_var(6) == "--band-research"
    assert dashboard._band_var(7) == "--band-research"
    assert dashboard._band_var("—") == "--band-none"  # untiered


def test_every_band_var_is_defined_in_the_css():
    """A typo'd var name renders as a transparent bar rather than an error."""
    for tier in (1, 3, 7, "—"):
        var = dashboard._band_var(tier)
        assert f"{var}:" in dashboard._CSS
        assert f"{var}-ink:" in dashboard._CSS


def test_rows_carry_filter_attributes():
    conn, companies = _setup(
        [
            ("Acme", Posting("Acme", "1", "Backend Engineer", "https://x/1", "NYC"),
             Decision.MATCH),
            ("Zeta", Posting("Zeta", "9", "Data Engineer", "https://x/9", "Remote"),
             Decision.UNCERTAIN),
        ],
        [_company("Acme", 1), _company("Zeta", 6, ats="ashby")],
    )
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert 'data-tier="1"' in doc and 'data-ats="greenhouse"' in doc
    assert 'data-tier="6"' in doc and 'data-ats="ashby"' in doc
    # The search blob is lowercased so the JS can do a case-insensitive substring test.
    assert "backend engineer" in doc
    # Both verdict buckets are rendered, in their own tables.
    assert "Open matches" in doc and "Uncertain" in doc


def test_dashboard_never_writes_to_the_database():
    """`report` marks manual companies as surfaced; opening a view must not."""
    conn, companies = _setup(
        [("Acme", Posting("Acme", "1", "Engineer", "https://x/1", "NYC"), Decision.MATCH)],
        [_company("Acme", 1), Company(name="Manual Co", ats="workday", slug="",
                                      tier=2, check_method="manual")],
    )
    before = conn.execute("SELECT count(*) FROM manual_checks").fetchone()[0]
    dashboard.build_dashboard(conn, companies, "2026-07-22")
    after = conn.execute("SELECT count(*) FROM manual_checks").fetchone()[0]
    assert before == after == 0
    # ...and the manual company is still surfaced in the page itself.
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "Manual Co" in doc


def test_empty_database_still_renders():
    conn = store.connect(":memory:")
    doc = dashboard.build_dashboard(conn, [], "2026-07-22")
    assert doc.startswith("<!doctype html>")
    assert "No run recorded yet" in doc
    assert doc.rstrip().endswith("</html>")


def test_closed_postings_are_excluded():
    conn = store.connect(":memory:")
    p = Posting("Acme", "1", "Backend Engineer", "https://x/1", "NYC")
    store.sync_postings(conn, "Acme", [p], "2026-07-01")
    store.record_verdict(
        conn, Verdict("Acme", "1", Decision.MATCH, "why", "rules"), "2026-07-01"
    )
    store.sync_postings(conn, "Acme", [], "2026-07-02")  # posting disappears -> closed
    doc = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22")
    assert "Backend Engineer" not in doc


# -- Today's picks -----------------------------------------------------------------
# The page's whole purpose is to shorten the distance between opening it and applying
# to something, so these guard the picks against the ways they could silently vanish.
def _ranked(conn, company, jid, score, why="because", loc="New York, NY"):
    from jobtracker.tasks.judge import RankJudgment

    store.record_judgment(conn, company, jid, RankJudgment("strong", "strong", "low", why),
                          "h", "2026-07-22")
    store.set_score(conn, company, jid, score, "2026-07-22")


def _one_match(jid="1", title="Backend Engineer, New Grad", loc="New York, NY"):
    return Posting("Acme", jid, title, f"https://x/{jid}", loc)


def test_picks_render_above_the_tables_with_their_reasoning():
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5, why="owns the ingestion pipeline")
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    assert doc.index('data-panel-body="today"') < doc.index('data-panel-body="all"')
    assert "owns the ingestion pipeline" in doc
    assert "88.5" in doc
    assert "fit strong" in doc


def test_picks_are_not_filterable():
    """A tier or location filter set on another tab must not empty a curated list.

    The filter JS selects `table[data-filterable]`; the picks are articles, not a
    filterable table, which is what keeps them out of its reach.
    """
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")

    picks = doc[doc.index('data-panel-body="today"'):doc.index('data-panel-body="all"')]
    assert "data-filterable" not in picks


def test_pick_content_is_pinned_to_the_second_column():
    """The action row must not fall back into the 34px rank column.

    `.pick` is a `34px 1fr` grid. `.why` and `.prefill` are both optional, so the number
    of content blocks varies — anything left to auto-placement lands in column 1 as soon
    as the count exceeds whatever the rank spans, and the button row stacks vertically in
    34px instead of running left to right. Pinning every non-rank child to column 2 is
    what makes the card independent of the block count.
    """
    assert ".pick > :not(.rank) { grid-column: 2; }" in dashboard._CSS
    assert "grid-row: 1 / span" not in dashboard._CSS


def test_unranked_matches_are_reported_not_hidden():
    """A silently short list is indistinguishable from having found nothing."""
    conn, companies = _setup(
        [("Acme", _one_match("1"), Decision.MATCH),
         ("Acme", _one_match("2"), Decision.MATCH)],
        [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)          # only one of the two is judged
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "1 open match(es) are unranked" in doc


def test_applied_and_skipped_postings_leave_the_picks():
    conn, companies = _setup(
        [("Acme", _one_match("1"), Decision.MATCH),
         ("Acme", _one_match("2"), Decision.MATCH),
         ("Acme", _one_match("3"), Decision.MATCH)],
        [_company("Acme", 1)])
    for jid, score in (("1", 90.0), ("2", 80.0), ("3", 70.0)):
        _ranked(conn, "Acme", jid, score)
    store.record_application(conn, "Acme", "1", "t", "applied", "2026-07-22")
    store.set_deferral(conn, "Acme", "2", "skipped", "2026-07-22")

    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    picks = doc[doc.index('data-panel-body="today"'):doc.index('data-panel-body="all"')]
    assert "https://x/3" in picks
    assert "https://x/1" not in picks and "https://x/2" not in picks


def test_the_static_file_has_no_buttons_and_the_served_page_does():
    """Buttons POST. The file written by `jobtracker dashboard` must stay offline and
    read-only, so a dead button in it would be worse than no button."""
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)

    static = dashboard.build_dashboard(conn, companies, "2026-07-22")
    served = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
    assert 'data-act="applied"' not in static
    assert 'data-act="applied"' in served
    assert 'data-act="snoozed"' in served


def test_tabs_carry_every_panel_server_side():
    """JS only hides panels; with it off the whole page is still there."""
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    for panel in ("today", "all", "boards"):
        assert f'data-panel-body="{panel}"' in doc
        assert f'data-panel="{panel}"' in doc
    assert doc.count("<script>") == 1  # tab JS lives in the one existing block


def test_a_hostile_company_name_cannot_break_out_of_a_button_attribute():
    """Button attributes carry company and job id straight back to the POST handler."""
    evil = Posting('Ac"me', "1", "SWE", "https://x/1", "NYC")
    conn = store.connect(":memory:")
    store.sync_postings(conn, 'Ac"me', [evil], "2026-07-01")
    store.record_verdict(
        conn, Verdict('Ac"me', "1", Decision.MATCH, "w", "rules"), "2026-07-01")
    _ranked(conn, 'Ac"me', "1", 90.0)

    doc = dashboard.build_dashboard(
        conn, [_company('Ac"me', 1)], "2026-07-22", interactive=True)
    assert 'data-company="Ac"me"' not in doc
    assert "Ac&quot;me" in doc


# -- prefill on the Today tab --------------------------------------------------------
# The counts belong in the static file (they say whether opening this one takes thirty
# seconds or ten minutes) but the button does not, for the same reason the disposition
# buttons do not: it drives a browser, which only `serve` can do.
def test_a_pick_says_how_much_of_the_application_is_already_filled():
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    store.record_plan(conn, "Acme", "1", "[]", fields=16, gaps=3,
                      answers_hash="h", now="2026-07-22")
    conn.commit()

    page = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "prefill 13/16 fields" in page
    assert "3 need you" in page


def test_a_fully_prefilled_application_says_there_is_nothing_left_to_type():
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    store.record_plan(conn, "Acme", "1", "[]", fields=8, gaps=0,
                      answers_hash="h", now="2026-07-22")
    conn.commit()

    page = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "prefill 8/8 fields" in page
    assert "nothing left to type" in page


def test_a_pick_with_no_plan_says_how_to_get_one():
    """Silence would read as "nothing to fill", which is the opposite of the truth."""
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)

    page = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "no prefill yet" in page
    assert "work --task prefill" in page


def test_the_open_prefilled_button_exists_only_under_serve():
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    store.record_plan(conn, "Acme", "1", "[]", 16, 3, "h", "2026-07-22")
    conn.commit()

    static = dashboard.build_dashboard(conn, companies, "2026-07-22")
    served = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
    assert 'class="apply-to"' not in static
    assert 'class="apply-to"' in served
    # The counts are useful offline and appear in both.
    assert "prefill 13/16 fields" in static and "prefill 13/16 fields" in served


def test_the_view_window_link_appears_only_with_a_viewer_configured():
    """`serve` on a headless host opens a window nobody can see; this is where to see it.

    A link, not a managed thing: the app never starts the viewer and never checks it, so
    the only states are "configured" and "not configured".
    """
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    conn.commit()

    plain = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
    with_view = dashboard.build_dashboard(
        conn, companies, "2026-07-22", interactive=True,
        view_url="https://gx10.example.ts.net/vnc.html")
    static = dashboard.build_dashboard(
        conn, companies, "2026-07-22", view_url="https://gx10.example.ts.net/vnc.html")

    # The element, not the word — the stylesheet names the class either way.
    assert 'class="viewwin"' not in plain
    assert 'class="viewwin"' in with_view
    assert "https://gx10.example.ts.net/vnc.html" in with_view
    # The mailable file has no button, so a link to a screen it cannot drive is noise.
    assert 'class="viewwin"' not in static


def test_a_hostile_viewer_url_cannot_smuggle_a_scheme():
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    conn.commit()

    doc = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True,
                                    view_url="javascript:alert(1)")
    assert "javascript:alert" not in doc


def test_the_open_prefilled_button_has_a_handler_on_the_page_that_renders_it():
    """The regression: it did not, and the click did nothing at all.

    The handler used to live in `server._JS`, which only the tuning and settings pages
    emit — so the one page carrying the button never loaded the code that answers it.
    Nothing failed, nothing logged, the button just sat there. Asserted structurally,
    because "the button is rendered" and "something listens for it" are two claims and
    only the first was ever checked.
    """
    conn, companies = _setup(
        [("Acme", _one_match(), Decision.MATCH)], [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    conn.commit()

    served = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
    script = served[served.rindex("<script>"):served.rindex("</script>")]
    assert "button.apply-to" in script
    assert "/api/apply-to" in script
