"""The three invariants: failure != absence, reachability != identity, empty != zero."""

from jobtracker.health import (
    EMPTY_ALERT_THRESHOLD,
    evaluate,
    identity_matches,
    is_degraded,
)
from jobtracker.models import BoardHealth, Company, FetchResult, HealthStatus, Posting


def _company(**kw):
    base = dict(name="Acme", ats="greenhouse", slug="acme", check_method="api")
    base.update(kw)
    return Company(**base)


def _ok_result(n=1, name="Acme"):
    postings = [Posting("Acme", str(i), "Software Engineer", "u") for i in range(n)]
    return FetchResult("Acme", "greenhouse", "acme", ok=True, status_code=200,
                       observed_board_name=name, postings=postings)


def test_fetch_failure_is_not_absence():
    res = FetchResult("Acme", "greenhouse", "acme", ok=False, status_code=500,
                      error="HTTP 500")
    h = evaluate(_company(), res, None, "2026-07-01", ever_nonempty=True)
    assert h.status is HealthStatus.FETCH_FAILED
    # A prior empty-run counter is preserved, not reset, on failure.
    prior = BoardHealth("Acme", HealthStatus.SUSPECT_EMPTY, consecutive_empty_runs=3)
    h2 = evaluate(_company(), res, prior, "2026-07-01", ever_nonempty=True)
    assert h2.consecutive_empty_runs == 3


def test_identity_drift_discards_contents():
    res = _ok_result(n=5, name="Cedar Real Estate")
    h = evaluate(_company(expected_board_name="Cedar Health"), res, None, "d", True)
    assert h.status is HealthStatus.IDENTITY_DRIFT


def test_identity_ok_when_fuzzy_match():
    res = _ok_result(n=5, name="Confluent, Inc.")
    h = evaluate(_company(expected_board_name="Confluent"), res, None, "d", True)
    assert h.status is HealthStatus.OK


def test_never_populated_empty_does_not_alert():
    res = _ok_result(n=0)
    h = evaluate(_company(), res, None, "d", ever_nonempty=False)
    assert h.status is HealthStatus.SUSPECT_EMPTY
    assert h.alerting is False  # dbtlabsinc / root: always empty, never pages


def test_empty_after_populated_escalates():
    res = _ok_result(n=0)
    prior = BoardHealth("Acme", HealthStatus.SUSPECT_EMPTY,
                        consecutive_empty_runs=EMPTY_ALERT_THRESHOLD - 1)
    h = evaluate(_company(), res, prior, "d", ever_nonempty=True)
    assert h.status is HealthStatus.SUSPECT_EMPTY
    assert h.consecutive_empty_runs == EMPTY_ALERT_THRESHOLD
    assert h.alerting is True


def test_ok_resets_empty_counter():
    prior = BoardHealth("Acme", HealthStatus.SUSPECT_EMPTY, consecutive_empty_runs=5)
    h = evaluate(_company(), _ok_result(n=2), prior, "2026-07-09", ever_nonempty=True)
    assert h.status is HealthStatus.OK
    assert h.consecutive_empty_runs == 0
    assert h.last_ok_at == "2026-07-09"


def test_identity_matches_helper():
    assert identity_matches("Confluent", "Confluent, Inc.")
    assert identity_matches("ramp", "ramp")
    assert not identity_matches("Cedar Health", "cedar-realestate")
    assert identity_matches("anything", "")  # missing data never cries drift


# -- is_degraded: what actually fails an unattended run --------------------------------
def test_degraded_on_fetch_failure_and_drift():
    for status in (HealthStatus.FETCH_FAILED, HealthStatus.IDENTITY_DRIFT):
        assert is_degraded(BoardHealth("Acme", status)) is True


def test_never_populated_empty_board_is_not_degraded():
    """The dbt Labs / Root Insurance case — permanently empty, permanently fine.

    These are correct slugs with genuinely zero reqs. They report SUSPECT_EMPTY on
    every run forever. If that failed the run, the scheduler would be red every night
    and the signal would be worthless.
    """
    h = evaluate(_company(), _ok_result(n=0), None, "d", ever_nonempty=False)
    assert h.status is HealthStatus.SUSPECT_EMPTY
    assert h.alerting is False
    assert is_degraded(h) is False


def test_board_that_went_empty_after_being_populated_is_degraded():
    """The other empty board: 500 postings yesterday, zero today. That's an emergency."""
    prior = BoardHealth("Acme", HealthStatus.SUSPECT_EMPTY,
                        consecutive_empty_runs=EMPTY_ALERT_THRESHOLD - 1)
    h = evaluate(_company(), _ok_result(n=0), prior, "d", ever_nonempty=True)
    assert h.alerting is True
    assert is_degraded(h) is True


def test_healthy_board_is_not_degraded():
    assert is_degraded(evaluate(_company(), _ok_result(n=3), None, "d", True)) is False
