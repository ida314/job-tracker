"""The Simplify new-grad listings board.

The highest-yield source for new-grad roles specifically, including employers this
tracker does not curate. It was a `check_method: aggregator` entry in companies.yaml,
parsed out of the README's HTML table; it is a job board plugin now, for two reasons.

The first is that the README was never the good copy. The repo publishes the same data as
JSON — `company_name`, a direct link to the employer's own application page, an epoch
`date_posted`, a category, and an explicit `active` flag — where the README carries an
HTML table that is restyled every hiring cycle and hides multi-location cells behind a
`<details>` summary. Every field the parser used to reconstruct by regex is simply named
here, and the direct link is what lets a row meet its own board's row under one dedupe
key. It is also much larger: ~3,000 active listings against the table's ~460.

The second is that a feed is not a company. It was never curation — the entry carried no
tier, its "board" was a README, and `repair` had to be kept away from it — and a plugin
group is the shape the tracker already has for that.

**A single module, not a package.** `pyproject.toml` lists packages by hand, and a
subpackage left out of that list is absent only from the container image, where a feed
that imports nothing looks exactly like a quiet night. A module inside `jobtracker.plugins`
ships with the package it lives in and cannot be forgotten.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models import Company, Posting
from .base import Plugin, register

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
    ".github/scripts/listings.json"
)

# The group name the aggregator entry already wrote under. Kept byte-identical on
# purpose: `postings` is keyed by `(company, ats_job_id)`, and every verdict, override,
# decision and application ever recorded against this feed hangs off that pair. Renaming
# it would orphan all of them and re-import the corpus as new.
GROUP = "Simplify New-Grad-Positions"


def _day(epoch: object) -> Optional[str]:
    """An epoch second as a plain ISO day, or None. Never today on failure."""
    try:
        seconds = int(epoch)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _locations(entry: dict) -> str:
    places = [str(p).strip() for p in (entry.get("locations") or []) if str(p).strip()]
    return "; ".join(places)


def _description(entry: dict) -> str:
    """What this board knows about a listing beyond its title.

    A listing carries no prose, so this is assembled from its fields rather than fetched.
    It is not decoration: a NULL description drops a row out of `level`'s queue *and* out
    of `matches_needing_judgment`, so it would sit in the table and never reach the
    product. Sponsorship rides along here and goes no further — as a criteria token it
    would become a gate applied before any title was read.
    """
    bits = []
    for label, value in (
        ("Category", entry.get("category")),
        ("Locations", _locations(entry)),
        ("Sponsorship", entry.get("sponsorship")),
    ):
        text = str(value or "").strip()
        if text:
            bits.append(f"{label}: {text}")
    degrees = [str(d).strip() for d in (entry.get("degrees") or []) if str(d).strip()]
    if degrees:
        bits.append("Degrees: " + ", ".join(degrees))
    return "\n".join(bits)


def _is_open(entry: dict) -> bool:
    """`active` is the board retracting a req; `is_visible` is it withdrawing one.

    Both mean the listing is no longer published, and the tracker has one word for that.
    """
    return bool(entry.get("active")) and bool(entry.get("is_visible", True))


class Simplify(Plugin):
    name = "simplify"
    tag = "simplify"
    summary = "import the Simplify new-grad listings board"
    page_format = "json"

    def defaults(self) -> dict:
        return {
            # Off, like every other import plugin, even though this replaces a feed
            # that was already running nightly. The rule is that installing one never
            # starts reading anything, and an upgrade that quietly began fetching 13MB
            # from a new host is exactly what it is for. Switching it back on is
            # `jobtracker plugins enable simplify`, or the card on /settings — a
            # decision you type, which is what plugins.yaml holds.
            "enabled": False,
            # A listing that stops being republished is retracted through `closed_ids`,
            # so age is the backstop rather than the mechanism.
            "expire_after_days": 90,
            # Settable because the repo has renamed this path before, and a board that
            # moved should be a line in plugins.yaml rather than a release.
            "board_url": LISTINGS_URL,
        }

    def validate(self, settings: dict) -> None:
        from .settings import InvalidSettings

        if settings.get("expire_after_days", 0) < 0:
            raise InvalidSettings("simplify: `expire_after_days` cannot be negative")
        url = str(settings.get("board_url") or "")
        if not url.startswith("https://"):
            raise InvalidSettings("simplify: `board_url` must be an https URL")

    def company(self, settings: dict) -> Company:
        return Company(
            name=GROUP,
            ats="plugin",
            slug="",
            tier=None,
            category="new-grad-listings",
            check_method="plugin",
            expected_board_name=None,
            notes="Imported by the simplify plugin. Not curation — see docs/plugins.md.",
        )

    # -- one page, and it is the whole board ------------------------------------------
    def page_urls(self, settings: dict, today: str) -> list:
        return [str(settings.get("board_url") or LISTINGS_URL)]

    def page_error(self, raw: object) -> Optional[str]:
        """Why this payload is not the listing, or None.

        Zero rows is deliberately **not** an error. A well-formed empty listing is a
        health question — `evaluate_plugin` flags an empty snapshot every run — and
        answering it here would turn one policy into two.

        A list of things that are not listings is an error, because that is what a
        rename or a format change looks like, and it parses to zero rows under any
        tolerant reader.
        """
        if not isinstance(raw, list):
            return "payload is not a list of listings"
        if raw and not any(isinstance(e, dict) and e.get("id") for e in raw[:50]):
            return "listings carry no id — the payload's shape has changed"
        return None

    def page_ids(self, raw: object) -> list:
        return [str(e.get("id")) for e in raw if isinstance(e, dict) and e.get("id")]

    def closed_ids(self, raw: object) -> list:
        """Everything the board says is no longer published.

        Most of these were never imported — the file carries the repo's whole history,
        some 20,000 entries against 3,000 live ones. That is fine and is why
        `store.close_feed_postings` filters by company and chunks: naming an id we do not
        hold costs nothing, and missing one we do hold would leave a filled req open for
        ninety days waiting on age.
        """
        return [
            str(e.get("id"))
            for e in raw
            if isinstance(e, dict) and e.get("id") and not _is_open(e)
        ]

    def parse_page(self, group: str, raw: object, settings: dict, today: str):
        postings: list[Posting] = []
        unparsed = 0
        skipped = 0
        for entry in raw:
            if not isinstance(entry, dict):
                unparsed += 1
                continue
            job_id = str(entry.get("id") or "").strip()
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
            if not (job_id and title and url):
                unparsed += 1
                continue
            if not _is_open(entry):
                skipped += 1
                continue

            employer = str(entry.get("company_name") or "").strip()
            postings.append(
                Posting(
                    company=group,
                    ats_job_id=job_id,
                    # "Employer — Role", the shape every other feed writes and the one
                    # criteria tokens, `decisions.title` and the eval corpus all read.
                    # The employer is also its own column now; this stays for them.
                    title=f"{employer} — {title}" if employer else title,
                    url=url,
                    location=_locations(entry),
                    employer=employer or None,
                    posted_at=str(entry.get("date_posted") or "") or None,
                    posted_on=_day(entry.get("date_posted")),
                    description=_description(entry),
                )
            )
        return postings, unparsed, skipped

    def describe_cursor(self, cursor: str) -> str:
        """There is no cursor. A snapshot board has nothing to remember."""
        return "reads the whole listing every run"


register(Simplify())
