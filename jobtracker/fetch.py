"""HTTP fetching: concurrency cap, backoff, per-host pacing.

Every failure catalogued in DESIGN.md §2.2 is an I/O concern handled here, not a
reasoning concern:

  * ~12-way parallel fetches got egress-throttled to http=000 for every host  ->  a
    hard concurrency cap (4) plus per-host minimum spacing.
  * large content payloads blew the 20s timeout  ->  sources drop ?content=true and we
    keep a firm timeout.
  * non-200 / timeout / bad JSON  ->  FetchResult.error is set and postings stays empty;
    a failed fetch is NEVER a zero-posting "success" (that distinction is health.py's job).

All three are invisible from the outside unless we say so, hence the logging here: a run
that spends its time asleep in the rate limiter and a run that spends it burning retries
take the same wall-clock but mean very different things. Progress lines go to the logger
at INFO, per-request detail at DEBUG; nothing is printed to stdout (that belongs to the
report).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from urllib.parse import urlparse

import requests
from opentelemetry import context as otel_context, metrics, trace

from .models import Company, FetchResult, Posting
from .sources import get_source

log = logging.getLogger(__name__)

# API only — never the SDK. If nothing configured a provider (see telemetry.py), this is
# a no-op tracer and every span call below costs essentially nothing. That is why there
# is no "is tracing enabled?" check anywhere in this file.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# Traces answer "what happened in this run"; metrics answer "what is happening over
# weeks". A board that grew from 0.4s to 4s since March is invisible in any single trace
# and obvious in a histogram — that is the whole reason tier 3 exists.
#
# Default histogram buckets are tuned for milliseconds, so second-scale values would all
# pile into the first bucket. These boundaries are chosen around what this job actually
# does: sub-second Ashby reads, ~2.7s paced Greenhouse reads, and a 20s timeout ceiling.
_DURATION_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 30]

fetch_duration = meter.create_histogram(
    "jobtracker.fetch.duration",
    unit="s",
    description="Wall time to fetch one company's board, including retries and pacing",
    explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
)
postings_seen = meter.create_histogram(
    "jobtracker.fetch.postings",
    unit="{posting}",
    description="Postings returned per board — a board falling to zero is the alarm",
    explicit_bucket_boundaries_advisory=[0, 1, 10, 50, 100, 250, 500, 1000],
)
retries_total = meter.create_counter(
    "jobtracker.fetch.retries",
    unit="{retry}",
    description="HTTP attempts beyond the first, by host",
)
rate_limited_seconds = meter.create_counter(
    "jobtracker.fetch.rate_limited.time",
    unit="s",
    description="Time deliberately slept in the per-host limiter, by host",
)

MAX_WORKERS = 4
PER_HOST_MIN_INTERVAL = 0.34  # ~3 req/s/host — well under the throttle threshold
TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_BASE = 1.5
RETRY_STATUS = {429, 500, 502, 503, 504}
# A host that answers 429 usually says how long to wait, in a standard header. Honouring
# it is strictly better than our own backoff — Discord's buckets routinely ask for longer
# than 1.5s/3.0s, so the existing ladder burns all three attempts and reports FETCH_FAILED
# for a request that would have succeeded. Capped, because a hostile or buggy header must
# not be able to hang a nightly batch job.
MAX_RETRY_AFTER = 30.0
USER_AGENT = "jobtracker/0.1 (+https://github.com/; backend-newgrad-tracker)"
# Ceiling on requests for one paged board. Workday caps a page at 20 rows, so Nvidia's
# ~2,000 reqs is ~100 requests. Measured 2026-08-31: the `cxs` endpoint answers in ~2.4s,
# which is latency and not our pacing (a 7-page Red Hat run reported 16.8s wall and 0.0s
# slept in the limiter), so a 2,000-req board costs ~4 minutes and the big boards run in
# parallel rather than faster.
#
# The cap is a backstop, not a filter, and it is not what ends a normal walk — the two
# rules in `_fetch_paged` are. Hitting it is logged as a warning, because a truncated
# board is a fact about the run: postings we did not see would otherwise be closed as
# though they had disappeared.
MAX_PAGES = 200


class _HostRateLimiter:
    """Serialize requests per host to a minimum interval, without blocking other hosts."""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}
        self._slept = 0.0  # cumulative, for the end-of-run summary

    def wait(self, host: str) -> float:
        """Block until this host may be hit again. Returns the seconds actually slept."""
        with self._lock:
            now = time.monotonic()
            allowed = self._next.get(host, 0.0)
            start = max(now, allowed)
            self._next[host] = start + self._min
            delay = start - now
            if delay > 0:
                self._slept += delay
        if delay > 0:
            time.sleep(delay)
            return delay
        return 0.0

    @property
    def slept(self) -> float:
        with self._lock:
            return self._slept


class Fetcher:
    def __init__(
        self,
        max_workers: int = MAX_WORKERS,
        min_interval: float = PER_HOST_MIN_INTERVAL,
        timeout: int = TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._max_workers = max_workers
        self._timeout = timeout
        # A parameter, defaulting to the nightly constant, so a caller that must answer a
        # human *now* can buy a smaller retry budget. `serve`'s add-a-company form is the
        # one such caller: the nightly budget is 3 attempts × a 20s timeout, which is a
        # fine trade for a batch job at 01:00 and a two-minute freeze for a single-threaded
        # web server. Nothing else passes it, so `health.py`'s "fetch.py already burned
        # MAX_RETRIES inside the run" still describes every scheduled path.
        self._max_retries = max_retries
        self._limiter = _HostRateLimiter(min_interval)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._retries = 0
        self._retry_lock = threading.Lock()

    def _count_retry(self) -> None:
        with self._retry_lock:
            self._retries += 1

    # -- single request with backoff -------------------------------------------------
    def _request_json(
        self,
        url: str,
        method: str = "GET",
        body: dict | None = None,
        headers: dict | None = None,
    ):
        """Return (status_code, parsed_json, error). Retries transient failures."""
        return self._request(url, method, want="json", body=body, headers=headers)

    def _request_text(self, url: str, method: str = "GET"):
        """Return (status_code, body_text, error). For non-JSON feeds (aggregator READMEs).

        Same retry/pacing/trace machinery as _request_json — an aggregator's GitHub host
        gets the same per-host governor as any board, and a 404 on a renamed repo is a
        FETCH_FAILED like any other, not a crash.
        """
        return self._request(url, method, want="text")

    def _request(
        self,
        url: str,
        method: str = "GET",
        want: str = "json",
        body: dict | None = None,
        headers: dict | None = None,
    ):
        last_error = "unknown error"
        status: int | None = None
        host = urlparse(url).netloc

        # One span per *logical* request, wrapping all attempts. The requests
        # auto-instrumentation adds a child span per actual wire call underneath, so a
        # retried request shows up as one parent with three children — exactly the shape
        # you want when asking "did this board need retries?".
        with tracer.start_as_current_span("http.request") as span:
            # Attribute names follow OTel semantic conventions. Sticking to them is what
            # lets a backend build latency-by-host charts without per-app configuration.
            span.set_attribute("http.request.method", method)
            span.set_attribute("url.full", url)
            span.set_attribute("server.address", host)

            for attempt in range(self._max_retries):
                paced = self._limiter.wait(host)
                # Events are timestamped notes inside a span — cheaper than a child span
                # and perfect for "something notable happened here".
                if paced:
                    span.add_event("rate_limited", {"sleep.seconds": paced})
                    rate_limited_seconds.add(paced, {"server.address": host})
                log.debug("%s %s (attempt %d/%d)", method, url, attempt + 1, self._max_retries)
                try:
                    # Per-request, never on the session. A session header would carry a
                    # plugin's bearer token to every board in companies.yaml. `requests`
                    # merges these over the session's, which is also how a caller
                    # overrides User-Agent for a host that mandates its own shape.
                    resp = self._session.request(
                        method, url, timeout=self._timeout, json=body, headers=headers
                    )
                    status = resp.status_code
                    span.set_attribute("http.response.status_code", status)
                    if status in RETRY_STATUS:
                        last_error = f"HTTP {status}"
                        span.add_event("retry", {"attempt": attempt + 1, "reason": last_error})
                        self._retry_after(
                            attempt, url, last_error,
                            self._parse_retry_after(resp.headers.get("Retry-After")),
                        )
                        continue
                    if status != 200:
                        return self._fail(span, status, f"HTTP {status}", attempt)
                    if attempt:
                        log.info("%s recovered on attempt %d/%d", url, attempt + 1, self._max_retries)
                    if want == "text":
                        payload = resp.text
                    else:
                        try:
                            payload = resp.json()
                        except ValueError:
                            return self._fail(span, status, "malformed JSON", attempt)
                    span.set_attribute("http.resend_count", attempt)
                    return status, payload, None
                except requests.RequestException as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    # record_exception attaches the traceback as a span event. Use it for
                    # genuine exceptions; set_status is what marks the span itself failed.
                    span.record_exception(exc)
                    span.add_event("retry", {"attempt": attempt + 1, "reason": last_error})
                    self._retry_after(attempt, url, last_error)

            return self._fail(span, status, last_error, self._max_retries - 1)

    @staticmethod
    def _fail(span, status: int | None, error: str, attempt: int):
        """Mark the span failed and return the error tuple. Keeps the two in lockstep."""
        span.set_attribute("http.resend_count", attempt)
        # An unset status means "no opinion"; ERROR is what makes a backend count this
        # toward an error rate. Forgetting it is why traces look healthy during outages.
        span.set_status(trace.Status(trace.StatusCode.ERROR, error))
        return status, None, error

    @staticmethod
    def _parse_retry_after(raw: object) -> float | None:
        """Seconds from a `Retry-After` header, or None if it does not say a number.

        RFC 9110 also allows an HTTP-date there. We ignore that form deliberately rather
        than parsing it: it is rare, it needs a clock comparison, and falling back to the
        existing backoff is a correct answer. Read from the *header* rather than a JSON
        body so this stays `want`-agnostic and works for a text fetch too.
        """
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    def _retry_after(
        self, attempt: int, url: str, reason: str, retry_after: float | None = None
    ) -> None:
        """Log and sleep between attempts. Silent retries hide creeping breakage."""
        backoff = min(retry_after, MAX_RETRY_AFTER) if retry_after else BACKOFF_BASE * (2**attempt)
        if attempt + 1 < self._max_retries:
            self._count_retry()
            # Low-cardinality attributes only: host, not url. A per-URL counter would mint
            # a fresh time series per company and blow up the backend's index.
            retries_total.add(1, {"server.address": urlparse(url).netloc})
            log.warning(
                "%s -> %s; retrying in %.1fs (attempt %d/%d)",
                url,
                reason,
                backoff,
                attempt + 2,
                self._max_retries,
            )
            time.sleep(backoff)
        else:
            # No sleep on the final attempt — there is nothing left to wait for.
            log.warning("%s -> %s; giving up after %d attempts", url, reason, self._max_retries)

    def fetch_page(self, url: str) -> tuple[str | None, str | None]:
        """One HTML page — a company's careers page — as `(text, error)`.

        Goes through `_request_text` so a careers host gets the same per-host governor,
        retry policy and trace shape as any board. Slug repair must not open a second,
        unpaced path to the network for exactly the reason `fetch_job_detail` does not:
        the remote host is the scarce resource, and there is only one governor.

        Returns the error rather than raising. A careers page that 404s is a fact about
        the curated data, and the repair pass reports it as one.
        """
        with tracer.start_as_current_span("fetch.page") as span:
            span.set_attribute("url.full", url)
            span.set_attribute("server.address", urlparse(url).netloc)
            _status, text, error = self._request_text(url)
            if error:
                span.set_status(trace.Status(trace.StatusCode.ERROR, error))
                return None, error
            return text, None

    def fetch_json(
        self, url: str, headers: dict | None = None
    ) -> tuple[int | None, object | None, str | None]:
        """One paced JSON GET with caller-supplied headers, as `(status, payload, error)`.

        The entry point for an import plugin, so nothing outside this module has to reach
        into a `_`-prefixed method to talk to a feed. Everything is inherited: the
        per-host governor, the retry policy, `Retry-After`, and the trace shape.

        It returns the **status** as well, unlike `fetch_page`. For a credentialed feed
        the code is the finding: 401 is a bad token, 403 is a permission the bot was not
        granted, 429 is pacing. Collapsing those into one error string would hide the
        only thing that tells you which of them to go and fix.

        `headers` may carry a credential, so note what is safe about that: `_request`
        records `url.full` as a span attribute and logs the URL on every retry, but never
        headers. **A token must therefore never move into a query parameter**, or it
        lands in traces and logs the same day.
        """
        with tracer.start_as_current_span("fetch.feed") as span:
            span.set_attribute("url.full", url)
            span.set_attribute("server.address", urlparse(url).netloc)
            status, payload, error = self._request_json(url, headers=headers)
            if error:
                span.set_status(trace.Status(trace.StatusCode.ERROR, error))
            return status, payload, error

    # -- one company -----------------------------------------------------------------
    def fetch_job_detail(self, company: Company, ats_job_id: str):
        """One posting's (description, posted_at) — `(None, None)` if unavailable.

        Both come from the same payload, so they are fetched together: asking twice
        would double the request count against the one host that is actually scarce.

        Goes through _request_json so it inherits the per-host limiter, the retry
        policy, and the trace shape. The ATS is the scarce resource here — the local
        model is not rate-limited, boards-api.greenhouse.io very much is — so this
        must be paced by exactly the same governor as the nightly sweep rather than
        opening a second, unpaced path to the same host.

        Returns `(None, None)` for Ashby and Lever, whose bulk payloads already carry
        the text and a usable date.
        """
        source = get_source(company.ats)
        if source is None or not company.slug:
            return None, None
        url = source.job_detail_url(company.slug, ats_job_id)
        if url is None:
            return None, None
        with tracer.start_as_current_span("fetch.description") as span:
            span.set_attribute("company.name", company.name)
            span.set_attribute("company.ats", company.ats)
            _status, data, error = self._request_json(url)
            if error or data is None:
                span.set_attribute("fetch.outcome", "error")
                return None, None
            text = source.parse_job_detail(data)
            span.set_attribute("fetch.outcome", "ok" if text else "empty")
            return text, source.parse_job_detail_posted_at(data)

    def fetch_application_form(self, company: Company, ats_job_id: str) -> list:
        """One posting's application questions. `[]` when the ATS does not publish them.

        Same governor as everything else that touches an ATS: `_request_json`, so the
        per-host limiter, the retry policy and the trace shape all apply. Prefill is a
        background task and the boards are the scarce resource — it must not get its own
        unpaced path to the same host.

        An empty list is not an error and never fails the run. For Ashby and Lever it is
        the expected answer, and their forms are read from the DOM instead.
        """
        source = get_source(company.ats)
        if source is None or not company.slug:
            return []
        url = source.application_form_url(company.slug, ats_job_id)
        if url is None:
            return []
        with tracer.start_as_current_span("fetch.application_form") as span:
            span.set_attribute("company.name", company.name)
            span.set_attribute("company.ats", company.ats)
            _status, data, error = self._request_json(url)
            if error or data is None:
                span.set_attribute("fetch.outcome", "error")
                return []
            fields = source.parse_application_form(data)
            span.set_attribute("fetch.outcome", "ok" if fields else "empty")
            span.set_attribute("form.fields", len(fields))
            return fields

    def _fetch_paged(self, source, company: Company):
        """Walk a paged board. Returns (status, postings, first_page, error).

        `first_page` is handed back so identity can be read from it. Without it the
        paged branch would call `identity_from_jobs(None)` and every paged board would
        report no identity — which for Workday is the right answer for its own reasons,
        and for Amazon would be a signal silently thrown away.

        Two stopping rules, and the second is not redundant.

        **A short page.** The obvious end of results, and the only one that is true on
        every page — `total` is not. Workday reports `total: 0` on every request carrying
        a non-zero offset, the figure being populated on page one alone, so a loop
        bounded by it reads the second request as the end of the board and silently keeps
        20 of 2,000 reqs.

        **A page that is all postings we already hold.** Measured against Nvidia
        2026-08-31: an offset past the end does not return a short page and does not
        error — it *wraps to the beginning*. offset=2000, 3000, 4000 and 5000 all return
        the same first row as offset=0, with `total` helpfully repopulated. So the first
        rule never fires on that board, and the loop ran to the page cap collecting 4,000
        postings for a board of 2,000, the second half being the first half again. Ids
        are the check because they are what identifies a posting; a page that adds none
        has told us nothing new, whatever it says about itself.
        """
        postings: list[Posting] = []
        seen: set[str] = set()
        status = None
        first_page = None
        for page in range(MAX_PAGES):
            offset = page * source.page_size
            status, raw, error = self._request_json(
                source.jobs_page_url(company.slug, offset),
                source.jobs_method,
                body=source.jobs_body(company.slug, offset),
            )
            if error is not None:
                return status, [], None, error
            # Ask the adapter whether this is a page at all before believing its row
            # count. A 200 carrying an unrecognized shape is not an empty board.
            bad = source.jobs_page_error(raw)
            if bad is not None:
                return status, [], None, f"page {page + 1}: {bad}"
            if first_page is None:
                first_page = raw
            batch = source.parse_jobs(company.name, raw)
            fresh = [p for p in batch if p.ats_job_id not in seen]
            seen.update(p.ats_job_id for p in fresh)
            postings.extend(fresh)
            if not fresh:
                log.debug(
                    "%s: page %d repeated postings already held — end of board",
                    company.name,
                    page + 1,
                )
                return status, postings, first_page, None
            if len(batch) < source.page_size:
                return status, postings, first_page, None
        log.warning(
            "%s: stopped at the %d-page cap with %d postings; the board may be truncated",
            company.name,
            MAX_PAGES,
            len(postings),
        )
        return status, postings, first_page, None

    def fetch_company(self, company: Company) -> FetchResult:
        # Span names should be low-cardinality — "fetch.company", never
        # f"fetch {company.name}". The company goes in an *attribute*, which is the
        # dimension you filter and group by. Putting it in the name instead gives a
        # backend 56 unrelated operations rather than one operation with 56 instances.
        with tracer.start_as_current_span("fetch.company") as span:
            span.set_attribute("company.name", company.name)
            span.set_attribute("company.ats", company.ats)
            span.set_attribute("company.slug", company.slug)

            source = get_source(company.ats)
            result = FetchResult(company=company.name, ats=company.ats, slug=company.slug)
            if source is None:
                result.error = f"no source adapter for ats={company.ats!r}"
                return self._finish(span, result)
            if not company.slug:
                result.error = "empty slug"
                return self._finish(span, result)

            raw = None
            if source.page_size:
                status, postings, raw, error = self._fetch_paged(source, company)
                result.status_code = status
                if error is not None:
                    result.error = error
                    return self._finish(span, result)
                result.ok = True
                result.postings = postings
            else:
                status, raw, error = self._request_json(
                    source.jobs_url(company.slug), source.jobs_method
                )
                result.status_code = status
                if error is not None:
                    result.error = error
                    return self._finish(span, result)
                result.ok = True
                result.postings = source.parse_jobs(company.name, raw)

            # A posting whose payload could not name its own URL (Workday sends a
            # site-relative path and no host) gets one built from the slug. Guarded on
            # emptiness so this is a no-op for every adapter that already has one.
            if result.postings and not result.postings[0].url:
                result.postings = [
                    replace(p, url=source.posting_url(company.slug, p.ats_job_id))
                    if not p.url
                    else p
                    for p in result.postings
                ]

            # Identity: a dedicated endpoint (Greenhouse) or derived from the payload.
            id_url = source.identity_url(company.slug)
            if id_url is not None:
                _, id_raw, id_err = self._request_json(id_url)
                if id_err is None:
                    result.observed_board_name = source.parse_identity(id_raw)
            else:
                result.observed_board_name = source.identity_from_jobs(raw)
            return self._finish(span, result)

    def fetch_aggregator(self, company: Company) -> FetchResult:
        """Fetch one aggregator feed (a raw README URL) and parse it to postings.

        Parallel to fetch_company but for `check_method: aggregator`: the URL is the
        company's `board_url` (there is no slug to template), the body is text not JSON,
        and there is no identity endpoint — a feed either parses to rows or it does not.
        The result flows through the same health/sync/match loop as any board.
        """
        with tracer.start_as_current_span("fetch.aggregator") as span:
            span.set_attribute("company.name", company.name)
            span.set_attribute("company.ats", company.ats)

            source = get_source(company.ats)
            result = FetchResult(company=company.name, ats=company.ats, slug="")
            if source is None:
                result.error = f"no source adapter for ats={company.ats!r}"
                return self._finish(span, result)
            if not company.board_url:
                result.error = "no board_url"
                return self._finish(span, result)

            status, text, error = self._request_text(company.board_url)
            result.status_code = status
            if error is not None:
                result.error = error
                return self._finish(span, result)

            result.ok = True
            result.postings = source.parse_jobs(company.name, text)
            return self._finish(span, result)

    @staticmethod
    def _finish(span, result: FetchResult) -> FetchResult:
        """Copy the outcome onto the span. One exit point, so nothing goes unrecorded."""
        span.set_attribute("fetch.postings.count", len(result.postings))
        span.set_attribute("fetch.ok", result.ok)
        if result.observed_board_name:
            span.set_attribute("board.observed_name", result.observed_board_name)
        if result.error:
            span.set_status(trace.Status(trace.StatusCode.ERROR, result.error))
        return result

    # -- fan out ---------------------------------------------------------------------
    def fetch_all(self, companies: list[Company]) -> list[FetchResult]:
        """Fetch every company, logging each board as it lands.

        Completion order is nondeterministic, so results are reassembled into the input
        order before returning — everything downstream stays reproducible.
        """
        if not companies:
            return []

        total = len(companies)
        width = len(str(total))
        started = time.monotonic()
        results: list[FetchResult | None] = [None] * total
        done = failed = 0

        log.info("fetching %d boards (%d workers)", total, self._max_workers)
        with tracer.start_as_current_span("fetch.all") as span:
            span.set_attribute("fetch.companies.count", total)
            span.set_attribute("fetch.max_workers", self._max_workers)

            # THE THREADING GOTCHA. OTel tracks the "current span" in a context variable,
            # and a brand-new worker thread does not inherit it. Submit naively and every
            # fetch.company span becomes a detached root — 56 unrelated one-span traces
            # instead of one tree. So capture the context here and re-attach it inside the
            # worker. Anything you hand to a thread, a queue, or a callback needs this.
            parent = otel_context.get_current()

            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                futures = {
                    pool.submit(self._fetch_timed, c, parent): i
                    for i, c in enumerate(companies)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    result, elapsed = future.result()
                    results[index] = result
                    done += 1
                    if result.error:
                        failed += 1
                    log.info(
                        "[%*d/%d] %-24s %-8s %s (%.1fs)",
                        width,
                        done,
                        total,
                        companies[index].name[:24],
                        f"{result.ats}/{result.slug}"[:8],
                        f"FAIL {result.error}"
                        if result.error
                        else f"{len(result.postings):>4} jobs",
                        elapsed,
                    )

            span.set_attribute("fetch.failed.count", failed)
            span.set_attribute("fetch.retries.count", self._retries)
            span.set_attribute("fetch.rate_limited.seconds", round(self._limiter.slept, 2))

        log.info(
            "fetched %d boards in %.1fs — %d failed, %d retries, %.1fs in per-host pacing",
            total,
            time.monotonic() - started,
            failed,
            self._retries,
            self._limiter.slept,
        )
        return [r for r in results if r is not None]

    def _fetch_timed(self, company: Company, parent) -> tuple[FetchResult, float]:
        token = otel_context.attach(parent)  # adopt the caller's span as our parent
        try:
            started = time.monotonic()
            result = self.fetch_company(company)
            elapsed = time.monotonic() - started
            # `ats` and `outcome` are bounded sets (4 × 2), so this is 8 series at most.
            # company.name would be 56 more — deliberately left off; that dimension is
            # what traces are for.
            attrs = {"ats": company.ats, "outcome": "ok" if result.ok else "failed"}
            fetch_duration.record(elapsed, attrs)
            if result.ok:
                postings_seen.record(len(result.postings), {"ats": company.ats})
            return result, elapsed
        finally:
            otel_context.detach(token)  # always detach, or the thread leaks context

    def close(self) -> None:
        self._session.close()
