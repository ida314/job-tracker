"""HTTP fetching: concurrency cap, backoff, per-host pacing.

Every failure catalogued in DESIGN.md §2.2 is an I/O concern handled here, not a
reasoning concern:

  * ~12-way parallel fetches got egress-throttled to http=000 for every host  ->  a
    hard concurrency cap (4) plus per-host minimum spacing.
  * large content payloads blew the 20s timeout  ->  sources drop ?content=true and we
    keep a firm timeout.
  * non-200 / timeout / bad JSON  ->  FetchResult.error is set and postings stays empty;
    a failed fetch is NEVER a zero-posting "success" (that distinction is health.py's job).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from .models import Company, FetchResult
from .sources import get_source

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

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            allowed = self._next.get(host, 0.0)
            start = max(now, allowed)
            self._next[host] = start + self._min
        delay = start - now
        if delay > 0:
            time.sleep(delay)


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

    # -- single request with backoff -------------------------------------------------
    def _request_json(self, url: str, method: str = "GET"):
        """Return (status_code, parsed_json, error). Retries transient failures."""
        last_error = "unknown error"
        status: int | None = None
        host = urlparse(url).netloc
        for attempt in range(MAX_RETRIES):
            self._limiter.wait(host)
            try:
                resp = self._session.request(method, url, timeout=self._timeout)
                status = resp.status_code
                if status in RETRY_STATUS:
                    last_error = f"HTTP {status}"
                    self._sleep_backoff(attempt)
                    continue
                if status != 200:
                    return status, None, f"HTTP {status}"
                try:
                    return status, resp.json(), None
                except ValueError:
                    return status, None, "malformed JSON"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._sleep_backoff(attempt)
        return status, None, last_error

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(BACKOFF_BASE * (2**attempt))

    # -- one company -----------------------------------------------------------------
    def fetch_company(self, company: Company) -> FetchResult:
        source = get_source(company.ats)
        result = FetchResult(company=company.name, ats=company.ats, slug=company.slug)
        if source is None:
            result.error = f"no source adapter for ats={company.ats!r}"
            return result
        if not company.slug:
            result.error = "empty slug"
            return result

        status, raw, error = self._request_json(
            source.jobs_url(company.slug), source.jobs_method
        )
        result.status_code = status
        if error is not None:
            result.error = error
            return result

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
        return result

    # -- fan out ---------------------------------------------------------------------
    def fetch_all(self, companies: list[Company]) -> list[FetchResult]:
        if not companies:
            return []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            return list(pool.map(self.fetch_company, companies))

    def close(self) -> None:
        self._session.close()
