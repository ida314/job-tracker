"""The retry budget.

`Fetcher` is otherwise exercised through `tests/test_integration.py` with a fake, because
the real one opens sockets. What is worth pinning here is the one knob that was added for
a caller with a human waiting on it, and the fact that nothing else moved.
"""

from __future__ import annotations

from jobtracker import fetch


def test_the_retry_budget_defaults_to_the_nightly_constant():
    """Every scheduled path still burns MAX_RETRIES with backoff inside the run, which is
    what `health.REPAIR_FAILURE_THRESHOLD`'s reasoning depends on — anything transient is
    absorbed a layer down before a night is counted as a failure."""
    f = fetch.Fetcher()
    try:
        assert f._max_retries == fetch.MAX_RETRIES == 3
    finally:
        f.close()


def test_a_caller_with_someone_waiting_can_buy_a_smaller_budget():
    """`serve`'s add-a-company form. Three attempts on a 20-second timeout is a fine
    trade for a batch job at 01:00 and a two-minute freeze for a single-threaded web
    server."""
    f = fetch.Fetcher(max_workers=1, timeout=8, max_retries=1)
    try:
        assert (f._max_retries, f._timeout, f._max_workers) == (1, 8, 1)
        # Pacing is NOT part of the bargain — the per-host governor stays at its default,
        # because politeness to the ATS is not something a waiting page gets to skip.
        assert f._limiter._min == fetch.PER_HOST_MIN_INTERVAL
    finally:
        f.close()


# -- paging ---------------------------------------------------------------------------
#
# Added 2026-08-31 with the Workday and Amazon adapters, the first sources whose boards
# do not arrive in one call. These test `_fetch_paged`'s stopping rule against a fake
# transport; the adapters' own payload shapes are pinned in tests/test_sources.py.


class _FakeWorkday:
    """A board of `n` postings served 20 at a time, reporting `total` the way Workday
    really does: the true figure on page one and zero on every page after it."""

    ats = "workday"
    jobs_method = "POST"
    page_size = 20

    def __init__(self, n, over_cap_on=None, wrap=False):
        self.n = n
        self.over_cap_on = over_cap_on
        # Nvidia's real behaviour: an offset past the end wraps to the beginning rather
        # than returning a short page.
        self.wrap = wrap
        self.offsets = []

    def jobs_url(self, slug):
        return "https://x.wd5.myworkdayjobs.com/wday/cxs/x/y/jobs"

    def jobs_page_url(self, slug, offset):
        return self.jobs_url(slug)

    def jobs_body(self, slug, offset):
        self.offsets.append(offset)
        return {"limit": 20, "offset": offset}

    def page(self, offset):
        if offset == self.over_cap_on:
            return {"total": self.n}  # no jobPostings key — the over-cap shape
        if self.wrap and offset >= self.n:
            offset = offset % self.n
        rows = [{"externalPath": f"/job/{i}"} for i in range(offset, min(offset + 20, self.n))]
        return {"total": self.n if offset == 0 else 0, "jobPostings": rows}

    def jobs_page_error(self, raw):
        return None if "jobPostings" in raw else "no `jobPostings` key"

    def parse_jobs(self, company, raw):
        from jobtracker.models import Posting
        return [
            Posting(company=company, ats_job_id=j["externalPath"], title="t", url="")
            for j in raw.get("jobPostings", [])
        ]

    def posting_url(self, slug, ats_job_id):
        return f"https://x.wd5.myworkdayjobs.com/y{ats_job_id}"

    def identity_url(self, slug):
        return None

    def identity_from_jobs(self, raw):
        return None


def _fetcher_over(source, monkeypatch):
    from jobtracker import fetch as fetch_mod
    f = fetch_mod.Fetcher(max_workers=1)
    monkeypatch.setattr(fetch_mod, "get_source", lambda ats: source)
    monkeypatch.setattr(
        f, "_request_json",
        lambda url, method="GET", body=None: (200, source.page(body["offset"]), None),
    )
    return f


def test_paging_stops_on_a_short_page_and_not_on_the_total(monkeypatch):
    """Workday reports `total: 0` on every request carrying a non-zero offset — the
    figure is only populated on page one. A loop bounded by it reads the second request
    as the end of the board and silently keeps 20 of 122 reqs."""
    from jobtracker.models import Company
    src = _FakeWorkday(122)
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="X", ats="workday", slug="x/wd5/y",
                                    check_method="api"))
        assert r.ok and r.error is None
        assert len(r.postings) == 122
        assert src.offsets == [0, 20, 40, 60, 80, 100, 120]
    finally:
        f.close()


def test_a_board_that_is_an_exact_multiple_still_terminates(monkeypatch):
    """40 postings means page 3 is empty rather than short. An empty page is a short
    page, so the loop ends there rather than running to the cap."""
    from jobtracker.models import Company
    src = _FakeWorkday(40)
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="X", ats="workday", slug="x/wd5/y",
                                    check_method="api"))
        assert len(r.postings) == 40
        assert src.offsets == [0, 20, 40]
    finally:
        f.close()


def test_an_unreadable_page_fails_the_board_rather_than_truncating_it(monkeypatch):
    """The whole point of `jobs_page_error`. A page we cannot read must not be reported
    as the end of the results with the rows we happened to collect first — the board's
    remaining postings would be closed as though they had disappeared (DESIGN.md §3.4)."""
    from jobtracker.models import Company
    src = _FakeWorkday(122, over_cap_on=40)
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="X", ats="workday", slug="x/wd5/y",
                                    check_method="api"))
        assert r.ok is False
        assert r.postings == []
        assert "jobPostings" in r.error and "page 3" in r.error
    finally:
        f.close()


def test_a_paged_posting_gets_its_url_built_from_the_slug(monkeypatch):
    """Workday's rows carry a site-relative path and the payload names no host, so
    `parse_jobs` cannot build a URL and `fetch_company` fills it in."""
    from jobtracker.models import Company
    src = _FakeWorkday(3)
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="X", ats="workday", slug="x/wd5/y",
                                    check_method="api"))
        assert r.postings[0].url == "https://x.wd5.myworkdayjobs.com/y/job/0"
    finally:
        f.close()


def test_the_page_cap_is_a_ceiling_not_a_filter(monkeypatch):
    """A board that never reports a short page stops at MAX_PAGES rather than running
    forever. Hitting it is a warning, because the postings past it are unseen, not gone."""
    from jobtracker.models import Company
    monkeypatch.setattr(fetch, "MAX_PAGES", 3)
    src = _FakeWorkday(10_000)
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="X", ats="workday", slug="x/wd5/y",
                                    check_method="api"))
        assert r.ok and len(r.postings) == 60
    finally:
        f.close()


def test_an_offset_past_the_end_wraps_and_must_not_loop(monkeypatch):
    """Measured against Nvidia 2026-08-31, and the reason the short-page rule is not
    enough on its own. An out-of-range offset does not return a short page and does not
    error — it returns page one again, `total` repopulated. The run collected 4,000
    postings for a board of 2,000 and stopped only at the page cap, spending ~100
    requests re-fetching what it already held."""
    from jobtracker.models import Company
    src = _FakeWorkday(2000, wrap=True)
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="Nvidia", ats="workday", slug="n/wd5/y",
                                    check_method="api"))
        assert r.ok and r.error is None
        # Exactly the board, once.
        assert len(r.postings) == 2000
        assert len({p.ats_job_id for p in r.postings}) == 2000
        # 100 pages of board, plus the one wrapped page that proved it had ended.
        assert len(src.offsets) == 101
    finally:
        f.close()


def test_a_paged_board_can_still_report_its_identity(monkeypatch):
    """The paged branch holds no payload of its own unless one is handed back, so
    `identity_from_jobs` was being called with None and every paged board reported no
    identity. Right answer for Workday, which has none to give; a signal thrown away for
    Amazon, whose rows name the employer."""
    from jobtracker.models import Company
    src = _FakeWorkday(3)
    src.identity_from_jobs = lambda raw: (
        "Amazon.com Services LLC" if raw and "jobPostings" in raw else None
    )
    f = _fetcher_over(src, monkeypatch)
    try:
        r = f.fetch_company(Company(name="X", ats="workday", slug="x/wd5/y",
                                    check_method="api"))
        assert r.observed_board_name == "Amazon.com Services LLC"
    finally:
        f.close()
