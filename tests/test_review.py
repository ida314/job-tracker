"""The `/review` page: the uncertain rows worth a weekly skim, read across both surfaces.

What this file guards is that the page is a *read*: every row on it is also on exactly
one of the Company pages and the Job boards page, its search box drives nothing but its
own table, and the Today count is the length of the same list.
"""

import re
from types import SimpleNamespace

import pytest

from jobtracker import config, dashboard, queue, server, store
from jobtracker.criteria import load_criteria
from jobtracker.models import Company
from jobtracker.plugins import coverage_by_origin

TODAY = "2026-11-06"


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


COMPANIES = [
    Company(name="Acme", ats="greenhouse", slug="acme", tier=1, check_method="api"),
    Company(name="Target Tech", ats="workday", slug="target/wd5/x", tier=4,
            check_method="api", board_scope="mixed"),
]


def _post(conn, company, jid, title, first_seen=TODAY, origin=None, employer=None,
          verdict="uncertain", url=None, location="New York, NY"):
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, location, url, first_seen, "
        "last_seen, origin, employer) VALUES (?,?,?,?,?,?,?,?,?)",
        (company, jid, title, location, url or f"https://x.example/{jid}", first_seen,
         TODAY, origin, employer),
    )
    conn.execute(
        "INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by, "
        "decided_at) VALUES (?,?,?,?,?,?)",
        (company, jid, verdict, "r", "rules", TODAY),
    )


@pytest.fixture()
def conn():
    conn = store.connect(":memory:")
    _post(conn, "Acme", "new", "Software Engineer")
    _post(conn, "Acme", "old", "Platform Engineer", first_seen="2026-10-01")
    _post(conn, "Acme", "bar", "Barista")
    _post(conn, "Target Tech", "tt", "Store Team Member")
    _post(conn, "Simplify New-Grad-Positions", "s1", "Klaviyo — Software Engineer",
          origin="simplify", employer="Klaviyo")
    _post(conn, "Acme", "m", "Backend Engineer", verdict="match")
    conn.commit()
    yield conn
    conn.close()


def _page(conn, criteria):
    return server.render_review(conn, criteria, COMPANIES, coverage_by_origin(), TODAY)


def _markup(page):
    """The page without its stylesheet and script, which name classes they do not render."""
    page = page[:page.rindex("<script>")] if "<script>" in page else page
    return re.sub(r"<style>.*?</style>", "", page, flags=re.S)


def _tbody(markup, cls):
    """The one `<tbody>` whose class attribute is exactly `cls`."""
    found = re.findall(rf'<tbody class="{cls}"[^>]*>.*?</tbody>', markup, re.S)
    assert len(found) == 1, (cls, len(found))
    return found[0]


def _hrefs(markup):
    return re.findall(r'<tr data-lane="[^"]+" data-search="[^"]*"><td class="role">'
                      r'<a href="([^"]+)"', markup)


def _panel(doc, name):
    order = ("today", "applications", "all", "jobboards", "boards")
    start = doc.index(f'data-panel-body="{name}"')
    after = order[order.index(name) + 1:]
    ends = [doc.index(f'data-panel-body="{n}"') for n in after
            if f'data-panel-body="{n}"' in doc]
    return doc[start:min(ends)] if ends else doc[start:]


# -- a read across the partition ------------------------------------------------------
def test_review_rows_are_each_on_exactly_one_surface(conn, criteria):
    markup = _markup(_page(conn, criteria))
    hrefs = _hrefs(markup)
    # every open uncertain row, and nothing else
    assert len(hrefs) == 5 and len(set(hrefs)) == 5
    assert "Backend Engineer" not in markup

    company_pages = _panel(dashboard.build_dashboard(
        conn, COMPANIES, TODAY, criteria, interactive=True, include_job_boards=False),
        "all")
    job_boards = _markup(server.render_job_boards(conn, COMPANIES, TODAY, criteria))
    for href in hrefs:
        on = [name for name, page in (("company", company_pages), ("boards", job_boards))
              if f'href="{href}"' in page]
        assert len(on) == 1, (href, on)


def test_review_default_view_respects_window(conn, criteria):
    markup = _markup(_page(conn, criteria))
    recent = _tbody(markup, "recent")
    assert "Software Engineer</a>" in recent         # Acme, seen today
    assert "job=s1" in recent                        # the feed row, seen today
    assert "job=old" not in recent and "job=bar" not in recent and "job=tt" not in recent
    assert "job=old" in _tbody(markup, "grp older")
    # the heading counts the default view, not the whole list
    assert '<h1>Review <span class="count rvn">2</span>' in markup
    # feed first: lane order is the sort
    assert recent.index("job=s1") < recent.index("job=new")


def test_review_window_is_inclusive_and_unreadable_dates_are_old(criteria):
    rows = [
        {"company": "Acme", "ats_job_id": "a", "title": "Software Engineer",
         "first_seen": "2026-10-23", "location": ""},
        {"company": "Acme", "ats_job_id": "b", "title": "Software Engineer",
         "first_seen": "2026-10-22", "location": ""},
        {"company": "Acme", "ats_job_id": "c", "title": "Software Engineer",
         "first_seen": "garbage", "location": ""},
    ]
    split = queue.review(rows, criteria, {}, {}, TODAY)
    assert [p.row["ats_job_id"] for p in split.recent] == ["a"]
    assert sorted(p.row["ats_job_id"] for p in split.older) == ["b", "c"]
    assert queue.REVIEW_WINDOW_DAYS == 14


def test_review_folds_render_closed(conn, criteria):
    markup = _markup(_page(conn, criteria))
    folds = re.findall(r'<tbody class="([^"]+)"( data-closed="1")?>', markup)
    assert folds == [("recent", ""), ("grp older", ' data-closed="1"'),
                     ("grp nosignal", ' data-closed="1"'),
                     ("grp noise sub", ' data-closed="1"')]
    assert "1 older" in markup
    # the no-signal fold counts the rows nested inside it too
    assert "2 with no field signal" in markup
    noise = _tbody(markup, "grp noise sub")
    assert "1 from boards that list many non-field roles: Target Tech" in noise
    assert "job=tt" in noise and "job=bar" not in noise
    # group heads carry no data-search, so they stay out of "N of M shown"
    assert not re.search(r'<tr class="cohead"[^>]*data-search', markup)
    # the nested fold goes with its parent while that is collapsed
    assert ".js-groups tbody.grp.closed + tbody.grp.sub { display: none; }" in dashboard._CSS


def test_noise_with_no_plain_no_signal_rows_is_not_nested(criteria):
    conn = store.connect(":memory:")
    _post(conn, "Target Tech", "tt", "Store Team Member")
    conn.commit()
    markup = _markup(_page(conn, criteria))
    assert 'class="grp noise sub"' not in markup
    assert 'class="grp noise"' in markup
    assert "with no field signal" not in markup
    assert "from boards that list many non-field roles" in markup


def test_review_is_not_data_filterable(conn, criteria):
    """Never a `table[data-filterable]` beside another panel's: one scope, one table, one
    search box — and the review table appears on no other page."""
    markup = _markup(_page(conn, criteria))
    assert markup.count("data-filter-scope") == 1
    assert markup.count("<table") == 1 and markup.count("data-filterable") == 1
    scope = markup[markup.index("data-filter-scope"):]
    assert scope.index("<table") < scope.index("</section>")
    # search only: no tier chips, no ATS select, no location select
    assert 'data-f="q"' in scope
    assert "fchip" not in markup and 'data-f="ats"' not in markup
    assert 'data-f="loc"' not in markup

    for interactive in (True, False):
        doc = dashboard.build_dashboard(conn, COMPANIES, TODAY, criteria,
                                        interactive=interactive)
        assert 'class="rv"' not in doc
    assert 'class="rv"' not in server.render_job_boards(conn, COMPANIES, TODAY, criteria)


def test_review_escapes_titles_and_checks_url_schemes(criteria):
    conn = store.connect(":memory:")
    _post(conn, 'A&B "C"', "x<1>", '<img src=x onerror=alert(1)> Software Engineer',
          url="javascript:alert(1)", location="<b>NYC</b>")
    _post(conn, "Feed", "f1", "<i>Evil</i> — Software Engineer", origin="<s>bad</s>",
          employer="<i>Evil</i>", url="JavaScript:alert(2)")
    conn.commit()
    markup = _markup(_page(conn, criteria))
    assert "<img" not in markup and "&lt;img src=x" in markup
    assert "<b>NYC" not in markup and "<i>Evil" not in markup and "<s>bad" not in markup
    assert "javascript:" not in markup.lower()
    assert "company=A%26B%20%22C%22&amp;job=x%3C1%3E" in markup


def test_today_review_count_links_only_under_serve(conn, criteria):
    served = _panel(dashboard.build_dashboard(conn, COMPANIES, TODAY, criteria,
                                              interactive=True), "today")
    static = _panel(dashboard.build_dashboard(conn, COMPANIES, TODAY, criteria), "today")
    assert '<a href="/review">2 to review →</a>' in served
    assert "2 to review" in static and 'href="/review"' not in static
    # the static file never gets the page itself
    assert 'class="rv"' not in static
    # no criteria, no lanes: the line is absent rather than a guessed zero
    bare = dashboard.build_dashboard(conn, COMPANIES, TODAY, None)
    assert "to review" not in bare


def test_review_is_in_the_nav_after_job_boards():
    nav = server._NAV
    assert '<a href="/review">Review</a>' in nav
    assert nav.index('href="/jobboards"') < nav.index('href="/review"')


# -- rulings ---------------------------------------------------------------------------
def test_review_buttons_have_handlers_and_no_data_act(conn, criteria):
    """The parity rule: a button's handler ships in the page that renders it. The rulings
    are rendered by `dashboard`, so `dashboard._REVIEW_JS` is the script that must answer
    them — and it is on `/review` alone, never in the static file."""
    page = _page(conn, criteria)
    markup = _markup(page)
    script = page[page.rindex("<script>"):]
    rendered = set(re.findall(r'<button[^>]*class="([^"]+)"', markup))
    assert rendered == {"cotoggle", "rv-yes", "rv-no", "rv-undo"}
    rulings = {c for c in rendered if c.startswith("rv-")}
    assert set(re.findall(r"button\.(rv-[a-z]+)", script)) == rulings
    assert set(re.findall(r"button\.(rv-[a-z]+)", dashboard._REVIEW_JS)) == rulings
    assert "'/api/decision'" in script and "'/api/decision/clear'" in script
    # swaps in place, never reloads
    assert "location.reload" not in dashboard._REVIEW_JS
    # `data-act` keeps meaning exactly the three disposition buttons on a pick
    assert "data-act" not in markup

    # every row gets the rulings, the folds included: folding is not a judgement
    rows = re.findall(r"<tr data-lane=[^>]*>.*?</tr>", markup, re.S)
    assert len(rows) == 5
    for row in rows:
        assert row.count('class="rv-yes"') == 1 and row.count('class="rv-no"') == 1
        assert row.count('class="rv-undo"') == 2
    assert 'data-company="Target Tech" data-job="tt"' in _tbody(markup, "grp noise sub")
    # both chips start hidden, so the cell first shows the two buttons
    assert len(re.findall(r'<span class="rvdone" data-ruled="(match|reject)" hidden>',
                          markup)) == 10
    # a fold head spans the extra column
    assert '<th colspan="6" scope="colgroup">' in markup
    assert 'colspan="5"' not in markup

    # not in the static file, and not on the dashboard's own Uncertain tables
    def bare(doc):
        return re.sub(r"<(style|script)>.*?</\1>", "", doc, flags=re.S)

    for interactive in (True, False):
        doc = dashboard.build_dashboard(conn, COMPANIES, TODAY, criteria,
                                        interactive=interactive)
        assert "rv-" not in bare(doc) and "button.rv-yes" not in doc
    boards = server.render_job_boards(conn, COMPANIES, TODAY, criteria)
    assert "rv-" not in bare(boards) and "button.rv-yes" not in boards
    assert "rv-yes" not in dashboard._JS


def _db(tmp_path):
    path = tmp_path / "state.db"
    conn = store.connect(path)
    _post(conn, "Acme", "new", "Software Engineer")
    _post(conn, "Simplify New-Grad-Positions", "s1", "Klaviyo — Software Engineer",
          origin="simplify", employer="Klaviyo")
    conn.commit()
    conn.close()
    return path


def _handler(path):
    h = server.Handler.__new__(server.Handler)
    h.server = SimpleNamespace(db_path=path, criteria_path=config.CRITERIA_YAML)
    return h


def _tables(path):
    conn = store.connect(path)
    try:
        return tuple(
            [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("decisions", "overrides", "verdicts")
        )
    finally:
        conn.close()


def test_decision_clear_refuses_when_nothing_to_clear(tmp_path):
    path = _db(tmp_path)
    # a pin with no decision behind it: nothing here is a ruling this undo may remove
    conn = store.connect(path)
    store.set_override(conn, "Acme", "new", "match", TODAY, reason="manual")
    conn.commit()
    conn.close()
    before = _tables(path)

    res = _handler(path)._api_decision_clear({"company": "Acme", "ats_job_id": "new"})
    assert res["ok"] is False and "no ruling" in res["error"]
    res = _handler(path)._api_decision_clear({"company": "Acme", "ats_job_id": "nope"})
    assert res["ok"] is False
    assert _tables(path) == before


def test_decision_clear_never_clears_an_llm_override(tmp_path):
    """A decision recorded without a pin (`decide` without --pin), on a posting the model
    pinned. Undoing the decision removes the decision; the model's pin is not a ruling
    this undoes, and the verdict re-derived after it still follows that pin."""
    path = _db(tmp_path)
    conn = store.connect(path)
    store.record_decision(conn, "Acme", "new", "Software Engineer", "match", TODAY)
    store.set_override(conn, "Acme", "new", "reject", TODAY, reason="llm:senior",
                       decided_by="llm")
    conn.commit()
    conn.close()

    res = _handler(path)._api_decision_clear({"company": "Acme", "ats_job_id": "new"})
    assert res["ok"] is True and res["decisions"] == 0

    conn = store.connect(path)
    pin = conn.execute("SELECT decision, decided_by FROM overrides").fetchone()
    assert tuple(pin) == ("reject", "llm")
    v = conn.execute("SELECT verdict, decided_by FROM verdicts WHERE ats_job_id='new'"
                     ).fetchone()
    assert tuple(v) == ("reject", "llm")
    conn.close()


def test_a_human_ruling_undone_is_uncertain_again_and_unpinned(tmp_path):
    path = _db(tmp_path)
    before = _tables(path)
    h = _handler(path)
    assert h._api_decision({"company": "Acme", "ats_job_id": "new",
                            "decision": "reject"})["ok"]
    res = h._api_decision_clear({"company": "Acme", "ats_job_id": "new"})
    assert res["ok"] is True and res["verdict"] == "uncertain"
    after = _tables(path)
    assert after[0] == [] and after[1] == []
    # the verdict is the rules' again; the other row was never touched
    verdicts = {r[1]: r for r in after[2]}
    assert verdicts["new"][2] == "uncertain" and verdicts["new"][4] == "rules"
    assert verdicts["s1"] == {r[1]: r for r in before[2]}["s1"]


def test_rule_undo_rule_leaves_exactly_one_decision(tmp_path):
    """The done condition: a click, its undo, then the other button."""
    path = _db(tmp_path)
    h = _handler(path)
    ident = {"company": "Acme", "ats_job_id": "new"}
    assert h._api_decision({**ident, "decision": "match"})["decisions"] == 1
    assert h._api_decision_clear(ident)["decisions"] == 0
    assert h._api_decision({**ident, "decision": "reject"})["decisions"] == 1
    decisions, overrides, _ = _tables(path)
    assert [(d[0], d[1], d[4]) for d in decisions] == [("Acme", "new", "reject")]
    assert [(o[0], o[1], o[2]) for o in overrides] == [("Acme", "new", "reject")]


def test_ruled_row_leaves_review_on_next_render(tmp_path, criteria):
    path = _db(tmp_path)
    conn = store.connect(path)
    assert "job=new" in _markup(_page(conn, criteria))
    conn.close()

    assert _handler(path)._api_decision({"company": "Acme", "ats_job_id": "new",
                                         "decision": "match"})["ok"]
    conn = store.connect(path)
    markup = _markup(_page(conn, criteria))
    assert "job=new" not in markup and "job=s1" in markup
    conn.close()

    # and an undo puts it back
    assert _handler(path)._api_decision_clear({"company": "Acme",
                                               "ats_job_id": "new"})["ok"]
    conn = store.connect(path)
    assert "job=new" in _markup(_page(conn, criteria))
    conn.close()


def test_match_ruling_feeds_eval(tmp_path, criteria):
    """The reason the buttons exist: a ruling is a row in the corpus `eval` replays."""
    from jobtracker import tuning

    path = _db(tmp_path)
    assert _handler(path)._api_decision({"company": "Acme", "ats_job_id": "new",
                                         "decision": "match"})["ok"]
    conn = store.connect(path)
    report = tuning.evaluate(store.all_decisions(conn), criteria)
    conn.close()
    assert report.total == 1
    # the rules still say uncertain: unresolved, never a regression
    assert report.ok and len(report.unresolved) == 1


def test_review_page_survives_an_empty_corpus(criteria):
    markup = _markup(_page(store.connect(":memory:"), criteria))
    assert "Nothing uncertain is open" in markup
    assert "<table" not in markup
