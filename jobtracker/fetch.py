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
from urllib.parse import urlparse

import requests
from opentelemetry import context as otel_context, metrics, trace

from .models import Company, FetchResult
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
USER_AGENT = "jobtracker/0.1 (+https://github.com/; backend-newgrad-tracker)"


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
    ) -> None:
        self._max_workers = max_workers
        self._timeout = timeout
        self._limiter = _HostRateLimiter(min_interval)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._retries = 0
        self._retry_lock = threading.Lock()

    def _count_retry(self) -> None:
        with self._retry_lock:
            self._retries += 1

    # -- single request with backoff -------------------------------------------------
    def _request_json(self, url: str, method: str = "GET"):
        """Return (status_code, parsed_json, error). Retries transient failures."""
        return self._request(url, method, want="json")

    def _request_text(self, url: str, method: str = "GET"):
        """Return (status_code, body_text, error). For non-JSON feeds (aggregator READMEs).

        Same retry/pacing/trace machinery as _request_json — an aggregator's GitHub host
        gets the same per-host governor as any board, and a 404 on a renamed repo is a
        FETCH_FAILED like any other, not a crash.
        """
        return self._request(url, method, want="text")

    def _request(self, url: str, method: str = "GET", want: str = "json"):
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

            for attempt in range(MAX_RETRIES):
                paced = self._limiter.wait(host)
                # Events are timestamped notes inside a span — cheaper than a child span
                # and perfect for "something notable happened here".
                if paced:
                    span.add_event("rate_limited", {"sleep.seconds": paced})
                    rate_limited_seconds.add(paced, {"server.address": host})
                log.debug("%s %s (attempt %d/%d)", method, url, attempt + 1, MAX_RETRIES)
                try:
                    resp = self._session.request(method, url, timeout=self._timeout)
                    status = resp.status_code
                    span.set_attribute("http.response.status_code", status)
                    if status in RETRY_STATUS:
                        last_error = f"HTTP {status}"
                        span.add_event("retry", {"attempt": attempt + 1, "reason": last_error})
                        self._retry_after(attempt, url, last_error)
                        continue
                    if status != 200:
                        return self._fail(span, status, f"HTTP {status}", attempt)
                    if attempt:
                        log.info("%s recovered on attempt %d/%d", url, attempt + 1, MAX_RETRIES)
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

            return self._fail(span, status, last_error, MAX_RETRIES - 1)

    @staticmethod
    def _fail(span, status: int | None, error: str, attempt: int):
        """Mark the span failed and return the error tuple. Keeps the two in lockstep."""
        span.set_attribute("http.resend_count", attempt)
        # An unset status means "no opinion"; ERROR is what makes a backend count this
        # toward an error rate. Forgetting it is why traces look healthy during outages.
        span.set_status(trace.Status(trace.StatusCode.ERROR, error))
        return status, None, error

    def _retry_after(self, attempt: int, url: str, reason: str) -> None:
        """Log and sleep between attempts. Silent retries hide creeping breakage."""
        backoff = BACKOFF_BASE * (2**attempt)
        if attempt + 1 < MAX_RETRIES:
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
                MAX_RETRIES,
            )
            time.sleep(backoff)
        else:
            # No sleep on the final attempt — there is nothing left to wait for.
            log.warning("%s -> %s; giving up after %d attempts", url, reason, MAX_RETRIES)

    # -- one company -----------------------------------------------------------------
    def fetch_job_description(self, company: Company, ats_job_id: str):
        """One posting's description, or None if this ATS bundles it already.

        Goes through _request_json so it inherits the per-host limiter, the retry
        policy, and the trace shape. The ATS is the scarce resource in the LLM pass —
        the local model is not rate-limited, boards-api.greenhouse.io very much is —
        so description fetching must be paced by exactly the same governor as the
        nightly sweep rather than opening a second, unpaced path to the same host.

        Returns None for Ashby and Lever, whose bulk payloads already carry the text.
        """
        source = get_source(company.ats)
        if source is None or not company.slug:
            return None
        url = source.job_detail_url(company.slug, ats_job_id)
        if url is None:
            return None
        with tracer.start_as_current_span("fetch.description") as span:
            span.set_attribute("company.name", company.name)
            span.set_attribute("company.ats", company.ats)
            _status, data, error = self._request_json(url)
            if error or data is None:
                span.set_attribute("fetch.outcome", "error")
                return None
            text = source.parse_job_detail(data)
            span.set_attribute("fetch.outcome", "ok" if text else "empty")
            return text

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

            status, raw, error = self._request_json(
                source.jobs_url(company.slug), source.jobs_method
            )
            result.status_code = status
            if error is not None:
                result.error = error
                return self._finish(span, result)

            result.ok = True
            result.postings = source.parse_jobs(company.name, raw)

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
