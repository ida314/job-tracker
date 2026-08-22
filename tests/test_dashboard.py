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


def test_a_hostile_posting_url_cannot_smuggle_a_scheme():
    """Every href on this page goes through `_safe_url`, and the URLs come from ATS APIs.

    This used to be asserted against the viewer link, which is gone — the window is not
    something you are pointed at any more. The rule it was guarding is not: an Apply
    button whose href is `javascript:` executes on click.
    """
    hostile = Posting("Acme", "1", "Backend Engineer, New Grad",
                      "javascript:alert(1)", "New York, NY")
    conn, companies = _setup([("Acme", hostile, Decision.MATCH)],
                             [_company("Acme", 1)])
    _ranked(conn, "Acme", "1", 88.5)
    conn.commit()

    doc = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
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


# -- the Applications tab ------------------------------------------------------------
# The read-only mirror of /applications. Its whole value is that the mailed snapshot
# still carries the record, so the assertions here are about what must NOT be in it.
def _applied(conn, company="Acme", jid="1", title="SWE", status="applied",
             at="2026-07-22T09:00:00", **kw):
    store.advance_application(conn, company, jid, title, status, at, **kw)


def test_applications_panel_renders_server_side():
    conn, companies = _setup([], [_company("Acme", 1)])
    _applied(conn, url="https://acme.example/1", location="New York, NY")
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert 'data-panel-body="applications"' in doc
    assert "New York, NY" in doc
    # And it is a real tab, counted like the others.
    assert 'data-panel="applications"' in doc


def test_applications_panel_is_not_filterable():
    """The filter JS selects table[data-filterable]. A tier or location filter left set
    on the All postings tab would otherwise silently empty the list of things you
    actually did — the same trap the picks are protected from."""
    conn, companies = _setup([], [_company("Acme", 1)])
    _applied(conn)
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    panel = doc[doc.index('data-panel-body="applications"'):
                doc.index('data-panel-body="all"')]
    assert "data-filterable" not in panel


def test_static_file_carries_no_application_controls():
    """A button in a file:// page has nothing to POST to. Editing lives at
    /applications, which only `serve` can answer."""
    conn, companies = _setup([], [_company("Acme", 1)])
    _applied(conn)
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "/api/application" not in doc
    assert "/api/mail" not in doc
    for cls in ("app-add", "app-save", "app-meta", "app-delete",
                "app-accept", "app-dismiss"):
        assert cls not in doc
    # Under serve there IS somewhere to send you, and only then.
    live = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
    assert 'href="/applications"' in live
    assert 'href="/applications"' not in doc


def test_a_manual_entry_is_escaped_everywhere():
    """Unlike a posting, this text was typed by a human into a form — but it lands in
    the same HTML, so it gets the same treatment."""
    conn, companies = _setup([], [])
    _applied(conn, company='<script>alert("x")</script>', jid="manual:evil",
             title='"><img src=x onerror=alert(1)>', note="<b>note</b>",
             url="javascript:alert(1)", source="manual")
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "<script>alert" not in doc
    assert "onerror=alert(1)>" not in doc
    assert "<b>note</b>" not in doc
    assert doc.count("<script>") == 1  # only the page's own


def test_a_manual_entry_with_no_link_is_not_a_dead_anchor():
    conn, companies = _setup([], [])
    _applied(conn, jid="manual:x", title="Referral Role", source="manual")
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    panel = doc[doc.index('data-panel-body="applications"'):
                doc.index('data-panel-body="all"')]
    assert "Referral Role" in panel
    assert 'href="#"' not in panel


def test_repeated_interviews_show_a_count_and_a_history():
    conn, companies = _setup([], [_company("Acme", 1)])
    for at, note in (("2026-07-01T09:00:00", ""), ("2026-07-10T09:00:00", "round 1"),
                     ("2026-07-18T09:00:00", "round 2")):
        status = "applied" if not note else "interview"
        store.advance_application(conn, "Acme", "1", "SWE", status, at, note=note)
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "interview ×2" in doc
    assert "History (3)" in doc
    assert "round 2" in doc


def test_an_empty_tracker_still_renders_the_tab():
    conn, companies = _setup([], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert 'data-panel-body="applications"' in doc
    assert "Nothing recorded yet" in doc


def test_rendering_applications_writes_nothing():
    """Same rule as the rest of the page: opening a view of your data must not mutate
    it. `report` marks manual companies as surfaced; this must not."""
    conn, companies = _setup([], [_company("Acme", 1)])
    _applied(conn)
    before = (conn.execute("SELECT * FROM applications").fetchall(),
              conn.execute("SELECT COUNT(*) n FROM application_events").fetchone()["n"])
    dashboard.build_dashboard(conn, companies, "2026-07-22")
    after = (conn.execute("SELECT * FROM applications").fetchall(),
             conn.execute("SELECT COUNT(*) n FROM application_events").fetchone()["n"])
    assert [dict(r) for r in before[0]] == [dict(r) for r in after[0]]
    assert before[1] == after[1]


# -- grouping the postings tables by company ----------------------------------------
def _matches(*specs):
    """[(company, job_id, title, location)] -> a conn with those open matches."""
    conn = store.connect(":memory:")
    by_company = {}
    for company, jid, title, loc in specs:
        by_company.setdefault(company, []).append(
            Posting(company, jid, title, f"https://x/{jid}", loc))
    for company, postings in by_company.items():
        store.sync_postings(conn, company, postings, "2026-07-01")
        for p in postings:
            store.record_verdict(
                conn, Verdict(company, p.ats_job_id, Decision.MATCH, "why", "rules"),
                "2026-07-01")
    conn.commit()
    return conn


def _all_panel(doc):
    return doc[doc.index('data-panel-body="all"'):doc.index('data-panel-body="boards"')]


def test_open_matches_are_grouped_one_tbody_per_company():
    conn = _matches(("Acme", "1", "Backend Engineer", "New York, NY"),
                    ("Acme", "2", "Platform Engineer", "New York, NY"),
                    ("Zeta", "3", "Infra Engineer", "Austin, TX"))
    panel = _all_panel(dashboard.build_dashboard(
        conn, [_company("Acme", 1), _company("Zeta", 3)], "2026-07-22"))
    assert panel.count('<tbody class="grp">') == 2
    assert panel.count('class="cohead"') == 2
    assert ">2 roles<" in panel and ">1 role<" in panel


def test_every_grouped_posting_is_rendered_visible_so_the_page_works_without_js():
    """The collapse is JS-side only. With JS off you get a caption and every row under
    it — the same bargain the tabs make."""
    conn = _matches(("Acme", "1", "Backend Engineer", "New York, NY"),
                    ("Acme", "2", "Platform Engineer", "New York, NY"))
    panel = _all_panel(dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22"))
    assert panel.count("data-search=") == 2
    assert "<tr hidden" not in panel and "<tbody hidden" not in panel
    assert 'class="grp closed"' not in panel


def test_the_group_toggle_is_hidden_until_js_confirms_it_is_running():
    """A button that cannot collapse anything is a dead control in a mailed file."""
    assert ".cotoggle { display: none; }" in dashboard._CSS
    assert ".js-groups .cotoggle" in dashboard._CSS
    assert "classList.add('js-groups')" in dashboard._JS


def test_a_group_head_is_not_counted_as_a_posting():
    """"N of M shown" means postings. Group heads carry no data-search, which is what
    keeps them out of it — and keeps the existing numbers where they were."""
    conn = _matches(("Acme", "1", "Backend Engineer", "New York, NY"))
    panel = _all_panel(dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22"))
    head = panel[panel.index('class="cohead"'):panel.index("</th></tr>")]
    assert "data-search" not in head
    assert "data-tier=" not in head
    assert "tr[data-search]" in dashboard._JS


def test_the_filter_counts_every_group_not_only_the_first():
    """`tBodies[0]` filtered one company and made the denominator that company's size."""
    assert "tBodies[0].rows" not in dashboard._JS
    assert "rowsOf(t)" in dashboard._JS


def test_a_filter_expands_the_groups_it_matches():
    """A collapsed page under a typed search reads as "nothing found", which is the one
    thing this page may never say while it is holding rows that match."""
    assert "var filtering = !!(text || ats || locs || tiers);" in dashboard._JS
    assert "b.dataset.closed === '1' && !filtering" in dashboard._JS


def test_a_row_still_matches_a_search_for_its_company_after_the_cell_moved():
    conn = _matches(("Acme", "1", "Backend Engineer", "New York, NY"))
    panel = _all_panel(dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22"))
    assert 'data-search="acme backend engineer new york, ny 1 greenhouse"' in panel


def test_a_hostile_company_name_is_escaped_in_the_group_head():
    conn = _matches(('<b>Ev"il</b>', "1", "Backend Engineer", "NYC"))
    doc = dashboard.build_dashboard(conn, [_company('<b>Ev"il</b>', 1)], "2026-07-22")
    assert "<b>Ev" not in doc
    assert "&lt;b&gt;Ev" in doc


# -- the rest of the ranking --------------------------------------------------------
def _ranked_pool(n=6):
    specs = [("Acme" if i % 2 else "Zeta", str(i), f"Engineer {i}", "New York, NY")
             for i in range(1, n + 1)]
    conn = _matches(*specs)
    for i in range(1, n + 1):
        _ranked(conn, "Acme" if i % 2 else "Zeta", str(i), 100.0 - i)
    conn.commit()
    return conn


def _today_panel(doc):
    return doc[doc.index('data-panel-body="today"'):
               doc.index('data-panel-body="applications"')]


def test_the_rest_of_the_ranking_is_reachable_from_the_today_tab():
    conn = _ranked_pool(6)
    panel = _today_panel(dashboard.build_dashboard(
        conn, [_company("Acme", 1), _company("Zeta", 3)], "2026-07-22"))
    assert "The rest of the ranking" in panel
    assert "3 roles at 2 companies" in panel
    # The real position in the ranking, not a per-company counter.
    assert '<span class="rn">4</span>' in panel
    assert '<span class="rn">6</span>' in panel


def test_the_rest_of_the_ranking_needs_no_js_and_is_not_filterable():
    conn = _ranked_pool(6)
    panel = _today_panel(dashboard.build_dashboard(
        conn, [_company("Acme", 1), _company("Zeta", 3)], "2026-07-22"))
    assert "<details class=\"rest\">" in panel
    assert "data-filterable" not in panel


def test_the_rest_of_the_ranking_carries_no_buttons_in_either_mode():
    """A pick is what has buttons. Anything else would put `.pick [data-act]` on more
    than the three cards the disposition handler is written for."""
    conn = _ranked_pool(6)
    for interactive in (False, True):
        doc = dashboard.build_dashboard(conn, [_company("Acme", 1), _company("Zeta", 3)],
                                        "2026-07-22", interactive=interactive)
        rest = doc[doc.index('<details class="rest">'):doc.index("</details>")]
        assert "<button" not in rest
        assert "data-act" not in rest


def test_the_rest_of_the_ranking_excludes_what_you_applied_to_or_skipped():
    """It has to come out of the same filtered list the picks did, or a job you applied
    to this morning reappears on the page it left."""
    conn = _ranked_pool(6)
    store.record_application(conn, "Acme", "5", "Engineer 5", "applied",
                            "2026-07-22T09:00:00")
    store.set_deferral(conn, "Zeta", "6", "skipped", "2026-07-22")
    conn.commit()
    panel = _today_panel(dashboard.build_dashboard(
        conn, [_company("Acme", 1), _company("Zeta", 3)], "2026-07-22"))
    assert "Engineer 5" not in panel
    assert "Engineer 6" not in panel
    assert "1 role at 1 company" in panel


def test_no_drawer_when_there_is_nothing_below_the_picks():
    conn = _ranked_pool(3)
    panel = _today_panel(dashboard.build_dashboard(conn, [_company("Acme", 1),
                                                          _company("Zeta", 3)],
                                                   "2026-07-22"))
    assert "The rest of the ranking" not in panel


# -- per-posting resume and the rebuild button --------------------------------------
def _one_ranked(conn=None):
    conn = conn or _matches(("Acme", "1", "Backend Engineer", "New York, NY"))
    _ranked(conn, "Acme", "1", 90.0)
    conn.commit()
    return conn


def test_the_prefill_line_is_two_text_nodes_so_the_script_never_writes_markup():
    conn = _one_ranked()
    store.record_plan(conn, "Acme", "1", "[]", 16, 3, "h", "2026-07-22")
    conn.commit()
    doc = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22")
    assert '<span class="counts">prefill 13/16 fields</span>' in doc
    assert '<span class="tail need"> · 3 need you</span>' in doc
    # The phrases the older tests pin are still contiguous.
    assert "prefill 13/16 fields" in doc and "3 need you" in doc


def test_the_rebuild_and_resume_controls_exist_only_under_serve():
    conn = _one_ranked()
    static = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22")
    live = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22",
                                     interactive=True)
    # The rendered control, not the class name: `dashboard._JS` names these selectors on
    # every page, and the script is not the button.
    for markup in ('class="pick-rebuild"', 'class="pick-attach"', 'class="pickfile"'):
        assert markup not in static, markup
        assert markup in live, markup
    # The name of the file that will be attached is useful offline, so it renders in both.
    assert "resume: the one in Settings" in static
    assert "resume: the one in Settings" in live


def test_the_pick_controls_have_their_handlers_on_the_page_that_renders_them():
    """The regression this repo already shipped: a button rendered by one file with its
    handler in another file's script, so every click did nothing at all."""
    conn = _one_ranked()
    doc = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22",
                                    interactive=True)
    script = doc[doc.rindex("<script>"):doc.rindex("</script>")]
    for cls in ("pick-rebuild", "pick-attach", "pick-detach"):
        assert f"button.{cls}" in script, cls
    for endpoint in ("/api/prefill", "/api/posting-resume", "/api/posting-resume/clear"):
        assert endpoint in script, endpoint
    assert doc.count("<script>") == 1


def test_a_posting_with_its_own_resume_says_so_in_both_modes():
    conn = _one_ranked()
    store.set_posting_resume(conn, "Acme", "1", "acme_1_ab12cd34.pdf", 1234, "2026-07-22")
    conn.commit()
    for interactive in (False, True):
        doc = dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-07-22",
                                        interactive=interactive)
        assert "resume for this posting: acme_1_ab12cd34.pdf" in doc
    # Only the live page offers to undo it.
    assert "pick-detach" in dashboard.build_dashboard(
        conn, [_company("Acme", 1)], "2026-07-22", interactive=True)


# -- the inbox banner ---------------------------------------------------------------
def _pending_mail(conn):
    conn.execute(
        "INSERT INTO mail_candidates (message_id, company, ats_job_id, choices, "
        "match_kind, subject, scanned_at) VALUES ('m1','Acme','1','[\"1\"]','sole_open',"
        "'Your application','2026-07-22')")
    store.record_mail_proposal(conn, "m1", "Acme", "1", "screen", "quote", "2026-07-22")
    conn.commit()


def test_the_static_file_counts_pending_mail_but_links_nowhere():
    """A file:// page pointing at a server that may not be running is worse than no
    link. The count still earns its place — it says whether opening the app is worth it."""
    conn, companies = _setup([], [_company("Acme", 1)])
    _pending_mail(conn)
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "1 email suggests an application moved" in doc
    assert 'href="/applications"' not in doc
    assert "jobtracker mail" in doc

    live = dashboard.build_dashboard(conn, companies, "2026-07-22", interactive=True)
    assert 'href="/applications"' in live


def test_the_dashboard_says_nothing_about_mail_when_there_is_nothing_to_review():
    """A permanent zero-state line stops being read long before it has anything to say."""
    conn, companies = _setup([], [_company("Acme", 1)])
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "banner mail" not in doc


def test_a_dismissed_proposal_leaves_the_banner():
    conn, companies = _setup([], [_company("Acme", 1)])
    _pending_mail(conn)
    store.resolve_mail_proposal(conn, "m1", "dismissed", "2026-07-23")
    conn.commit()
    doc = dashboard.build_dashboard(conn, companies, "2026-07-22")
    assert "banner mail" not in doc
