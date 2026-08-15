"""Board-level invariants. Each one encodes a bug observed in v1 (DESIGN.md §7).

The single organizing rule: absence of data is never evidence of absence. A fetch
failure, an empty board, and a wrong-company board are three distinct states, and none
of them is "no openings".
"""

from __future__ import annotations

import re
from typing import Optional

from .models import BoardHealth, Company, FetchResult, HealthStatus

# Consecutive empty runs (on a board that was once populated) before we escalate.
EMPTY_ALERT_THRESHOLD = 2

# Consecutive *failed* runs before a board counts as persistently broken and its careers
# page is worth reading (DESIGN.md §8.2). Nights, not retries: fetch.py already burns
# MAX_RETRIES with backoff inside a single run, so anything transient has been absorbed
# one layer down before this counter moves at all. Two nights is past any plausible
# throttle or deploy window, and rewriting a hand-verified slug because a CDN hiccuped
# once is the expensive mistake here.
REPAIR_FAILURE_THRESHOLD = 2


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def identity_matches(expected: str, observed: str) -> bool:
    """Fuzzy identity: equal, or one contains the other, after normalization.

    Tolerates 'Confluent' vs 'Confluent, Inc.' while still catching ashby/cedar, where
    the observed org slug is an entirely different token.
    """
    e, o = _normalize(expected), _normalize(observed)
    if not e or not o:
        return True  # nothing to compare against — don't cry drift on missing data
    return e == o or e in o or o in e


def evaluate(
    company: Company,
    result: FetchResult,
    prior: Optional[BoardHealth],
    now: str,
    ever_nonempty: bool,
) -> BoardHealth:
    prev_empty = prior.consecutive_empty_runs if prior else 0
    prev_failures = prior.consecutive_failures if prior else 0
    prev_ok_at = prior.last_ok_at if prior else None
    prev_name = prior.observed_board_name if prior else None
    observed = result.observed_board_name or prev_name

    # 7.3 Failure is not absence.
    #
    # This is the only branch that advances consecutive_failures. Every other outcome
    # below leaves it at its default of 0, which is deliberate rather than an oversight:
    # drift and empty are *answers* — the fetch worked and the board said something — so
    # a streak of failed fetches has ended even though the board is still unhealthy.
    if not result.ok or result.error:
        return BoardHealth(
            company.name,
            HealthStatus.FETCH_FAILED,
            consecutive_empty_runs=prev_empty,
            observed_board_name=prev_name,
            last_ok_at=prev_ok_at,
            detail=result.error or "fetch failed",
            consecutive_failures=prev_failures + 1,
        )

    # 7.2 Reachability is not identity. Contents discarded on drift.
    if company.expected_board_name and result.observed_board_name:
        if not identity_matches(company.expected_board_name, result.observed_board_name):
            return BoardHealth(
                company.name,
                HealthStatus.IDENTITY_DRIFT,
                consecutive_empty_runs=prev_empty,
                observed_board_name=result.observed_board_name,
                last_ok_at=prev_ok_at,
                detail=(
                    f"expected {company.expected_board_name!r}, "
                    f"saw {result.observed_board_name!r}"
                ),
            )

    # 7.1 Empty is not zero.
    if not result.postings:
        empty = prev_empty + 1
        alerting = ever_nonempty and empty >= EMPTY_ALERT_THRESHOLD
        detail = f"{empty} consecutive empty run(s)"
        if alerting:
            detail = f"ALERT: {empty} empty runs after being populated"
        elif not ever_nonempty:
            detail = f"{empty} empty run(s) — board has never been populated"
        return BoardHealth(
            company.name,
            HealthStatus.SUSPECT_EMPTY,
            consecutive_empty_runs=empty,
            observed_board_name=observed,
            last_ok_at=prev_ok_at,
            detail=detail,
            alerting=alerting,
        )

    # Healthy, non-empty, identity confirmed (or unpinned).
    return BoardHealth(
        company.name,
        HealthStatus.OK,
        consecutive_empty_runs=0,
        observed_board_name=observed,
        last_ok_at=now,
        detail=f"{len(result.postings)} postings",
    )


def is_degraded(health: BoardHealth) -> bool:
    """Is this board broken enough to fail the whole run?

    Deliberately narrower than `status != OK`. A board can be legitimately non-OK
    forever: dbt Labs and Root Insurance are correct slugs with genuinely zero reqs
    and have never been populated, so they are SUSPECT_EMPTY on every run. Treating
    those as failures means the run is red every night, which is the same as having
    no signal at all — the night something actually breaks would look identical.

    So: FETCH_FAILED and IDENTITY_DRIFT always count (data is missing or wrong), and
    SUSPECT_EMPTY counts only once `alerting` is set, which by construction requires
    the board to have been populated before. A board that went from 500 postings to
    zero is an emergency; a board that was always zero is a fact about the company.
    """
    return (
        health.status in (HealthStatus.FETCH_FAILED, HealthStatus.IDENTITY_DRIFT)
        or health.alerting
    )


def needs_repair(
    health: BoardHealth, threshold: int = REPAIR_FAILURE_THRESHOLD
) -> bool:
    """Is this board broken in a way a *new slug* could plausibly fix? (DESIGN.md §8.2)

    Deliberately not `is_degraded()`, and the two differences both matter.

    `is_degraded` answers "should tonight's run go red", so it fires on the first failed
    fetch — which is exactly when you must not go reading careers pages and rewriting
    curated slugs, because most single-night failures fix themselves. This one answers
    "has this been broken long enough that the board itself probably moved".

    Three triggers:

      IDENTITY_DRIFT  — immediately. Drift is not transient: the assertion is
                        deterministic over two stable strings, so one observation is
                        worth as much as ten, and the board we are reading is already
                        known to be the wrong company's.
      FETCH_FAILED    — after `threshold` consecutive nights.
      SUSPECT_EMPTY   — only when `alerting`. This trigger is an addition to what §8
                        anticipated, and it is the one that covers the failure mode this
                        repo cites most: `greenhouse/hubspot` is a real, reachable,
                        permanently empty board, and Mercury and Vercel both left one
                        behind when they migrated to Greenhouse. A dead board never
                        presents as FETCH_FAILED — it answers 200 with an empty array
                        forever — so leaving it out would put the canonical case outside
                        the reach of the thing built to fix it.

    dbt Labs and Root Insurance are excluded by `alerting`, which by construction
    requires the board to have been populated at some point. They are correct slugs with
    genuinely zero reqs, they report SUSPECT_EMPTY every night, and "repairing" them
    nightly would corrupt hand-verified data to fix nothing.
    """
    if health.status is HealthStatus.IDENTITY_DRIFT:
        return True
    if health.status is HealthStatus.FETCH_FAILED:
        return health.consecutive_failures >= threshold
    if health.status is HealthStatus.SUSPECT_EMPTY:
        return health.alerting
    return False
