"""Scoring and queue semantics for the nightly ranking pass.

The tests that matter most here are the ones about absence. An unjudged posting must
not be scored zero (that buries a model failure at the bottom of the list where nobody
looks), and an undated one must not be treated as posted today (that floats stale reqs
to the top). Both failures would look exactly like a working ranking.
"""

import pytest

from jobtracker import config, rank, store
from jobtracker.criteria import load_criteria
from jobtracker.llm.client import RankJudgment
from jobtracker.models import Decision, Posting, Verdict
from jobtracker.profile import load_profile


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


@pytest.fixture(scope="module")
def profile():
    return load_profile(config.PROFILE_YAML)


def _row(**over):
    base = {
        "company": "Acme", "ats_job_id": "1", "title": "Software Engineer",
        "location": "New York, NY", "url": "u", "posted_on": "2026-08-02",
        "first_seen": "2026-08-02", "backend_fit": "strong", "growth": "strong",
        "entry_risk": "low", "why": "", "score": None, "prose_hash": "h",
        "applied_status": None, "deferral_kind": None, "deferral_until": None,
    }
    base.update(over)
    return base


# sqlite3.Row exposes .keys(); dicts do too, so plain dicts stand in for rows.
def _score(row, profile, criteria, tier=1, today="2026-08-02"):
    return rank.score_posting(row, profile, criteria, tier, today)


# -- absence is not a low score ----------------------------------------------------
def test_an_unjudged_posting_scores_none_not_zero(profile, criteria):
    """A model failure must be a visible gap, not a posting silently ranked last."""
    assert _score(_row(backend_fit=None), profile, criteria) is None
    assert _score(_row(backend_fit="excellent"), profile, criteria) is None  # off-enum
    assert _score(_row(entry_risk="unknown"), profile, criteria) is None


def test_an_undated_posting_is_mid_scale_not_fresh(profile, criteria):
    """NULL posted_on must read as 'unknown', never as 'posted today'."""
    fresh = _score(_row(posted_on="2026-08-02"), profile, criteria).terms["recency"]
    unknown = _score(_row(posted_on=None), profile, criteria).terms["recency"]
    old = _score(_row(posted_on="2026-05-01"), profile, criteria).terms["recency"]
    assert old < unknown < fresh


# -- the score behaves the way the profile says it should --------------------------
def test_a_better_posting_outscores_a_worse_one(profile, criteria):
    best = _score(_row(), profile, criteria, tier=1)
    worst = _score(
        _row(backend_fit="none", growth="none", entry_risk="high",
             location="Bengaluru, India", posted_on="2025-01-01"),
        profile, criteria, tier=7,
    )
    assert best.total > worst.total


def test_entry_risk_subtracts(profile, criteria):
    low = _score(_row(entry_risk="low"), profile, criteria)
    high = _score(_row(entry_risk="high"), profile, criteria)
    assert high.total < low.total
    assert high.terms["entry_risk"] < 0  # a penalty, not a smaller bonus


def test_recency_decays_by_half_every_half_life(profile, criteria):
    half = profile.recency_half_life_days
    today = "2026-08-02"
    from datetime import date, timedelta

    one = (date.fromisoformat(today) - timedelta(days=int(half))).isoformat()
    fresh = rank.recency_value(today, today, half)
    aged = rank.recency_value(one, today, half)
    assert fresh == pytest.approx(1.0)
    assert aged == pytest.approx(0.5, abs=0.02)


def test_a_future_date_clamps_rather_than_exceeding_the_scale(profile, criteria):
    """A vendor clock a few hours ahead should not score above a same-day posting."""
    assert rank.recency_value("2026-09-01", "2026-08-02", 14) == 1.0


def test_nyc_outranks_elsewhere_but_nothing_is_excluded(profile, criteria):
    nyc = _score(_row(location="New York, NY"), profile, criteria)
    other = _score(_row(location="Austin, TX"), profile, criteria)
    abroad = _score(_row(location="Bengaluru, India"), profile, criteria)
    assert nyc.total > other.total > abroad.total
    assert abroad is not None  # location ranks, it never disqualifies


def test_tier_uses_three_bands_not_seven_steps(profile, criteria):
    """Tier numbers encode a strategy, not a ranking — tier 4 is not twice tier 2."""
    assert rank.tier_band(1) == rank.tier_band(2) == "anchor"
    assert rank.tier_band(3) == rank.tier_band(5) == "applied"
    assert rank.tier_band(6) == rank.tier_band(7) == "research"
    assert rank.tier_band(None) == "none"


def test_the_breakdown_names_every_term(profile, criteria):
    """'Why is this #1' has to be answerable (DESIGN.md §3.5)."""
    b = _score(_row(), profile, criteria)
    assert set(b.terms) == {"fit", "growth", "recency", "tier", "location", "entry_risk"}
    assert b.total == pytest.approx(sum(b.terms.values()))
    assert "fit" in b.explain()


# -- reweighting is free -----------------------------------------------------------
def test_changing_a_weight_reorders_without_touching_the_judgments(criteria, tmp_path):
    """The payoff of keeping arithmetic out of the model.

    Two postings: one a strong fit at a research-tier company, one a weak fit at an
    anchor. Which wins is a matter of weights alone, and flipping it must not require
    re-asking the model anything.
    """
    base = """
target_roles: |
  Backend.
career_goals: |
  Growth.
profile: |
  2027.
weights: {{fit: {fit}, growth: 10, recency: 10, tier: {tier}, location: 5, entry_risk: -10}}
recency_half_life_days: 14
"""
    strong_fit_low_tier = _row(backend_fit="strong", growth="weak")
    weak_fit_high_tier = _row(backend_fit="weak", growth="weak")

    p = tmp_path / "profile.yaml"
    p.write_text(base.format(fit=100, tier=1))
    fit_first = load_profile(p)
    assert (_score(strong_fit_low_tier, fit_first, criteria, tier=7).total
            > _score(weak_fit_high_tier, fit_first, criteria, tier=1).total)

    p.write_text(base.format(fit=1, tier=100))
    tier_first = load_profile(p)
    assert (_score(strong_fit_low_tier, tier_first, criteria, tier=7).total
            < _score(weak_fit_high_tier, tier_first, criteria, tier=1).total)

    # And the judgments themselves were never consulted — same prose, same hash.
    assert fit_first.prose_hash == tier_first.prose_hash


# -- the queue ---------------------------------------------------------------------
def test_applied_skipped_and_snoozed_leave_the_queue():
    today = "2026-08-02"
    assert rank.is_available(_row(), today)
    assert not rank.is_available(_row(applied_status="applied"), today)
    assert not rank.is_available(_row(deferral_kind="skipped"), today)
    assert not rank.is_available(
        _row(deferral_kind="snoozed", deferral_until="2026-08-09"), today)


def test_an_expired_snooze_returns_the_posting_on_its_own():
    """That is the difference between snoozing and skipping."""
    assert rank.is_available(
        _row(deferral_kind="snoozed", deferral_until="2026-08-01"), "2026-08-02")


def test_top_n_skips_unranked_and_unavailable():
    rows = [
        _row(ats_job_id="1", score=90.0),
        _row(ats_job_id="2", score=80.0, applied_status="applied"),
        _row(ats_job_id="3", score=None),               # unjudged: never surfaced
        _row(ats_job_id="4", score=70.0),
        _row(ats_job_id="5", score=60.0),
    ]
    picked = [r["ats_job_id"] for r in rank.top_n(rows, 3, "2026-08-02")]
    assert picked == ["1", "4", "5"]


# -- storage round-trip ------------------------------------------------------------
def test_judgment_round_trips_and_survives_a_rescore():
    conn = store.connect(":memory:")
    store.sync_postings(conn, "Acme", [Posting("Acme", "1", "SWE", "u")], "2026-08-02")
    store.record_verdict(
        conn, Verdict("Acme", "1", Decision.MATCH, "r", "rules"), "2026-08-02")

    j = RankJudgment("strong", "moderate", "low", "owns the ingestion pipeline")
    store.record_judgment(conn, "Acme", "1", j, "hash1", "2026-08-02")
    store.set_score(conn, "Acme", "1", 77.5, "2026-08-02")

    row = store.ranked_matches(conn)[0]
    assert row["backend_fit"] == "strong"
    assert row["why"] == "owns the ingestion pipeline"
    assert row["score"] == 77.5


def test_a_changed_prose_hash_puts_a_posting_back_in_the_judging_queue():
    conn = store.connect(":memory:")
    store.sync_postings(conn, "Acme", [Posting("Acme", "1", "SWE", "u")], "2026-08-02")
    store.set_description(conn, "Acme", "1", "a description")
    store.record_verdict(conn, Verdict("Acme", "1", Decision.MATCH, "r", "rules"), "2026-08-02")

    assert len(store.matches_needing_judgment(conn, "hash1")) == 1
    store.record_judgment(
        conn, "Acme", "1", RankJudgment("strong", "strong", "low", ""), "hash1", "2026-08-02")
    assert store.matches_needing_judgment(conn, "hash1") == []
    # You rewrote your career goals: the old answer was to a different question.
    assert len(store.matches_needing_judgment(conn, "hash2")) == 1


def test_a_posting_with_no_description_is_never_queued_for_judgment():
    """Nothing to read means nothing to ask about — do not burn a call to find out."""
    conn = store.connect(":memory:")
    store.sync_postings(conn, "Acme", [Posting("Acme", "1", "SWE", "u")], "2026-08-02")
    store.record_verdict(conn, Verdict("Acme", "1", Decision.MATCH, "r", "rules"), "2026-08-02")
    assert store.matches_needing_judgment(conn, "hash1") == []


def test_snooze_without_a_date_is_rejected():
    conn = store.connect(":memory:")
    with pytest.raises(ValueError, match="needs an 'until' date"):
        store.set_deferral(conn, "Acme", "1", "snoozed", "2026-08-02")


def test_unreadable_postings_are_counted_not_defaulted():
    """A fabricated 'moderate' is worse than a visible gap."""
    class _Silent:
        def judge_posting(self, *_a):
            return None

    rows = [{"company": "Acme", "ats_job_id": "1", "title": "SWE",
             "description": "d"}]
    out, stats = rank.judge_matches(rows, _Silent(), load_profile(config.PROFILE_YAML))
    assert out == []
    assert stats.considered == 1 and stats.unreadable == 1 and stats.judged == 0


# -- the CLI disposition loop ------------------------------------------------------
def _seed(db, jid="1", score=90.0):
    conn = store.connect(db)
    store.sync_postings(conn, "Acme", [Posting("Acme", jid, "SWE", f"https://x/{jid}")],
                        "2026-08-02")
    store.set_description(conn, "Acme", jid, "a description")
    store.record_verdict(conn, Verdict("Acme", jid, Decision.MATCH, "r", "rules"),
                         "2026-08-02")
    store.record_judgment(conn, "Acme", jid, RankJudgment("strong", "strong", "low", "w"),
                          "h", "2026-08-02")
    store.set_score(conn, "Acme", jid, score, "2026-08-02")
    conn.commit()
    conn.close()


def test_today_applied_leaves_the_queue_and_lands_in_applications(tmp_path):
    """One action, two records: the queue forgets it, the outer loop remembers it."""
    from jobtracker.cli import main

    db = str(tmp_path / "s.db")
    _seed(db)
    assert main(["today", "--db", db, "--applied", "Acme", "1"]) == 0

    conn = store.connect(db)
    assert len(store.all_applications(conn)) == 1
    assert rank.top_n(store.ranked_matches(conn), 3, "2026-08-02") == []
    conn.close()


def test_today_snooze_returns_the_posting_after_the_window(tmp_path):
    from jobtracker.cli import main

    db = str(tmp_path / "s.db")
    _seed(db)
    assert main(["today", "--db", db, "--snooze", "Acme", "1", "--days", "7"]) == 0

    conn = store.connect(db)
    rows = store.ranked_matches(conn)
    assert rank.top_n(rows, 3, "2026-08-02") == []          # gone today
    assert len(rank.top_n(rows, 3, "2026-08-20")) == 1      # back later
    conn.close()


def test_today_on_an_unknown_posting_is_an_error_not_a_silent_no_op(tmp_path):
    from jobtracker.cli import main

    db = str(tmp_path / "s.db")
    _seed(db)
    assert main(["today", "--db", db, "--skip", "Nope", "999"]) == 1


def test_rank_with_no_model_still_scores_what_it_has(tmp_path):
    """A weights-only edit must re-sort the queue with no model involved at all."""
    from jobtracker.cli import main

    db = str(tmp_path / "s.db")
    _seed(db, score=0.0)  # stale score; judgment already stored
    assert main(["rank", "--db", db]) == 0

    conn = store.connect(db)
    assert store.ranked_matches(conn)[0]["score"] > 0  # rescored without a model
    conn.close()
