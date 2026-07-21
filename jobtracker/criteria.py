"""Load and validate criteria.yaml.

The whole reason this module exists: a config format nothing validates is not
configuration, it is a comment (DESIGN.md §2.1). Loading fails loudly on anything
malformed so the pipeline never runs against a criteria file it silently misread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

# Every list-valued key we expect. Missing ones default to empty; unknown keys are an
# error, because a typo'd key would otherwise be a silently-ignored rule.
_LIST_KEYS = {
    "level_include",
    "exclude_titles",
    "engineering_terms",
    "role_type_include",
    "role_type_exclude",
    "locations_preferred",
    "locations_acceptable",
    "locations_exclude",
    "keywords_bonus",
}


def _compile(token: str) -> re.Pattern[str]:
    """Match `token` on alphanumeric boundaries, case-insensitively.

    Lookarounds treat [a-z0-9] as the "word" class so that 'ii' does not fire inside
    'iii', 'sr.' matches 'sr. engineer' but not 'senior', and 'Software Engineer I'
    does not match 'Software Engineer II'.
    """
    esc = re.escape(token.lower())
    return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])", re.IGNORECASE)


@dataclass
class Criteria:
    level_include: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)
    engineering_terms: list[str] = field(default_factory=list)
    role_type_include: list[str] = field(default_factory=list)
    role_type_exclude: list[str] = field(default_factory=list)
    locations_preferred: list[str] = field(default_factory=list)
    locations_acceptable: list[str] = field(default_factory=list)
    locations_exclude: list[str] = field(default_factory=list)
    keywords_bonus: list[str] = field(default_factory=list)

    # Compiled patterns, keyed by list name -> [(token, pattern), ...].
    _patterns: dict[str, list[tuple[str, re.Pattern[str]]]] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        for key in _LIST_KEYS:
            tokens = getattr(self, key)
            self._patterns[key] = [(t, _compile(t)) for t in tokens]

    def first_hit(self, key: str, text: str) -> str | None:
        """Return the first token from list `key` that matches `text`, else None."""
        for token, pattern in self._patterns[key]:
            if pattern.search(text):
                return token
        return None

    def hits(self, key: str, text: str) -> list[str]:
        return [tok for tok, pat in self._patterns[key] if pat.search(text)]


def load_criteria(path: str | Path) -> Criteria:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"criteria file not found: {path}")

    with path.open() as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a top-level mapping, got {type(data).__name__}")

    unknown = set(data) - _LIST_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown criteria keys: {sorted(unknown)}")

    kwargs: dict[str, list[str]] = {}
    for key in _LIST_KEYS:
        value = data.get(key, [])
        if value is None:
            value = []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in _as_iter(value)):
            raise ValueError(f"{path}: '{key}' must be a list of strings")
        kwargs[key] = [v.strip() for v in value if v and v.strip()]

    return Criteria(**kwargs)


def _as_iter(value: object) -> Iterable:
    return value if isinstance(value, list) else []
