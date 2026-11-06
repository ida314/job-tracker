"""Lanes on the existing Uncertain tables: a Status column and a trailing fold.

The tables stay the full record of the uncertain list — nothing is removed and the
company-pages / job-boards partition does not move. What changes is the words in one
column, and that the rows `queue.lane` folds render in one trailing group, collapsed by
the script like any company group and force-expanded by any filter.
"""

import re

import pytest

from jobtracker import config, dashboard, queue, store
from jobtracker.criteria import load_criteria
from jobtracker.models import Company, Decision, Posting, Verdict
from jobtracker.queue import Lane

TODAY = "2026-11-06"
GROUP = "Simplify New-Grad-Positions"
REASON = "no_level_token_in_title"


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


ACME = Company(name="Acme", ats="greenhouse", slug="acme", tier=1, check_method="api")
TARGET = Company(name="Target Tech", ats="workday", slug="target/wd5/x", tier=4,
                 check_method="api", board_scope="mixed")


def _uncertain(conn, company, *jobs):
    """`jobs` is `(jid, title)` pairs, synced in one call: a board sync is a complete
    statement, so syncing them one at a time would close all but the last."""
    store.sync_postings(
        conn, company,
        [Posting(company, jid, title, f"https://x/{jid}", "New York, NY")
         for jid, title in jobs],
        TODAY)
    for jid, _ in jobs:
        store.record_verdict(
            conn, Verdict(company, jid, Decision.UNCERTAIN, REASON, "rules"), TODAY)


def _board_uncertain(conn, jid, title, employer="Klaviyo"):
    store.append_postings(
        conn, GROUP,
        [Posting(GROUP, jid, f"{employer} — {title}", f"https://x/{jid}", "New York, NY",
                 employer=employer, description="d")],
        TODAY, origin="simplify",
    )
    store.record_verdict(conn, Verdict(GROUP, jid, Decision.UNCERTAIN, REASON, "rules"),
                         TODAY)


def _corpus():
    """One row in every lane, on both surfaces."""
    conn = store.connect(":memory:")
    _uncertain(conn, "Acme",
               ("1", "Backend Software Engineer"),       # signal
               ("2", "Platform Software Engineer"),      # read, unclear
               ("3", "Field Marketer"))                  # no_signal
    store.record_level_reading(conn, "Acme", "2", "unclear", "", "abcd", TODAY)
    _uncertain(conn, "Target Tech", ("4", "Store Team Member"))   # noise
    _board_uncertain(conn, "b1", "Software Engineer")               # feed
    _board_uncertain(conn, "b2", "Barista")                         # no_signal
    conn.commit()
    return conn


def _panel(doc, name):
    order = ("today", "applications", "all", "jobboards", "boards")
    start = doc.index(f'data-panel-body="{name}"')
    ends = [doc.index(f'data-panel-body="{n}"') for n in order[order.index(name) + 1:]
            if f'data-panel-body="{n}"' in doc]
    return doc[start:min(ends)] if ends else doc[start:]


def _uncertain_table(panel):
    start = panel.index("Uncertain — needs a human")
    return panel[start:panel.index("</table>", start)]


def _row(table, title):
    """The `<tr>` holding this title."""
    i = table.index(f">{title}</a>")
    return table[table.rindex("<tr", 0, i):table.index("</tr>", i)]


def _fold(table):
    start = table.index('<tbody class="grp fold"')
    return table[start:table.index("</tbody>", start)]


# -- the Status column --------------------------------------------------------------
def test_uncertain_status_column_shows_lane_label(criteria):
    doc = dashboard.build_dashboard(_corpus(), [ACME, TARGET], TODAY, criteria)
    company = _uncertain_table(_panel(doc, "all"))
    boards = _uncertain_table(_panel(doc, "jobboards"))

    for table in (company, boards):
        assert "<th>Status</th>" in table and "Why uncertain" not in table

    expect = {
        (company, "Backend Software Engineer"): queue.label(Lane.SIGNAL),
        (company, "Platform Software Engineer"): queue.label(Lane.READ_UNCLEAR),
        (company, "Field Marketer"): queue.label(Lane.NO_SIGNAL),
        (company, "Store Team Member"): queue.label(Lane.BOARD_NOISE),
        (boards, "Software Engineer"): queue.label(Lane.FEED, "entry"),
        (boards, "Barista"): queue.label(Lane.NO_SIGNAL),
    }
    for (table, title), label in expect.items():
        row = _row(table, title)
        # The lane's words are the cell's text; the raw reason is one hover away.
        assert f'<td class="why" title="{REASON}">{label}</td>' in row, title
    assert f">{REASON}<" not in company + boards


def test_without_criteria_the_raw_reason_shows_and_nothing_folds():
    """No criteria means no field signal to read. The table says what the rules said
    rather than guessing a lane, and folds nothing."""
    doc = dashboard.build_dashboard(_corpus(), [ACME, TARGET], TODAY)
    table = _uncertain_table(_panel(doc, "all"))
    assert f'<td class="why">{REASON}</td>' in table
    assert "grp fold" not in table
    assert "needs a human read" in _panel(doc, "all")


# -- the fold -----------------------------------------------------------------------
@pytest.mark.parametrize("interactive", [False, True])
def test_no_signal_group_renders_open_and_carries_no_data_search_on_its_head(
        criteria, interactive):
    doc = dashboard.build_dashboard(_corpus(), [ACME, TARGET], TODAY, criteria,
                                    interactive=interactive)
    company = _uncertain_table(_panel(doc, "all"))
    boards = _uncertain_table(_panel(doc, "jobboards"))

    for table, folded, kept in (
        (company, ("Field Marketer", "Store Team Member"),
         ("Backend Software Engineer", "Platform Software Engineer")),
        (boards, ("Barista",), ("Software Engineer",)),
    ):
        fold = _fold(table)
        # Trailing: nothing renders after it.
        assert table.rindex("<tbody") == table.index('<tbody class="grp fold"')
        # Open in the HTML; the script collapses it on load, never the reverse.
        tag = fold[:fold.index(">") + 1]
        assert "closed" not in tag and "hidden" not in tag
        assert "<tr hidden" not in fold
        head = fold[fold.index('class="cohead"'):fold.index("</th></tr>")]
        assert "data-search" not in head
        assert f"No field signal —</span><span class=\"n\">{len(folded)} " in head
        assert 'class="cotoggle"' in head
        # A data row carries data-search; every folded row is one.
        assert fold.count("data-search=") == len(folded)
        for title in folded:
            assert f">{title}</a>" in fold
        for title in kept:
            assert f">{title}</a>" not in fold and f">{title}</a>" in table

    # The fold inside the fold: a mixed board's noise comes after plain no-signal.
    fold = _fold(company)
    assert fold.index("Field Marketer") < fold.index("Store Team Member")
    # The company head is not above a folded row, so it rides beside the role.
    assert '<span class="cobadge">Target Tech</span>' in _row(company, "Store Team Member")


def test_the_fold_spans_the_columns_the_header_has(criteria):
    for interactive in (False, True):
        doc = dashboard.build_dashboard(_corpus(), [ACME, TARGET], TODAY, criteria,
                                        interactive=interactive)
        for name in ("all", "jobboards"):
            table = _uncertain_table(_panel(doc, name))
            header = table[table.index("<thead>"):table.index("</thead>")]
            span = int(re.search(r'colspan="(\d+)"', _fold(table)).group(1))
            assert span == len(re.findall(r"<th[ >]", header)), (name, interactive)


def test_filter_expands_no_signal_group(criteria):
    """The fold is a `grp` like any company's, so the one loop that collapses groups on
    load and force-expands them under a filter reaches it with no second rule."""
    doc = dashboard.build_dashboard(_corpus(), [ACME, TARGET], TODAY, criteria)
    for name in ("all", "jobboards"):
        table = _uncertain_table(_panel(doc, name))
        assert "data-filterable" in _panel(doc, name)
        assert '<tbody class="grp fold"' in table
    js = dashboard._JS
    assert "if (b.classList.contains('grp')) b.dataset.closed = '1';" in js
    assert "var filtering = !!(text || ats || locs || pressed);" in js
    assert "b.dataset.closed === '1' && !filtering" in js
    # The count the script rewrites says rows here, roles on a company.
    assert "b.dataset.noun || 'role'" in js


def test_static_and_served_fold_the_same_rows(criteria):
    conn = _corpus()
    static = dashboard.build_dashboard(conn, [ACME, TARGET], TODAY, criteria)
    served = dashboard.build_dashboard(conn, [ACME, TARGET], TODAY, criteria,
                                       interactive=True)
    page = "\n".join(dashboard.job_board_page_parts(conn, [ACME, TARGET], TODAY, criteria))

    def titles(fold):
        return re.findall(r">([^<>]+)</a>", fold)

    for name in ("all", "jobboards"):
        assert (titles(_fold(_uncertain_table(_panel(static, name))))
                == titles(_fold(_uncertain_table(_panel(served, name)))))
    assert (titles(_fold(_uncertain_table(page)))
            == titles(_fold(_uncertain_table(_panel(static, "jobboards")))))


def test_folding_removes_no_row_and_keeps_the_partition(criteria):
    conn = _corpus()
    doc = dashboard.build_dashboard(conn, [ACME, TARGET], TODAY, criteria)
    company = _uncertain_table(_panel(doc, "all"))
    boards = _uncertain_table(_panel(doc, "jobboards"))
    assert company.count("data-search=") == 4
    assert boards.count("data-search=") == 2
    assert 'id="uncertain-count">4<' in company
    assert 'id="buncertain-count">2<' in boards


# -- the tile -----------------------------------------------------------------------
def _tile(panel, key):
    i = panel.index(f'<div class="k">{key}</div>')
    return panel[i:panel.index('</div></div>', i)]


def test_uncertain_tile_count_unchanged(criteria):
    """The count is the whole company-pages list, folded rows included; the sub-line
    counts the lanes worth a read."""
    doc = dashboard.build_dashboard(_corpus(), [ACME, TARGET], TODAY, criteria)
    tile = _tile(_panel(doc, "all"), "Uncertain")
    assert '<div class="v">4</div>' in tile
    # Acme 1 (signal) and 2 (read). The folds are not worth a read, and the job boards
    # rows are not on this tab's count.
    assert '<div class="n">2 worth a read' in tile


def test_worth_a_read_is_the_complement_of_the_fold():
    assert dashboard._WORTH_A_READ == {Lane.FEED, Lane.READ_UNCLEAR, Lane.SIGNAL}
    assert not dashboard._WORTH_A_READ & queue.FOLDED
