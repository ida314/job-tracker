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
    prev_ok_at = prior.last_ok_at if prior else None
    prev_name = prior.observed_board_name if prior else None
    observed = result.observed_board_name or prev_name

    # 7.3 Failure is not absence.
    if not result.ok or result.error:
        return BoardHealth(
            company.name,
            HealthStatus.FETCH_FAILED,
            consecutive_empty_runs=prev_empty,
            observed_board_name=prev_name,
            last_ok_at=prev_ok_at,
            detail=result.error or "fetch failed",
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
