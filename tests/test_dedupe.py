"""One req, one identity, however it arrived.

The properties that matter here, and what each one is protecting:

  * A URL-derived key and an identity-derived key agree for the same req. This is the
    whole mechanism: a Discord link to a company's Greenhouse board has to land on the
    same string as that board's own row, or dedupe covers nothing.
  * An api row is never closed in favour of a feed row, and two api rows sharing a key
    are reported rather than merged. These are the two rules that bound the damage a bad
    key can do, and they are the reason this can run across every source.
  * The winner does not depend on the order rows arrive in.
  * Every http(s) URL yields a key, so the backfill drains.

Not tested here: anything touching SQLite. This module is pure, and that is the point —
`store.py` computes and stores, `cli.py` decides when.
"""

import pytest

from jobtracker import dedupe


def _row(company, job_id, check_method="api", first_seen="2026-08-01"):
    return {
        "company": company,
        "ats_job_id": job_id,
        "check_method": check_method,
        "first_seen": first_seen,
    }


# -- key derivation ----------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        # The shapes measured in the live database, verbatim.
        ("https://job-boards.greenhouse.io/airtable/jobs/8391589002",
         "greenhouse:airtable:8391589002"),
        ("https://boards.greenhouse.io/cloudflare/jobs/7958059?gh_jid=7958059",
         "greenhouse:cloudflare:7958059"),
        ("https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245",
         "ashby:ramp:34413f8d-26bf-4bbc-8ade-eb309a0e2245"),
        ("https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c",
         "lever:palantir:ac978161-6f46-4f6b-ad9e-a258e642751c"),
        # The sample Discord posting that started this.
        ("https://jobs.lever.co/artera-2/eae88c70-fbf5-4525-890c-d3f9377418b0",
         "lever:artera-2:eae88c70-fbf5-4525-890c-d3f9377418b0"),
        # Regional and apply-path variants of the same req.
        ("https://jobs.eu.lever.co/acme/abc123", "lever:acme:abc123"),
        ("https://jobs.lever.co/acme/abc123/apply", "lever:acme:abc123"),
        ("https://jobs.ashbyhq.com/ramp/uuid-1/application", "ashby:ramp:uuid-1"),
    ],
)
def test_an_ats_url_keys_on_the_identity_inside_its_path(url, expected):
    assert dedupe.dedupe_key(url) == expected


def test_the_greenhouse_embed_url_keys_off_its_query_string():
    """The form URL `browser.py` builds puts both halves of the identity in the query.

    This is the one case where dropping the query before extracting would lose the
    identity entirely, which is why ATS matching runs first.
    """
    url = "https://boards.greenhouse.io/embed/job_app?for=stripe&token=4567"
    assert dedupe.dedupe_key(url) == "greenhouse:stripe:4567"


def test_tracking_parameters_and_trailing_slashes_do_not_split_one_req():
    a = dedupe.dedupe_key("https://acme.com/careers/job/99?utm_source=discord")
    b = dedupe.dedupe_key("https://www.acme.com/careers/job/99/")
    assert a == b == "url:acme.com/careers/job/99"


def test_the_path_is_not_lowercased_because_a_lever_slug_is_case_sensitive():
    """`lever/Onehouse` is a live board here and `lever/onehouse` 404s.

    Merging two genuinely distinct paths is worse than missing a duplicate: a bad merge
    closes a real posting, a missed one costs a redundant row.
    """
    assert dedupe.dedupe_key("https://acme.com/Jobs/A") != dedupe.dedupe_key(
        "https://acme.com/jobs/a"
    )


def test_an_ats_slug_and_id_are_lowercased_because_there_the_failure_runs_the_other_way():
    assert dedupe.dedupe_key("https://jobs.lever.co/Acme/ABC123") == "lever:acme:abc123"


@pytest.mark.parametrize("url", ["", "   ", "javascript:alert(1)", "mailto:a@b.com", "notaurl"])
def test_only_a_non_http_url_has_no_key(url):
    assert dedupe.dedupe_key(url) is None


def test_every_http_url_yields_a_key_so_the_backfill_drains():
    """`backfill_dedupe_key` drains on `dedupe_key IS NULL`.

    If an ordinary http URL could come back None, those rows would be re-examined every
    night forever and the backfill would never be a no-op.
    """
    for url in ["http://x", "https://a.b/c", "https://a.b", "https://a.b/?q=1#frag"]:
        assert dedupe.dedupe_key(url) is not None


# -- identity, the bridge for the 5,663 rows whose URL is not ats-shaped -------------
def test_the_identity_key_matches_the_key_read_out_of_the_hosted_url():
    """The property the whole feature rests on.

    Measured against the live database: 3,487 ats-hosted rows agree, 0 disagree. So a
    Discord link to a company's own board lands on that board's row.
    """
    assert dedupe.key_from_identity("greenhouse", "airtable", "8391589002") == dedupe.dedupe_key(
        "https://job-boards.greenhouse.io/airtable/jobs/8391589002"
    )
    assert dedupe.key_from_identity("lever", "artera-2", "eae88c70") == dedupe.dedupe_key(
        "https://jobs.lever.co/artera-2/eae88c70"
    )


def test_identity_bridges_a_board_whose_url_is_its_own_careers_site():
    """25 of 45 Greenhouse boards return a careers-site absolute_url; Stripe's is a
    search page with no req id in it. Keyed off that URL the row would sit in a
    namespace no feed link could reach — which is why identity is the primary rule."""
    stored_url = "https://stripe.com/jobs/search?gh_jid=4567"
    assert dedupe.dedupe_key(stored_url) != "greenhouse:stripe:4567"
    assert dedupe.key_from_identity("greenhouse", "stripe", "4567") == "greenhouse:stripe:4567"
    # ...and a Discord link to the real board meets it.
    assert dedupe.dedupe_key(
        "https://job-boards.greenhouse.io/stripe/jobs/4567"
    ) == dedupe.key_from_identity("greenhouse", "stripe", "4567")


def test_a_workday_identity_keys_on_the_tenant_not_the_data_centre():
    """A Workday slug is the triple tenant/dc/site. The dc is a hostname detail, so a
    board moving from wd5 to wd12 must not become a different req."""
    assert dedupe.key_from_identity("workday", "redhat/wd5/jobs", "R-1") == \
        dedupe.key_from_identity("workday", "redhat/wd12/jobs", "R-1")


@pytest.mark.parametrize(
    "ats,slug,job", [("aggregator", "x", "1"), ("plugin", "x", "1"),
                     ("bespoke", "x", "1"), ("greenhouse", "", "1"), ("greenhouse", "x", "")],
)
def test_identity_declines_when_there_is_no_curated_shape(ats, slug, job):
    """The caller then falls back to the URL. A feed has no ats identity by definition."""
    assert dedupe.key_from_identity(ats, slug, job) is None


# -- precedence: the two rules that bound the damage a bad key can do ----------------
def test_an_api_row_is_never_closed_in_favour_of_a_feed_row():
    api = _row("Stripe", "4567", "api", first_seen="2026-08-20")
    feed = _row("Simplify", "abc", "aggregator", first_seen="2026-01-01")
    plugin = _row("Discord: #jobs", "999", "plugin", first_seen="2026-01-01")
    winner, losers = dedupe.preferred([feed, plugin, api])
    assert winner is api
    assert losers == [feed, plugin] or set(map(id, losers)) == {id(feed), id(plugin)}


def test_two_api_rows_sharing_a_key_are_reported_and_neither_is_closed():
    """The fallback key is a normalized URL, and a board that links every req to one
    careers-search page hands dozens of live postings one key. Closing those is the most
    expensive failure this feature can have, so equal-rank api rows are a finding."""
    a = _row("Stripe", "1", "api", first_seen="2026-01-01")
    b = _row("Stripe", "2", "api", first_seen="2026-02-01")
    winner, losers = dedupe.preferred([a, b])
    assert winner is a
    assert losers == []
    assert dedupe.conflicting_api_rows([a, b]) == [a, b]


def test_a_feed_row_behind_two_conflicting_api_rows_is_still_closed():
    """The conflict is between the api rows. The feed row is redundant either way."""
    a = _row("Stripe", "1", "api")
    b = _row("Stripe", "2", "api")
    feed = _row("Simplify", "x", "aggregator")
    _, losers = dedupe.preferred([a, b, feed])
    assert losers == [feed]


def test_two_feed_rows_of_equal_rank_do_collapse():
    older = _row("Simplify", "a", "aggregator", first_seen="2026-01-01")
    newer = _row("Simplify", "b", "aggregator", first_seen="2026-05-01")
    winner, losers = dedupe.preferred([newer, older])
    assert winner is older and losers == [newer]


def test_the_winner_is_the_same_whichever_board_was_fetched_first():
    """`close_duplicates` runs once over the whole open set precisely so the outcome does
    not depend on companies.yaml ordering — but `preferred` must be order-free too, or
    that guarantee stops at the function boundary."""
    rows = [
        _row("Simplify", "a", "aggregator", "2026-01-01"),
        _row("Stripe", "4567", "api", "2026-08-01"),
        _row("Discord: #jobs", "99", "plugin", "2026-02-01"),
    ]
    import itertools

    winners = {dedupe.preferred(list(p))[0]["company"] for p in itertools.permutations(rows)}
    assert winners == {"Stripe"}


def test_ties_within_a_rank_break_on_first_seen_then_the_primary_key():
    same_day = [
        _row("B feed", "2", "aggregator", "2026-01-01"),
        _row("A feed", "1", "aggregator", "2026-01-01"),
    ]
    winner, _ = dedupe.preferred(same_day)
    assert winner["company"] == "A feed"


def test_one_row_is_never_its_own_duplicate():
    only = _row("Stripe", "1", "api")
    assert dedupe.preferred([only]) == (only, [])
    assert dedupe.preferred([]) == (None, [])


def test_an_unknown_check_method_loses_to_a_board_but_is_never_closed_by_a_peer():
    """A company missing from companies.yaml is far more likely to be a board we lost
    track of than a feed, and the damage runs one way. Measured on the live database:
    2,805 open postings sit on 13 shared fallback keys — 795 Databricks, 527 Stripe, 400
    MongoDB — and every one is safe only because its rows rank as something that is not a
    feed. Rank the unknown last instead and one dropped entry closes 795 live jobs."""
    known = _row("Stripe", "1", "api")
    weird = _row("Odd", "2", "somethingelse")
    winner, losers = dedupe.preferred([weird, known])
    assert winner is known
    assert losers == []  # not a feed, so not closed

    # Two unknowns are likewise left alone, and reported instead.
    other = _row("Odder", "3", "somethingelse")
    assert dedupe.preferred([weird, other])[1] == []
    assert len(dedupe.conflicting_api_rows([weird, other])) == 2

    # A real feed row behind them is still redundant and is still closed.
    feed = _row("Simplify", "9", "aggregator")
    assert dedupe.preferred([weird, known, feed])[1] == [feed]


def test_a_careers_url_keeps_the_query_parameter_that_identifies_the_req():
    """Betterment links all 41 of its live reqs to one careers page, distinguished only
    by `gh_jid` — which is the Greenhouse job id, not tracking. Verified over the live
    database: it appears on 6,019 URLs and equals the stored ats_job_id on all 6,403 rows
    carrying it, with none differing. Dropping it collapses a board into one key."""
    a = dedupe.dedupe_key(
        "https://www.betterment.com/careers/current-openings/job?gh_jid=7184616&gh_jid=7184616"
    )
    b = dedupe.dedupe_key(
        "https://www.betterment.com/careers/current-openings/job?gh_jid=7187115&gh_jid=7187115"
    )
    assert a != b
    assert a == "url:betterment.com/careers/current-openings/job?gh_jid=7184616"


def test_tracking_parameters_beside_an_identity_one_are_still_dropped():
    """Robinhood's URLs carry `?t=gh_src=&gh_jid=...`; only the second one identifies."""
    with_tracking = dedupe.dedupe_key("https://x.example/job?t=gh_src=&gh_jid=99&utm_source=d")
    plain = dedupe.dedupe_key("https://x.example/job?gh_jid=99")
    assert with_tracking == plain == "url:x.example/job?gh_jid=99"


# -- documented blind spot -----------------------------------------------------------
def test_a_simplify_wrapper_and_its_target_are_a_documented_miss():
    """Resolving this needs a redirect followed per posting against a third party at
    ingest time, to save one duplicate row. Not worth it — but the miss is pinned here
    so it stays a known limit rather than becoming a surprise."""
    wrapper = dedupe.dedupe_key("https://simplify.jobs/p/eae88c70-fbf5-4525-890c-d3f9377418b0")
    target = dedupe.dedupe_key("https://jobs.lever.co/artera-2/eae88c70-fbf5-4525-890c-d3f9377418b0")
    assert wrapper != target
    # Two Simplify links to one req do still meet each other.
    assert wrapper == dedupe.dedupe_key(
        "https://www.simplify.jobs/p/eae88c70-fbf5-4525-890c-d3f9377418b0?utm=x"
    )
