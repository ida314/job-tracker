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
