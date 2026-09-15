"""One identity for one req, however you arrived at it.

A job reaches this tracker by several roads. Stripe's own Greenhouse board publishes it;
the Simplify new-grad README lists it; a Discord channel announces it. Those are three
rows in `postings` describing one application you can only submit once.

Knowing that is what lets the pages *say* so. Postings are split in two — company career
pages and job boards — and a row on one is never closed, hidden or refused for having a
twin on the other: a shared key renders as an "on company page" chip, a key matching an
application renders as "applied", and `rank.one_row_per_req` shows one pick per req
rather than the same job twice. Nothing here closes anything. This module derives the
key, and answers one question about collisions (`conflicting_api_rows`).

The key is derived from the URL, because the URL is the one thing every road agrees on.
Not from the URL *string*, though — that is where this gets interesting, and it is why
this module is bigger than a `.strip('/')`:

  * **The identity is usually inside the path.** `jobs.lever.co/artera-2/eae88c70-...`
    names the ATS, the board and the req, and that triple is exactly what the tracker
    already stores as `(ats, slug, ats_job_id)` for every api board. Read it out and a
    Discord link meets its own board's row even though the two strings differ.
  * **Query strings carry identity exactly once.** Greenhouse's `embed/job_app?for=X&
    token=Y` — the URL `browser.py` itself builds — puts both halves in the query, while
    `?gh_jid=` and `?utm_source=` next door are noise. So ATS extraction runs *before*
    the query is dropped, never after.
  * **Most stored URLs are not ATS URLs at all.** Measured over the live database:
    9,150 postings, of which 2,342 are Greenhouse-hosted, 843 Ashby and 302 Lever. The
    other 5,663 are careers-site links — `databricks.com`, `stripe.com`, `www.okta.com`
    — because 25 of 45 Greenhouse boards return a careers-site `absolute_url` (CLAUDE.md).
    A URL-only key would therefore miss the majority of the api corpus, which is why
    `key_from_identity` exists and is preferred for any row that has curated identity.

Pure: no I/O, no SQL, no clock. `store.py` computes and stores; `cli.py` decides when.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

# Where a row came from. Lower is the more authoritative source: a board row carries
# health, identity, a description and a form, while a feed row is a pointer to it.
#
# Nothing is closed on the strength of this any more. It survives for one question —
# `_is_feed` — which decides whether two rows sharing a key are a collision worth
# reporting or just the same job reached twice.
SOURCE_RANK = {"api": 0, "aggregator": 2, "plugin": 3}
# An unknown check_method is NOT a feed. A company missing from companies.yaml is far
# more likely to be a board we lost track of, and reading it as a feed would silence a
# real collision rather than report it. Measured: 2,805 open postings sit on 13 shared
# fallback keys (795 Databricks, 527 Stripe, 400 MongoDB) — exactly the shape that
# misreading would hide.
_UNRANKED = 1

# Query parameters that carry identity rather than tracking, and must therefore survive
# normalization. `gh_jid` is the Greenhouse job id, and it is the difference between
# `betterment.com/careers/current-openings/job?gh_jid=7184616` and the forty other live
# Betterment reqs at byte-identical URLs. Verified over the live database: it appears on
# 6,019 URLs and equals the stored `ats_job_id` on all 6,403 rows carrying it, with none
# differing. `t` (which holds `gh_src`) is tracking and is dropped.
_IDENTITY_PARAMS = ("gh_jid",)

# Greenhouse ids are numeric, Lever and Ashby ids are UUIDs. Kept loose (`[\w-]+`) rather
# than pinned to a UUID shape: a vendor tightening its id format should cost a missed
# duplicate, never a mis-parse that hands two different reqs one key.
_ID = r"[\w-]+"
_SLUG = r"[\w.-]+"

_LEVER = re.compile(rf"^jobs(?:\.eu)?\.lever\.co/({_SLUG})/({_ID})", re.IGNORECASE)
_GREENHOUSE = re.compile(
    rf"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io/({_SLUG})/jobs/({_ID})", re.IGNORECASE
)
_GREENHOUSE_EMBED = re.compile(
    r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_app", re.IGNORECASE
)
_ASHBY = re.compile(rf"^jobs\.ashbyhq\.com/({_SLUG})/({_ID})", re.IGNORECASE)
_SIMPLIFY = re.compile(rf"^(?:www\.)?simplify\.jobs/p/({_ID})", re.IGNORECASE)

# Workday is deliberately absent. Its human-facing URL carries a locale segment and a
# title-derived path that does NOT follow a rename (CLAUDE.md records two live examples),
# and the req number is not always in it — so a regex here would be guessing at a shape
# nothing in the corpus can check it against. `key_from_identity("workday", ...)` covers
# every Workday row we curate, and the URL fallback covers the rest. A key that is merely
# missing costs one duplicate row; a key that is wrong closes a real posting.


def _host_and_path(url: str) -> Optional[tuple[str, str, str]]:
    """(host, path, query) with the host lowercased and `www.`/port stripped."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host, parsed.path, parsed.query


def dedupe_key(url: str) -> Optional[str]:
    """A stable identity for the req behind a URL, or None if there is no usable one.

    None means only "this is not an http(s) URL" — an empty string, or a `javascript:`
    or `mailto:` scheme. **Every non-empty http(s) URL yields a key**, and that totality
    is what makes `store.backfill_dedupe_key` terminate: it drains on `dedupe_key IS NULL`
    and would otherwise re-examine the same unkeyable rows every night forever.

    Two normalization asymmetries below look like oversights and are not:

      * **The path is not lowercased.** Paths are case-sensitive on most servers, and this
        repo already has a live trap in that direction — Onehouse's board is
        `lever/Onehouse` and `lever/onehouse` 404s. Merging two genuinely distinct paths
        is worse than missing a duplicate, because a bad merge closes a real posting.
      * **The slug and id inside an ATS key are lowercased**, because those are known
        case-insensitive identifiers within a vendor's own namespace, and there the
        failure runs the other way: a case mismatch splitting one req into two rows.
    """
    parts = _host_and_path(url)
    if parts is None:
        return None
    host, path, query = parts
    target = f"{host}{path}"

    # ATS shapes first, while the query string is still in hand.
    m = _LEVER.match(target)
    if m:
        return f"lever:{m.group(1).lower()}:{m.group(2).lower()}"
    m = _GREENHOUSE.match(target)
    if m:
        return f"greenhouse:{m.group(1).lower()}:{m.group(2).lower()}"
    if _GREENHOUSE_EMBED.match(target):
        # The form URL `browser.py` builds: identity lives entirely in the query.
        params = parse_qs(query)
        slug = (params.get("for") or [""])[0]
        job_id = (params.get("token") or [""])[0]
        if slug and job_id:
            return f"greenhouse:{slug.lower()}:{job_id.lower()}"
    m = _ASHBY.match(target)
    if m:
        return f"ashby:{m.group(1).lower()}:{m.group(2).lower()}"
    m = _SIMPLIFY.match(target)
    if m:
        # Two Simplify links dedupe each other. A Simplify link and its redirect target
        # do not, and cannot without following the redirect — a documented blind spot.
        return f"simplify:{m.group(1).lower()}"

    # Identity-bearing parameters are appended in a fixed order, so two spellings of one
    # URL still agree. Without this, every board that links its reqs to a single careers
    # page collapses into one key — which is not a hypothetical: 13 such keys cover 2,805
    # open postings in the live database today.
    base = f"url:{host}{path.rstrip('/')}"
    params = parse_qs(query)
    identity = [
        f"{name}={params[name][0].lower()}"
        for name in _IDENTITY_PARAMS
        if params.get(name) and params[name][0]
    ]
    return f"{base}?{'&'.join(identity)}" if identity else base


def key_from_identity(ats: str, slug: str, ats_job_id: str) -> Optional[str]:
    """The same key minted from curated identity instead of read out of a link.

    This is the primary rule for `check_method: api` rows, and it is not a convenience.
    A Greenhouse `absolute_url` is often not a Greenhouse URL — 25 of 45 tracked boards
    return the employer's own careers site, and Stripe's is a search page with no req id
    in it at all. Keyed off the URL, those rows would sit in a namespace no feed link
    could ever reach, and system-wide dedupe would quietly cover only the third of the
    corpus that happens to link to its own ATS.

    But the tracker already holds `(ats, slug, ats_job_id)` for exactly those rows, and
    that triple is the tuple `dedupe_key` reconstructs from a URL. Verified against the
    live database: for all four ATS shapes the stored `ats_job_id` is byte-identical to
    the id in the hosted URL, so the two derivations land on the same string.

    Returns None when the ats has no known shape (`aggregator`, `plugin`, `bespoke`) or
    either component is empty — the caller then falls back to `dedupe_key`.
    """
    if not slug or not ats_job_id:
        return None
    family = ats.strip().lower()
    if family not in ("lever", "greenhouse", "ashby", "workday"):
        return None
    if family == "workday":
        # A Workday slug is the triple tenant/dc/site; the tenant alone identifies the
        # employer and the dc is a hostname detail, so key on tenant to stay stable if a
        # board moves data centre.
        slug = slug.split("/")[0]
    return f"{family}:{slug.strip().lower()}:{ats_job_id.strip().lower()}"


def rank_for(check_method: str) -> int:
    """Lower wins. An unknown method ranks last rather than raising."""
    return SOURCE_RANK.get((check_method or "").strip().lower(), _UNRANKED)


def _is_feed(row) -> bool:
    """Is this row redundant by construction — an aggregator or plugin import?

    The question `conflicting_api_rows` turns on, and it is deliberately not "is this
    api?". A row whose company nobody curates any more is not a feed, and treating it as
    one would make forgetting an entry in companies.yaml a way to hide a collision
    between two live reqs.
    """
    return rank_for(row["check_method"]) in (SOURCE_RANK["aggregator"], SOURCE_RANK["plugin"])


def conflicting_api_rows(rows: list) -> list:
    """The non-feed rows sharing one key, when there is more than one. Empty otherwise.

Two *board* rows on one key is a finding, not a duplicate:
    almost certainly a key too coarse to tell two live reqs apart. Two rows from different
    kinds of source is the ordinary case the two pages exist to render, and is not
    reported. `cmd_check` logs this at WARNING, and — since nothing is closed anywhere —
    that log line is the only way such a key ever becomes visible.
    """
    boards = [r for r in rows if not _is_feed(r)]
    return boards if len(boards) > 1 else []
