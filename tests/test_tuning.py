"""Overrides, regression evaluation, and rule suggestion.

The load-bearing case is `test_uncertain_is_not_a_regression`: if rules saying
"uncertain" counted against you, `eval` would push you to add title-guessing rules,
which is the over-fitting the whole mechanism exists to prevent.
"""

import pytest

from jobtracker import config, store, tuning
from jobtracker.criteria import Criteria, load_criteria
from jobtracker.models import Decision, Verdict
from jobtracker.tuning import Outcome


def _dec(title, decision, company="Acme", jid="1", location=""):
    return {
        "company": company,
        "ats_job_id": jid,
        "title": title,
        "location": location,
        "decision": decision,
    }


@pytest.fixture
def criteria():
    return load_criteria(config.CRITERIA_YAML)


# -- overrides ---------------------------------------------------------------------
def test_override_replaces_verdict_and_records_authorship():
    v = Verdict("Acme", "1", Decision.MATCH, "level:new grad+eng:generic")
    overrides = {("Acme", "1"): {"decision": "reject", "reason": "not backend"}}
    out = tuning.apply_override(v, overrides)
    assert out.decision is Decision.REJECT
    assert out.reason == "override:not backend"
    assert out.decided_by == "human"  # distinguishable from 'rules' and 'llm' forever


def test_override_leaves_other_postings_untouched():
    v = Verdict("Acme", "2", Decision.MATCH, "level:new grad+eng:generic")
    overrides = {("Acme", "1"): {"decision": "reject", "reason": "x"}}
    assert tuning.apply_override(v, overrides) is v


def test_overrides_survive_rematch(tmp_path):
    """The whole point of an override: a rule change must not silently re-open it."""
    conn = store.connect(":memory:")
    crit = load_criteria(config.CRITERIA_YAML)
    store.set_override(conn, "Acme", "1", "reject", "2026-07-23", reason="FDE role")
    overrides = store.load_overrides(conn)

    from jobtracker.match import match
    from jobtracker.models import Posting

    p = Posting("Acme", "1", "Software Engineer, New Grad", "u")
    assert match(p, crit).decision is Decision.MATCH        # rules would match it
    assert tuning.apply_override(match(p, crit), overrides).decision is Decision.REJECT

    store.clear_override(conn, "Acme", "1")
    assert store.load_overrides(conn) == {}


# -- evaluation --------------------------------------------------------------------
def test_agreement(criteria):
    rows = [_dec("Software Engineer, New Grad", "match"),
            _dec("Senior Staff Engineer", "reject", jid="2")]
    r = tuning.evaluate(rows, criteria)
    assert r.total == 2 and r.agree == 2
    assert r.ok and not r.regressions


def test_regression_is_detected(criteria):
    """Rules actively contradict a judgment — the only signal worth acting on."""
    rows = [_dec("Senior Staff Engineer", "match")]   # rules reject 'senior'
    r = tuning.evaluate(rows, criteria)
    assert not r.ok
    assert len(r.regressions) == 1
    c = r.regressions[0]
    assert c.outcome is Outcome.REGRESSION
    assert c.yours == "match" and c.rules == "reject"
    assert "senior" in c.rule_reason           # names the rule that fired


def test_uncertain_is_not_a_regression(criteria):
    """`uncertain` on a title with no level token is correct, not a failure.

    'Platform Engineer' has no level signal. The honest verdict is uncertain, and
    resolving it is the description-reading pass's job — not a title rule's.
    """
    rows = [_dec("Platform Engineer", "match")]
    r = tuning.evaluate(rows, criteria)
    assert r.regressions == []
    assert r.ok                                 # does not block a rule change
    assert len(r.unresolved) == 1
    assert r.unresolved[0].outcome is Outcome.UNRESOLVED


def test_eval_report_summary_mentions_all_buckets(criteria):
    r = tuning.evaluate([_dec("Platform Engineer", "match")], criteria)
    for word in ("decisions", "agree", "regressions", "unresolved"):
        assert word in r.summary()


# -- suggestions -------------------------------------------------------------------
def test_suggests_phrase_common_to_rejects():
    rows = [
        _dec("Forward Deployed Software Engineer, New Grad", "reject", jid="1"),
        _dec("Forward Deployed Software Engineer - Defense", "reject", jid="2"),
        _dec("Forward Deployed Engineer, Commercial", "reject", jid="3"),
        _dec("Software Engineer, New Grad - Infrastructure", "match", jid="4"),
    ]
    phrases = [s.phrase for s in tuning.suggest_rules(rows, min_count=3)]
    assert "forward deployed" in phrases


def test_never_suggests_a_phrase_seen_in_an_accepted_title():
    """This is what keeps 'engineer' and 'software' out without a hand-kept blocklist."""
    rows = [
        _dec("Software Engineer, Operations", "reject", jid="1"),
        _dec("Software Engineer, Support", "reject", jid="2"),
        _dec("Software Engineer, Billing", "reject", jid="3"),
        _dec("Software Engineer, New Grad", "match", jid="4"),
    ]
    phrases = [s.phrase for s in tuning.suggest_rules(rows, min_count=3)]
    assert "software" not in phrases
    assert "engineer" not in phrases
    assert "software engineer" not in phrases


def test_prefers_the_specific_phrase_over_its_words():
    rows = [_dec(f"Forward Deployed Engineer {i}", "reject", jid=str(i)) for i in range(4)]
    phrases = [s.phrase for s in tuning.suggest_rules(rows, min_count=3)]
    assert "forward deployed" in phrases
    assert "forward" not in phrases          # subsumed by the bigram

def test_does_not_resuggest_an_existing_rule():
    rows = [_dec(f"Senior Engineer {i}", "reject", jid=str(i)) for i in range(4)]
    crit = Criteria(exclude_titles=["senior"])
    assert "senior" not in [s.phrase for s in tuning.suggest_rules(rows, crit, min_count=3)]


def test_suggestions_are_self_terminating():
    """Once a rule covers the rejects, it stops proposing variations on the same fix.

    Without this, accepting 'forward deployed' just promotes the overlapping bigram
    'deployed software' to the top of the list, and the list never empties.
    """
    rows = [_dec(f"Forward Deployed Software Engineer {i}", "reject", jid=str(i))
            for i in range(4)]
    assert tuning.suggest_rules(rows, min_count=3)                    # gap exists
    fixed = Criteria(exclude_titles=["forward deployed"])
    assert tuning.suggest_rules(rows, fixed, min_count=3) == []       # gap closed


def test_suggestion_carries_examples():
    rows = [_dec(f"Forward Deployed Engineer {i}", "reject", jid=str(i)) for i in range(4)]
    s = next(s for s in tuning.suggest_rules(rows, min_count=3) if s.phrase == "forward deployed")
    assert s.rejected == 4
    assert s.examples and "Forward Deployed" in s.examples[0]


# -- storage round-trip ------------------------------------------------------------
def test_decisions_survive_the_posting_closing():
    """Titles are denormalized so the corpus does not shrink when reqs close."""
    conn = store.connect(":memory:")
    store.record_decision(conn, "Acme", "1", "Ops Associate", "reject", "2026-07-23")
    conn.execute("DELETE FROM postings")       # req closed and got pruned
    conn.commit()
    rows = store.all_decisions(conn)
    assert len(rows) == 1 and rows[0]["title"] == "Ops Associate"


def test_record_decision_rejects_a_bad_verdict():
    conn = store.connect(":memory:")
    with pytest.raises(ValueError):
        store.record_decision(conn, "Acme", "1", "T", "maybe", "2026-07-23")
