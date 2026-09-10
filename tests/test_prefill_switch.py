"""The switched-off world: `config.PREFILL_ENABLED` is False.

Everything else in this suite runs with the prefill/browser half forced **on** (see
`conftest.py`), because the feature is mothballed rather than deleted and its tests are
what keep it turnable-back-on. This module is the other side: with the switch off, what
does each surface actually say?

The claim under test is not "it does nothing". It is:

  * **no browser is ever launched**, at either door, and the guard is in the function
    that launches one rather than only in the UI;
  * **nothing renders a control that would refuse** — a dead button is worse than none;
  * **nothing reads as broken.** Switched off is a decision somebody typed, so it is
    named where you would go looking, and the answers already stored are kept.
"""

import argparse
import importlib

import pytest

from jobtracker import browser, config, dashboard, prefill, server, store
from jobtracker.models import Company, Decision, Posting, Verdict

TODAY = "2026-09-10"


@pytest.fixture
def off(monkeypatch):
    """The production default, stated rather than inherited."""
    monkeypatch.setattr(config, "PREFILL_ENABLED", False)


# -- the switch itself ---------------------------------------------------------------
def test_the_switch_is_off_unless_the_environment_turns_it_on(monkeypatch):
    """Reloaded with a clean environment, because the default is the whole point.

    The autouse fixture forces it on for every other test, so the shipped default is
    the one fact this suite could otherwise never see.
    """
    monkeypatch.delenv("JOBTRACKER_PREFILL", raising=False)
    try:
        importlib.reload(config)
        assert config.PREFILL_ENABLED is False
        assert config.prefill_off() == config.PREFILL_OFF
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False),
])
def test_the_environment_variable_reads_as_a_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("JOBTRACKER_PREFILL", raw)
    assert config._flag("JOBTRACKER_PREFILL", False) is expected


def test_switched_on_is_the_world_the_rest_of_the_suite_tests():
    """The autouse fixture is doing what it claims, or every other test here is vacuous."""
    assert config.PREFILL_ENABLED is True
    assert config.prefill_off() is None


# -- no browser, at either door ------------------------------------------------------
def test_fill_application_refuses_before_it_could_launch_anything(off):
    """The guard that matters, in the one function that opens a browser at a third
    party's form. A switch enforced only in the UI is one a later caller walks past."""
    with pytest.raises(browser.BrowserUnavailable) as exc:
        browser.fill_application(
            conn=None, company=Company(name="Acme", ats="greenhouse", slug="acme"),
            ats_job_id="1", url="https://example.invalid/x", answers=None,
            today=TODAY, user_data_dir="/nonexistent",
        )
    assert "switched off" in str(exc.value)


def test_the_browser_reports_the_switch_rather_than_a_missing_install(off):
    """Two different states, and only one of them is a fault. Telling somebody to run
    `playwright install` about a feature they turned off is the wrong repair."""
    reason = browser.unavailable_reason()
    assert reason == config.PREFILL_OFF
    assert "playwright" not in reason.lower()


def test_planning_reports_the_switch_before_a_missing_answer_bank(off):
    """A disabled plugin is never asked for its `unavailable_reason`; same rule here.

    With no bank *and* no switch, the honest answer is the switch: fixing answers.yaml
    would change nothing.
    """
    ctx = argparse.Namespace(answers=None)
    assert prefill.unavailable_reason(ctx) == config.PREFILL_OFF


# -- the CLI: exit 0, because you asked for this --------------------------------------
def test_prefill_returns_before_it_opens_anything(off, capsys):
    """An empty Namespace is the assertion: it never reaches `--db`, `--criteria` or
    the answer bank, so there is nothing to configure for a pass that will not run."""
    assert cli().cmd_prefill(argparse.Namespace()) == 0
    assert "switched off" in capsys.readouterr().out


def test_apply_to_returns_before_it_opens_anything(off, capsys):
    assert cli().cmd_apply_to(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "switched off" in out and "JOBTRACKER_PREFILL=1" in out


def test_prepare_still_rescores_and_still_exits_zero(off, tmp_path, capsys):
    """The half that survives the switch, and the half that must not fail the run.

    `prepare` is nightly. Reporting "NOT READY" about three picks nobody will prefill
    would make it exit 2 every night for a decision — the dbt Labs trap by another
    route — so with the switch off it reports the picks and stops.
    """
    db = tmp_path / "s.db"
    _seed_pick(db)
    code = cli().cmd_prepare(_prepare_args(db))
    out = capsys.readouterr().out
    assert code == 0
    assert "Acme" in out and "switched off" in out
    assert "NOT READY" not in out and "prefill 0/" not in out


def test_prepare_is_still_the_command_that_scores(off, tmp_path):
    """Rescoring is not part of the switch: without it `today` opens on a stale order."""
    db = tmp_path / "s.db"
    _seed_pick(db, score=None)
    cli().cmd_prepare(_prepare_args(db))
    conn = store.connect(db)
    try:
        assert store.ranked_matches(conn)[0]["score"] is not None
    finally:
        conn.close()


# -- serve: refusals that name the switch ---------------------------------------------
def _handler(db_path, answers_path=None):
    from tests.test_server import _handler_for
    return _handler_for(db_path, config.CRITERIA_YAML, answers_path)


def test_the_apply_to_endpoint_refuses_and_starts_no_session(off, tmp_path):
    db = tmp_path / "s.db"
    _seed_pick(db)
    from jobtracker import live

    # Whatever the module-level session happens to be — `live` is a process global and
    # other tests set it — this must not change it. That is the claim: `_api_apply_to`
    # is the only thing that starts a session, so with it refused every
    # `/api/session/*` endpoint is inert by construction rather than by nine guards.
    before = live.current()
    res = _handler(db)._api_apply_to({"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is False and res["error"] == config.PREFILL_OFF
    assert live.current() is before


def test_the_rebuild_endpoint_refuses(off, tmp_path):
    db = tmp_path / "s.db"
    _seed_pick(db)
    res = _handler(db)._api_prefill({"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is False and res["error"] == config.PREFILL_OFF


def test_the_apply_page_names_the_switch_instead_of_no_window_is_open(off):
    conn = store.connect(":memory:")
    try:
        page = server.render_apply(conn, None)
    finally:
        conn.close()
    assert "switched off" in page
    # "No window is open" is true and useless here: the button that opens one is gone,
    # so it would read as a working feature nobody had started.
    assert "No window is open" not in page


def test_settings_keeps_the_gaps_and_stops_asking_about_them(off, tmp_path):
    """The rows are a record of what forms asked; the questions are work for a fill
    that will not happen. So the count stays and the cards go."""
    conn = store.connect(":memory:")
    store.record_gap(conn, "current_employer", "Who is your current employer?",
                     "text", "Stripe", TODAY, None)
    conn.commit()
    try:
        page = server.render_settings(conn, tmp_path / "answers.yaml")
    finally:
        conn.close()
    assert "Unanswered questions (1)" in page
    assert "switched off" in page
    assert "Who is your current employer?" not in page


# -- the dashboard --------------------------------------------------------------------
def test_the_picks_carry_no_prefill_control_and_no_prefill_line(off, tmp_path):
    db = tmp_path / "s.db"
    _seed_pick(db)
    conn = store.connect(db)
    try:
        page = dashboard.build_dashboard(conn, [_acme()], TODAY, interactive=True)
    finally:
        conn.close()
    for markup in ('class="apply-to"', 'class="pick-rebuild"'):
        assert markup not in page, markup
    # Not "no prefill yet — jobtracker prefill" either: an instruction you cannot follow,
    # repeated on every card, every day.
    assert "no prefill yet" not in page
    # And the rest of the card is untouched.
    assert "I applied" in page and "Acme" in page


def test_a_stored_plan_is_not_reported_when_the_feature_is_off(off, tmp_path):
    """The plans outlive the switch — they are not deleted — and must not be read back
    onto a page whose only way to act on them is gone."""
    db = tmp_path / "s.db"
    _seed_pick(db)
    conn = store.connect(db)
    store.record_plan(conn, "Acme", "1", "[]", 16, 3, "h", TODAY)
    conn.commit()
    try:
        page = dashboard.build_dashboard(conn, [_acme()], TODAY, interactive=True)
    finally:
        conn.close()
    assert "prefill 13/16" not in page


# -- helpers ---------------------------------------------------------------------------
def cli():
    from jobtracker import cli as cli_mod
    return cli_mod


def _acme():
    return Company(name="Acme", ats="greenhouse", slug="acme", tier=1,
                   check_method="api")


def _seed_pick(db_path, jid="1", score=90.0):
    from jobtracker.tasks.judge import RankJudgment

    conn = store.connect(db_path)
    store.sync_postings(conn, "Acme",
                        [Posting("Acme", jid, "Backend Engineer, New Grad",
                                 f"https://example.invalid/{jid}")], TODAY)
    store.record_verdict(conn, Verdict("Acme", jid, Decision.MATCH, "r", "rules"), TODAY)
    store.record_judgment(conn, "Acme", jid,
                          RankJudgment("strong", "strong", "low", "why"), "h", TODAY)
    if score is not None:
        store.set_score(conn, "Acme", jid, score, TODAY)
    conn.commit()
    conn.close()


def _prepare_args(db_path):
    return argparse.Namespace(
        db=str(db_path), since=TODAY, count=3,
        criteria=str(config.CRITERIA_YAML), profile=str(config.PROFILE_YAML),
        companies=str(config.COMPANIES_YAML), answers=None, resume_source=None,
    )
