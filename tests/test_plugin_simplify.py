"""The Simplify listings board.

Read against a real capture of the repo's `listings.json` (five entries picked out of
20,138 on 2026-09-15, one of each shape that matters), because the failures worth
catching here are shape changes in someone else's file. Assertions are derived from the
fixture rather than hardcoded, so refreshing the capture cannot quietly turn a test
green against values nobody checked.
"""

import json
import pathlib

import pytest

from jobtracker.plugins import get_plugin

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "simplify_listings.json"


@pytest.fixture(scope="module")
def listings():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def plugin():
    return get_plugin("simplify")


def _open(entry):
    return bool(entry.get("active")) and bool(entry.get("is_visible", True))


def test_the_capture_still_holds_every_shape_this_board_produces(listings):
    """A guard on the fixture itself: refresh it carelessly and the tests below would
    pass while covering one case."""
    assert any(_open(e) for e in listings)
    assert any(not e.get("active") for e in listings)
    assert any(e.get("active") and not e.get("is_visible") for e in listings)
    assert any(len(e.get("locations") or []) > 1 for e in listings)


def test_only_what_the_board_still_publishes_is_imported(plugin, listings):
    postings, unparsed, skipped = plugin.parse_page("Feed", listings, {}, "2026-09-15")
    expected = [e for e in listings if _open(e)]
    assert [p.ats_job_id for p in postings] == [str(e["id"]) for e in expected]
    assert skipped == len(listings) - len(expected)
    assert unparsed == 0


def test_the_employer_is_a_field_and_still_leads_the_title(plugin, listings):
    """Titles keep the "Employer — Role" shape that criteria tokens, `decisions.title`
    and the eval corpus all read. The column is the same fact for the pages to render."""
    postings, _, _ = plugin.parse_page("Feed", listings, {}, "2026-09-15")
    for posting in postings:
        assert posting.employer
        assert posting.title.startswith(f"{posting.employer} — ")
        assert posting.company == "Feed"  # the group, never the employer


def test_a_listing_links_to_the_employers_own_application_page(plugin, listings):
    """The reason this reads JSON rather than the README: the link is the employer's
    own, so a row can meet its board's row under one dedupe key."""
    from jobtracker import dedupe

    postings, _, _ = plugin.parse_page("Feed", listings, {}, "2026-09-15")
    for posting in postings:
        assert posting.url.startswith("https://")
        assert "simplify.jobs" not in posting.url
        assert dedupe.dedupe_key(posting.url)


def test_dates_are_real_days_and_locations_survive_intact(plugin, listings):
    postings, _, _ = plugin.parse_page("Feed", listings, {}, "2026-09-15")
    by_id = {str(e["id"]): e for e in listings}
    for posting in postings:
        entry = by_id[posting.ats_job_id]
        assert posting.posted_on and len(posting.posted_on) == 10
        assert posting.posted_on <= "2026-09-15"
        assert posting.location == "; ".join(entry["locations"])


def test_every_posting_arrives_with_something_to_read(plugin, listings):
    """A NULL description drops a row out of `level`'s queue and out of
    `matches_needing_judgment` — in the table, absent from the product. The board ships
    no prose, so the description is assembled from what it does say."""
    postings, _, _ = plugin.parse_page("Feed", listings, {}, "2026-09-15")
    for posting in postings:
        assert "Category:" in posting.description


def test_a_withdrawn_or_retracted_listing_is_named_as_closed(plugin, listings):
    """`active` is the board retracting a req and `is_visible` is it withdrawing one.
    Both mean it is no longer published, and both beat waiting ninety days for age."""
    closed = plugin.closed_ids(listings)
    assert closed == [str(e["id"]) for e in listings if not _open(e)]
    assert closed


def test_an_empty_listing_is_health_business_not_a_parse_error(plugin):
    """Zero rows on a well-formed page is allowed through; `evaluate_plugin` flags an
    empty snapshot every run. Deciding it here would make two policies out of one."""
    assert plugin.page_error([]) is None


@pytest.mark.parametrize("payload", [
    {"jobs": []},                                   # the shape a rewrite would produce
    "not json at all",
    [{"company_name": "Acme", "title": "SWE"}],     # listings with no id
])
def test_a_payload_that_is_not_the_listing_is_a_failure_not_an_empty_board(plugin, payload):
    """Every one of these parses to zero rows under a tolerant reader, which is exactly
    how a board that emptied looks. `sync`-style closure is not reachable from here, but
    reporting OK on a rename would still hide the board going dark."""
    assert plugin.page_error(payload) is not None


def test_the_board_is_one_page_and_keeps_no_cursor(plugin):
    urls = plugin.page_urls({}, "2026-09-15")
    assert len(urls) == 1 and urls[0].endswith("listings.json")
    assert "whole listing" in plugin.describe_cursor("anything")


def test_the_group_name_is_the_one_the_old_feed_wrote_under(plugin):
    """`postings` is keyed by (company, ats_job_id), and every verdict, override,
    decision and application recorded against this feed hangs off that pair."""
    assert plugin.company({}).name == "Simplify New-Grad-Positions"


def test_installing_it_never_starts_reading_anything(plugin):
    assert plugin.defaults()["enabled"] is False
