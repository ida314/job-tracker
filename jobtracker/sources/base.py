"""Source adapter interface + registry.

Each ATS gets one adapter, and it is the *only* place that vendor's JSON shape is
known. Everything downstream speaks in normalized Posting objects. Adapters are pure:
they build URLs and parse payloads; they never touch the network (that is fetch.py).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..models import Posting


def iso_day(raw: object) -> Optional[str]:
    """The date part of an ISO-8601 timestamp, or None if it is not one.

    Shared by Greenhouse and Ashby, whose formats differ only in offset spelling and
    fractional seconds — `2026-08-01T01:46:42-04:00` vs `2026-08-01T01:57:58.337+00:00`.
    Deliberately does *not* convert to local time: a posting is dated by the day the
    vendor says it was posted, and shifting that by a timezone would move ~4% of
    postings by a day for no gain.
    """
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    # Some boards send a bare date; accept it, reject anything else.
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


class Source:
    ats: str = ""
    jobs_method: str = "GET"
    # Rows per request, for boards that will not return a whole board in one call.
    # 0 means "one request returns everything", which is true of Greenhouse, Lever and
    # Ashby and is why this stayed unused until Workday. See `jobs_body` below.
    page_size: int = 0

    def jobs_url(self, slug: str) -> str:
        raise NotImplementedError

    def jobs_page_url(self, slug: str, offset: int) -> str:
        """The URL for one page of results.

        Paged boards split into two kinds and both are in use here: Workday carries the
        offset in a POST body and reuses one URL, so it takes this default; Amazon is a
        GET and carries the offset in the query string, so it overrides. Ignored entirely
        when `page_size` is 0.
        """
        return self.jobs_url(slug)

    def jobs_body(self, slug: str, offset: int) -> Optional[dict]:
        """The request body for one page, or None for a GET with no body.

        Only meaningful when `page_size` is set. The adapter owns the body shape the
        same way it owns the URL — `fetch.py` sends whatever this returns and knows
        nothing about facets, offsets or vendor pagination vocabulary.
        """
        return None

    def jobs_page_error(self, raw: object) -> Optional[str]:
        """Why this payload is not a usable page of results, or None if it is.

        This exists because on a paged board `parse_jobs` returning `[]` is ambiguous in
        a way it never is for a single-call board: it means either "the last page" or
        "we asked wrongly and got a shape we do not understand". Workday answers a
        `limit` above its cap with a payload that simply has no `jobPostings` key —
        a 200, valid JSON, and zero rows. Read as an empty page that would silently
        close every posting on the board (DESIGN.md §3.4), so the adapter is given a
        way to say "that was not an answer" and the fetch becomes FETCH_FAILED.

        This lives on the base class rather than inside one adapter because a second
        paged vendor, tried the same day, did the same thing independently: amazon.jobs
        answers an over-cap `result_limit` with `"jobs": null`, and its 10,000-result
        offset ceiling with a JSON error body. That adapter was dropped for the ceiling;
        the shape it proved is general.
        """
        return None

    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        raise NotImplementedError

    def posting_url(self, slug: str, ats_job_id: str) -> str:
        """The human-facing URL for a posting, when the payload cannot carry one.

        Greenhouse, Lever and Ashby all ship an absolute URL on the row, so they leave
        this alone. Workday's bulk row carries only a site-relative `externalPath` and
        the payload names no host, so the URL can only be built from the slug — which
        `parse_jobs` is not given. `fetch.py` calls this for any posting that came back
        without a URL; an empty return leaves it empty.
        """
        return ""

    # Identity: how we confirm the board belongs to the right company (DESIGN.md §7.2).
    # Greenhouse has a dedicated endpoint; Ashby/Lever derive identity from the payload.
    def identity_url(self, slug: str) -> Optional[str]:
        return None

    def parse_identity(self, raw: object) -> Optional[str]:
        return None

    def identity_from_jobs(self, raw: object) -> Optional[str]:
        return None

    # Per-posting description. Only needed where the bulk jobs payload omits it:
    # Ashby and Lever both ship `descriptionPlain` in the list call, so they leave
    # this alone and cost zero extra requests. Greenhouse is the exception, because
    # we deliberately drop ?content=true from the bulk call (it blows the timeout on
    # large boards — CLAUDE.md), so its descriptions are fetched one at a time and
    # only for postings the LLM pass actually needs to read.
    def job_detail_url(self, slug: str, ats_job_id: str) -> Optional[str]:
        return None

    def parse_job_detail(self, raw: object) -> Optional[str]:
        """Plain-text description from a single-job payload."""
        return None

    def parse_job_detail_posted_at(self, raw: object) -> Optional[str]:
        """A better posted timestamp from the single-job payload, if there is one.

        Only Greenhouse has one: the bulk call exposes `updated_at`, which moves every
        time anyone edits the req, while the detail payload carries `first_published`.
        """
        return None

    # Vendor timestamp -> plain ISO date. `today` is a parameter rather than a clock
    # read because adapters are pure (DESIGN.md §3.1) and because one source dates its
    # rows relatively, so "now" has to come from the caller that already knows it.
    def normalize_posted_at(self, raw: object, today: str) -> Optional[str]:
        return iso_day(raw)

    # The application form's questions, for prefill. Greenhouse publishes these keylessly
    # and completely; nobody else does. Ashby's per-job posting-api answers 401 and its
    # GraphQL introspection is off, and Lever's public postings API carries no custom
    # questions at all — so both return None here and their forms are learned from the
    # DOM on the first browser visit instead (docs/prefill.md).
    #
    # Returning None is not a stub to be filled in later. It is the honest state, and it
    # is what makes the DOM path load-bearing rather than a fallback nobody exercises.
    def application_form_url(self, slug: str, ats_job_id: str) -> Optional[str]:
        return None

    def parse_application_form(self, raw: object) -> list:
        """Questions from an application-form payload, as `FormField`s."""
        return []


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    _REGISTRY[source.ats] = source
    return source


def get_source(ats: str) -> Optional[Source]:
    return _REGISTRY.get(ats)


def api_sources() -> set[str]:
    return set(_REGISTRY)
