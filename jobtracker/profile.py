"""Load and validate profile.yaml — who you are and what you are optimizing for.

Curation, not observation (DESIGN.md §3.3). This sits beside companies.yaml and
criteria.yaml: human-authored, git-tracked, never written by a run. A change to it is
a visible commit that explains why yesterday's ranking and today's differ.

It is deliberately NOT part of criteria.yaml. That file is a set of matching rules
guarded by `jobtracker eval`, which replays it against recorded judgments; profile
edits have nothing to do with title matching and would make that gate noisy.

The file has two halves and the split is the whole point:

  * **Prose** goes into the model's context verbatim. Career goals do not reduce to
    tokens, which is the entire reason a model is involved at all.
  * **Weights** never reach the model. They are arithmetic, applied in Python where
    they can be diffed, tested, and replayed.

`prose_hash` covers only the prose. That is what makes retuning free: change a weight
and cached judgments stand, so re-sorting the whole queue costs zero model calls.
Change what you told the model about yourself and the hash moves, so the judgments are
re-taken — because they were answers to a question you have now changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Free-text blocks handed to the model.
_PROSE_KEYS = ("target_roles", "career_goals", "profile")

# Scoring weights, applied in Python. Every one must be present in the file — a missing
# weight silently dropping a whole axis out of the score is exactly the kind of quiet
# misconfiguration this repo refuses to ship (DESIGN.md §2.1).
_WEIGHT_KEYS = ("fit", "growth", "recency", "tier", "location", "entry_risk")

_TOP_KEYS = set(_PROSE_KEYS) | {"weights", "recency_half_life_days"}


@dataclass(frozen=True)
class Profile:
    target_roles: str = ""
    career_goals: str = ""
    profile: str = ""
    weights: dict = field(default_factory=dict)
    recency_half_life_days: float = 14.0

    @property
    def prose(self) -> str:
        """The three blocks as one string, in a fixed order.

        Fixed order matters: this is both what the model reads and what gets hashed,
        so reordering the file must not silently invalidate every cached judgment.
        """
        return (
            f"TARGET ROLES:\n{self.target_roles.strip()}\n\n"
            f"CAREER GOALS, IN PRIORITY ORDER:\n{self.career_goals.strip()}\n\n"
            f"OTHER CONTEXT:\n{self.profile.strip()}"
        )

    @property
    def prose_hash(self) -> str:
        """Identity of the question the model was asked — weights excluded.

        Deliberately not a hash of the whole file. Weights change how answers are
        combined, not what was asked, so they must not invalidate the answers.
        """
        return hashlib.sha256(self.prose.encode()).hexdigest()[:16]


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"profile file not found: {path}")

    with path.open() as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a top-level mapping, got {type(data).__name__}")

    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown profile keys: {sorted(unknown)}")

    prose = {}
    for key in _PROSE_KEYS:
        value = data.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{path}: '{key}' must be a string block")
        prose[key] = value.strip()

    raw_weights = data.get("weights") or {}
    if not isinstance(raw_weights, dict):
        raise ValueError(f"{path}: 'weights' must be a mapping")
    unknown_w = set(raw_weights) - set(_WEIGHT_KEYS)
    if unknown_w:
        raise ValueError(f"{path}: unknown weights: {sorted(unknown_w)}")
    missing = set(_WEIGHT_KEYS) - set(raw_weights)
    if missing:
        raise ValueError(f"{path}: missing weights: {sorted(missing)}")

    weights = {}
    for key in _WEIGHT_KEYS:
        value = raw_weights[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: weight '{key}' must be a number")
        weights[key] = float(value)

    half_life = data.get("recency_half_life_days", 14)
    if isinstance(half_life, bool) or not isinstance(half_life, (int, float)):
        raise ValueError(f"{path}: 'recency_half_life_days' must be a number")
    if half_life <= 0:
        raise ValueError(f"{path}: 'recency_half_life_days' must be positive")

    return Profile(
        weights=weights, recency_half_life_days=float(half_life), **prose
    )
