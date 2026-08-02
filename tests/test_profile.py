"""profile.yaml: the one input that most determines the ranking.

The load-bearing test here is `test_changing_a_weight_does_not_invalidate_judgments`.
prose_hash deliberately covers only the prose, which is what makes retuning free: a
weight change re-sorts the whole queue with zero model calls, while a prose change
re-asks the question and forces re-judging. Hash the whole file instead and every
tweak to a number costs a full GPU pass over the corpus.
"""

import pytest

from jobtracker import config
from jobtracker.profile import Profile, load_profile

_MINIMAL = """
target_roles: |
  Backend.
career_goals: |
  Growth first.
profile: |
  2027 grad.
weights:
  fit: 30
  growth: 25
  recency: 20
  tier: 15
  location: 10
  entry_risk: -15
recency_half_life_days: 14
"""


def _write(tmp_path, text):
    p = tmp_path / "profile.yaml"
    p.write_text(text)
    return p


def test_loads_the_repo_profile():
    """The shipped file must parse — every rank run depends on it."""
    prof = load_profile(config.PROFILE_YAML)
    assert prof.target_roles and prof.career_goals and prof.profile
    assert prof.weights["fit"] > 0
    assert prof.weights["entry_risk"] < 0  # a penalty, not a bonus
    assert prof.recency_half_life_days > 0


def test_prose_carries_all_three_blocks_in_a_fixed_order(tmp_path):
    prof = load_profile(_write(tmp_path, _MINIMAL))
    assert "Backend." in prof.prose
    assert "Growth first." in prof.prose
    assert "2027 grad." in prof.prose
    assert prof.prose.index("Backend.") < prof.prose.index("Growth first.")


def test_changing_a_weight_does_not_invalidate_judgments(tmp_path):
    """The mechanism that makes retuning free."""
    before = load_profile(_write(tmp_path, _MINIMAL))
    after = load_profile(_write(tmp_path, _MINIMAL.replace("fit: 30", "fit: 5")))
    assert after.weights["fit"] != before.weights["fit"]
    assert after.prose_hash == before.prose_hash  # judgments stand; only the sort moves


def test_changing_the_prose_does_invalidate_judgments(tmp_path):
    """A judgment is an answer to a question. Change the question, re-ask it."""
    before = load_profile(_write(tmp_path, _MINIMAL))
    after = load_profile(_write(tmp_path, _MINIMAL.replace("Backend.", "Frontend.")))
    assert after.prose_hash != before.prose_hash


def test_missing_file_is_loud():
    with pytest.raises(FileNotFoundError):
        load_profile("/nonexistent/profile.yaml")


def test_unknown_key_is_an_error(tmp_path):
    """A typo'd key must not be a silently-ignored preference (DESIGN.md §2.1)."""
    with pytest.raises(ValueError, match="unknown profile keys"):
        load_profile(_write(tmp_path, _MINIMAL + "\ncareer_gaols: |\n  typo\n"))


def test_a_missing_weight_is_an_error_not_a_default(tmp_path):
    """A dropped weight would silently remove a whole axis from the score."""
    text = _MINIMAL.replace("  recency: 20\n", "")
    with pytest.raises(ValueError, match="missing weights"):
        load_profile(_write(tmp_path, text))


def test_unknown_weight_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="unknown weights"):
        load_profile(_write(tmp_path, _MINIMAL.replace("  fit: 30", "  fitt: 30\n  fit: 30")))


@pytest.mark.parametrize("bad", ["  fit: yes\n", "  fit: high\n"])
def test_non_numeric_weight_is_an_error(tmp_path, bad):
    with pytest.raises(ValueError, match="must be a number"):
        load_profile(_write(tmp_path, _MINIMAL.replace("  fit: 30\n", bad)))


def test_half_life_must_be_positive(tmp_path):
    """A zero or negative half-life would make the recency term explode or invert."""
    with pytest.raises(ValueError, match="must be positive"):
        load_profile(_write(tmp_path, _MINIMAL.replace("days: 14", "days: 0")))


def test_prose_hash_is_stable_across_loads(tmp_path):
    path = _write(tmp_path, _MINIMAL)
    assert load_profile(path).prose_hash == load_profile(path).prose_hash
    assert len(Profile().prose_hash) == 16
