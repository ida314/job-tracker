"""The Y Combinator jobs board.

Read against a trimmed capture of the real page (2026-09-15), because everything worth
catching here is a property of someone else's HTML. Two of these tests are about the two
ways this board can lie to us: a page that renders without its payload, and an apply link
that is the same string on every job.
"""

import pathlib

import pytest

from jobtracker.plugins import get_plugin

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "yc_jobs_page.html"
TODAY = "2026-09-15"


@pytest.fixture(scope="module")
def page():
    return FIXTURE.read_text()


@pytest.fixture(scope="module")
def plugin():
    return get_plugin("ycombinator")


def test_the_listing_is_read_out_of_the_pages_own_payload(plugin, page):
    postings, unparsed, skipped = plugin.parse_page("YC", page, {}, TODAY)
    assert len(postings) == 3 and unparsed == 0 and skipped == 0
    assert plugin.page_error(page) is None
    assert plugin.page_ids(page) == ["97376", "59310", "109178"]


def test_a_row_links_to_the_job_and_never_to_the_shared_login_url(plugin, page):
    """Every YC job carries the *same* `applyUrl`, an account.ycombinator.com login link.

    Stored as the posting's URL, every row on this board would normalize to one dedupe
    key: the ranking would collapse the whole board to a single pick, and the page would
    claim they were all the same req. The job's own path is unique, so that is what is
    stored — and it is what makes the key unique without a special extractor.
    """
    from jobtracker import dedupe

    postings, _, _ = plugin.parse_page("YC", page, {}, TODAY)
    keys = set()
    for posting in postings:
        assert posting.url.startswith("https://www.ycombinator.com/companies/")
        assert "account.ycombinator.com" not in posting.url
        keys.add(dedupe.dedupe_key(posting.url))
    assert len(keys) == 3


def test_the_employer_is_a_field_and_still_leads_the_title(plugin, page):
    postings, _, _ = plugin.parse_page("YC", page, {}, TODAY)
    first = postings[0]
    assert first.employer == "Scispot"
    assert first.title == "Scispot — Senior Backend & Infrastructure Engineer"
    assert first.company == "YC"  # the group, never the employer


def test_relative_ages_become_real_days_and_locations_are_split(plugin, page):
    """YC dates its listings "3 months" and "5 days", never as a timestamp."""
    postings, _, _ = plugin.parse_page("YC", page, {}, TODAY)
    by_employer = {p.employer: p for p in postings}
    assert by_employer["SalaryBox"].posted_on == "2026-09-10"      # "5 days"
    assert by_employer["Scispot"].posted_on == "2026-06-17"        # "3 months"
    assert by_employer["Candid Health"].posted_on == "2023-09-16"  # "over 3 years"
    assert by_employer["Candid Health"].location == \
        "San Francisco, CA, US; New York, NY, US; Denver, CO, US"


def test_the_experience_line_reaches_the_model_as_prose(plugin, page):
    """`minExperience` is the field that separates a new-grad role from a senior one here
    ("Any (new grads ok)" against "6+ years"). It is left as text on purpose: deciding a
    level is the `level` task's job, and a gate here would apply before any title."""
    postings, _, _ = plugin.parse_page("YC", page, {}, TODAY)
    assert "Experience: 6+ years" in postings[0].description
    assert "Skills: Terraform" in postings[0].description


@pytest.mark.parametrize("body,expected", [
    ("<html><body><p>Log in to continue</p></body></html>", "no data-page"),
    ('<div data-page="{&quot;props&quot;:{}}"></div>', "no jobPostings"),
    ('<div data-page="not json"></div>', "no data-page"),
    (b"bytes are not a page", "not a page"),
])
def test_a_page_that_is_not_the_listing_is_a_failure_not_an_empty_board(
    plugin, body, expected
):
    """All four answer 200 and parse to zero jobs, which is exactly how a board that
    emptied looks. A login wall must never read as "YC has no openings today"."""
    reason = plugin.page_error(body)
    assert reason and expected in reason


def test_nothing_here_reads_the_dom(plugin):
    """The exception taken for this board is "parse the payload the page ships", not
    "scrape the page". A DOM walk would make a restyle a breakage and, worse, make a
    breakage look like an empty board."""
    source = pathlib.Path(plugin.__module__.replace(".", "/") + ".py")
    text = (pathlib.Path.cwd() / source).read_text()
    for banned in ("BeautifulSoup", "lxml", "html.parser", "findAll", "select_one"):
        assert banned not in text, banned


def test_the_read_is_a_fixed_few_pages_and_stays_that_way(plugin):
    urls = plugin.page_urls({}, TODAY)
    assert all(u.startswith("https://www.ycombinator.com/jobs/") for u in urls)
    assert 1 <= len(urls) <= 6

    from jobtracker.plugins.settings import InvalidSettings

    with pytest.raises(InvalidSettings):
        plugin.validate({"paths": ",".join(f"/jobs/{i}" for i in range(7))})
    with pytest.raises(InvalidSettings):
        plugin.validate({"paths": "https://example.invalid/jobs"})


def test_installing_it_never_starts_reading_anything(plugin):
    assert plugin.defaults()["enabled"] is False
