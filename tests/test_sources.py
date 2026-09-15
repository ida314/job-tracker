"""Each adapter normalizes its vendor JSON shape into Posting objects."""

from jobtracker.sources import get_source


def test_greenhouse_parse_and_identity():
    src = get_source("greenhouse")
    raw = {
        "jobs": [
            {
                "id": 12345,
                "title": "Software Engineer, New Grad",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
                "location": {"name": "New York, NY"},
                "updated_at": "2026-07-01T00:00:00Z",
            },
            {"id": None, "title": "broken"},  # skipped
        ]
    }
    postings = src.parse_jobs("Acme", raw)
    assert len(postings) == 1
    p = postings[0]
    assert p.ats_job_id == "12345"
    assert p.title == "Software Engineer, New Grad"
    assert p.location == "New York, NY"
    assert p.url.endswith("/12345")
    assert src.parse_identity({"name": "Acme Inc"}) == "Acme Inc"
    assert src.parse_identity({}) is None


def test_ashby_parse_and_identity_from_jobs():
    src = get_source("ashby")
    raw = {
        "jobs": [
            {
                "id": "abc-1",
                "title": "Backend Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/ramp/abc-1",
                "location": "New York",
                "isRemote": True,
                "publishedAt": "2026-07-01",
            }
        ]
    }
    postings = src.parse_jobs("Ramp", raw)
    assert postings[0].ats_job_id == "abc-1"
    assert "Remote" in postings[0].location
    assert src.identity_from_jobs(raw) == "ramp"  # org slug from jobUrl path


def test_lever_parse_and_identity():
    src = get_source("lever")
    raw = [
        {
            "id": "lev-1",
            "text": "Platform Engineer",
            "hostedUrl": "https://jobs.lever.co/matterport/lev-1",
            "categories": {"location": "Remote - US"},
            "createdAt": 1700000000000,
        }
    ]
    postings = src.parse_jobs("Matterport", raw)
    assert postings[0].title == "Platform Engineer"
    assert postings[0].location == "Remote - US"
    assert src.identity_from_jobs(raw) == "matterport"


def test_parsers_tolerate_garbage():
    for ats in ("greenhouse", "ashby", "lever"):
        src = get_source(ats)
        assert src.parse_jobs("X", None) == []
        assert src.parse_jobs("X", {"jobs": "nope"} if ats != "lever" else []) == []


# -- posted dates ------------------------------------------------------------------
# Three sources, three mutually incomparable raw formats, one TEXT column. Before
# normalization `ORDER BY posted_at` was silently wrong: the Lever epoch strings
# collate before every ISO timestamp, so every Lever posting sorted to one end
# regardless of its actual age. These pin each adapter's conversion.
def test_each_source_normalizes_its_own_format_to_an_iso_day():
    today = "2026-08-02"
    assert get_source("greenhouse").normalize_posted_at(
        "2026-08-01T01:46:42-04:00", today) == "2026-08-01"
    assert get_source("ashby").normalize_posted_at(
        "2026-08-01T01:57:58.337+00:00", today) == "2026-08-01"
    assert get_source("lever").normalize_posted_at("1785533737281", today) == "2026-07-31"


def test_lever_epoch_millis_would_otherwise_outsort_every_iso_string():
    """The specific bug: as raw text, '1259971200000' < '2026-...' for every ISO date."""
    src = get_source("lever")
    old, new = "1259971200000", "1785533737281"
    assert old < new  # correct as numbers-as-text, by luck of equal length
    assert new < "2026-08-01T00:00:00Z"  # but wrong against any other source
    assert src.normalize_posted_at(old, "2026-08-02") == "2009-12-05"
    assert src.normalize_posted_at(new, "2026-08-02") == "2026-07-31"


def test_unparseable_dates_are_none_not_today():
    """A missing date must never read as 'posted today' — that inverts the ranking."""
    today = "2026-08-02"
    for ats in ("greenhouse", "ashby", "lever"):
        src = get_source(ats)
        for raw in (None, "", "garbage", "not-a-date"):
            assert src.normalize_posted_at(raw, today) is None, (ats, raw)


def test_greenhouse_prefers_first_published_over_updated_at():
    """`updated_at` moves whenever anyone edits the req — a salary fix re-dates a
    six-month-old posting as fresh. Only the detail payload carries the real one."""
    src = get_source("greenhouse")
    detail = {"content": "x", "updated_at": "2026-08-02T00:00:00Z",
              "first_published": "2026-03-14T00:00:00Z"}
    assert src.parse_job_detail_posted_at(detail) == "2026-03-14T00:00:00Z"
    # Falls back when the board does not expose it.
    assert src.parse_job_detail_posted_at(
        {"updated_at": "2026-08-02T00:00:00Z"}) == "2026-08-02T00:00:00Z"
    assert src.parse_job_detail_posted_at(None) is None


# -- Workday: the first paged board ---------------------------------------------------
#
# Workday was `check_method: manual` until 2026-08-31 on the belief that its portal has no
# keyless JSON board. It has one. Every fixture below is a trimmed capture of a real
# response taken that day, because each of these tests exists for a shape that was assumed
# rather than looked at.

# One page of https://redhat.wd5.myworkdayjobs.com/wday/cxs/redhat/jobs/jobs
_WORKDAY_PAGE = {
    "total": 122,
    "jobPostings": [
        {
            "title": "Associate Consultant - OpenShift",
            "externalPath": "/job/Mumbai/Associate-Consultant---OpenShift_R-058865-1",
            "locationsText": "Mumbai",
            "postedOn": "Posted Today",
            "remoteType": "Onsite",
            "bulletFields": ["R-058865"],
        },
        {
            "title": "Architect, OpenShift",
            "externalPath": "/job/Remote-US-DC/Architect--OpenShift_R-058111",
            "locationsText": "Remote US DC",
            "postedOn": "Posted 2 Days Ago",
            "bulletFields": ["R-058111"],
        },
        {"title": "no path, skipped"},
    ],
    "facets": [],
    "userAuthenticated": False,
}


def test_workday_parses_a_page_and_builds_urls_from_the_slug():
    src = get_source("workday")
    postings = src.parse_jobs("Red Hat", _WORKDAY_PAGE)
    assert len(postings) == 2
    p = postings[0]
    # The path is the id: unique on the board and the exact key the detail endpoint takes.
    assert p.ats_job_id == "/job/Mumbai/Associate-Consultant---OpenShift_R-058865-1"
    assert p.title == "Associate Consultant - OpenShift"
    assert p.location == "Mumbai"
    assert p.posted_at == "Posted Today"
    # The payload names no host, so parse_jobs cannot build a URL and does not pretend to.
    assert p.url == ""
    assert src.posting_url("redhat/wd5/jobs", p.ats_job_id) == (
        "https://redhat.wd5.myworkdayjobs.com/jobs"
        "/job/Mumbai/Associate-Consultant---OpenShift_R-058865-1"
    )


def test_a_workday_page_over_the_cap_is_a_failure_not_an_empty_board():
    """The trap this whole hook exists for.

    Workday caps a page at 20 rows. `limit: 50` does not clamp and does not error — it
    returns HTTP 200, valid JSON, and no `jobPostings` key at all. Read as zero rows on
    page one, that closes every posting the company has (DESIGN.md §3.4).
    """
    src = get_source("workday")
    over_cap = {"total": 122, "facets": [], "userAuthenticated": False}
    assert src.jobs_page_error(over_cap) is not None
    assert "jobPostings" in src.jobs_page_error(over_cap)
    # And a real page is accepted.
    assert src.jobs_page_error(_WORKDAY_PAGE) is None
    # Zero rows on a well-formed page is a genuinely empty board, which is a different
    # fact and must still be allowed through to health.py.
    assert src.jobs_page_error({"total": 0, "jobPostings": []}) is None


def test_workday_never_claims_identity_because_it_would_restate_the_slug():
    """Nothing in either payload names the employer. The only company-ish string is the
    tenant inside the URL we asked for — the `ashby/cedar` tautology."""
    src = get_source("workday")
    assert src.identity_from_jobs(_WORKDAY_PAGE) is None
    assert src.identity_url("redhat/wd5/jobs") is None


def test_a_workday_slug_is_a_triple_and_a_partial_one_is_refused():
    src = get_source("workday")
    assert src.parse_slug("redhat/wd5/jobs") == ("redhat", "wd5", "jobs")
    # The two entries that predate the adapter were hand-written with spaces.
    assert src.parse_slug("redhat / wd5 / jobs") == ("redhat", "wd5", "jobs")
    for bad in ("redhat", "redhat/wd5", "", "redhat//jobs", "a/b/c/d", None):
        assert src.parse_slug(bad) is None, bad


def test_workday_relative_prose_and_exact_dates_both_resolve():
    src = get_source("workday")
    today = "2026-08-31"
    assert src.normalize_posted_at("Posted Today", today) == "2026-08-31"
    assert src.normalize_posted_at("Posted Yesterday", today) == "2026-08-30"
    assert src.normalize_posted_at("Posted 2 Days Ago", today) == "2026-08-29"
    # "30+" is a bound, floored at its edge.
    assert src.normalize_posted_at("Posted 30+ Days Ago", today) == "2026-08-01"
    # The detail payload's startDate arrives here too, and wins by being exact.
    assert src.normalize_posted_at("2026-08-29", today) == "2026-08-29"


def test_workday_start_date_is_the_posted_date():
    """Verified against live data 2026-08-31: a posting reading "Posted 2 Days Ago"
    carries startDate 2026-08-29. The relative prose is counted from it."""
    src = get_source("workday")
    detail = {"jobPostingInfo": {
        "startDate": "2026-08-29", "jobDescription": "<p>About the <b>Job</b></p>"}}
    assert src.parse_job_detail_posted_at(detail) == "2026-08-29"
    assert src.parse_job_detail(detail) == "About the Job"
    assert src.parse_job_detail_posted_at({"jobPostingInfo": {}}) is None
    assert src.parse_job_detail(None) is None


def test_the_paged_adapter_tolerates_garbage_like_every_other_one():
    src = get_source("workday")
    assert src.parse_jobs("X", None) == []
    assert src.parse_jobs("X", {"jobPostings": "nope"}) == []
    for raw in (None, "", "garbage", "not-a-date"):
        assert src.normalize_posted_at(raw, "2026-08-31") is None, raw
