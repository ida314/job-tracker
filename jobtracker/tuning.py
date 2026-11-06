"""Making the matcher improvable: overrides, regression evaluation, rule suggestions.

`criteria.yaml` is easy to edit and impossible to edit *safely* — a token added to fix
one bad match silently changes the verdict on thousands of postings you already judged.
This module is the missing half: it records what you decided and replays proposed rules
against that record, so a change can be measured before it ships.

Everything here is pure. `match()` stays a function of (posting, criteria) with no
database access, because that purity is what makes it testable; overrides are applied
*around* it by the caller, not inside it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence

from .criteria import Criteria, _LIST_KEYS
from .match import match
from .models import Decision, Posting, Verdict


# -- overrides ---------------------------------------------------------------------
def apply_override(verdict: Verdict, overrides: dict) -> Verdict:
    """Let a recorded judgment win over the rules for one specific posting.

    Applied in the caller path (`check`, `rematch`) rather than inside `match()`, so
    the matcher stays pure and the override is visible in the verdict's reason.

    Two kinds of judgment pin here, and the distinction has to survive: a human ruling,
    and a verdict the LLM pass reached by reading a description. Both need protection
    from `check`, which re-derives a rules verdict from the title every night and would
    otherwise overwrite them. Neither may masquerade as the other, so `decided_by` is
    read off the row rather than assumed — DESIGN.md §3.5, every verdict carries its
    provenance.
    """
    ov = overrides.get((verdict.company, verdict.ats_job_id))
    if ov is None:
        return verdict
    reason = ov["reason"] or "manual"
    # sqlite3.Row and dict both answer keys(); a row predating the decided_by column,
    # or a hand-built dict in a test, is a human override by definition.
    decided_by = ov["decided_by"] if "decided_by" in ov.keys() else "human"
    return Verdict(
        verdict.company,
        verdict.ats_job_id,
        Decision(ov["decision"]),
        f"override:{reason}",
        decided_by,
    )


# -- evaluation --------------------------------------------------------------------
class Outcome(str, Enum):
    """How the current rules compare to a judgment you already made.

    REGRESSION and UNRESOLVED are deliberately different. If the rules say `uncertain`
    where you said `match`, the rules have not made a mistake — `uncertain` is the
    honest answer for a title with no level token, and resolving it is the LLM pass's
    job. Counting that as a failure would push you to add rules that guess from titles,
    which is precisely the over-fitting this file exists to prevent.

    A REGRESSION is the rules *actively contradicting* you: reject where you said
    match, or match where you said reject. That is the only signal worth acting on.
    """

    AGREE = "agree"
    REGRESSION = "regression"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Conflict:
    company: str
    ats_job_id: str
    title: str
    yours: str
    rules: str
    rule_reason: str
    outcome: Outcome


@dataclass
class EvalReport:
    total: int = 0
    agree: int = 0
    regressions: list[Conflict] = field(default_factory=list)
    unresolved: list[Conflict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing in the corpus contradicts the current rules."""
        return not self.regressions

    def summary(self) -> str:
        return (
            f"{self.total} decisions · agree {self.agree} · "
            f"regressions {len(self.regressions)} · unresolved {len(self.unresolved)}"
        )


def evaluate(decisions: Iterable, criteria: Criteria) -> EvalReport:
    """Replay `criteria` over every recorded judgment.

    Rows need `.title`, `.decision`, `.company`, `.ats_job_id` — sqlite3.Row or any
    mapping. No I/O: hand it rows and criteria, get a verdict on the criteria.
    """
    report = EvalReport()
    for row in decisions:
        report.total += 1
        posting = Posting(
            company=row["company"],
            ats_job_id=row["ats_job_id"],
            title=row["title"],
            url="",
            location=(row["location"] or "") if "location" in row.keys() else "",
        )
        verdict = match(posting, criteria)
        rules = verdict.decision.value
        yours = row["decision"]

        if rules == yours:
            report.agree += 1
            continue

        conflict_kind = (
            Outcome.UNRESOLVED if rules == Decision.UNCERTAIN.value else Outcome.REGRESSION
        )
        conflict = Conflict(
            company=row["company"],
            ats_job_id=row["ats_job_id"],
            title=row["title"],
            yours=yours,
            rules=rules,
            rule_reason=verdict.reason,
            outcome=conflict_kind,
        )
        if conflict_kind is Outcome.REGRESSION:
            report.regressions.append(conflict)
        else:
            report.unresolved.append(conflict)
    return report


# -- rule suggestions --------------------------------------------------------------
# Single words that carry no signal on their own. Anything domain-specific is caught
# automatically by the never-in-an-accepted-title test, so this list stays tiny.
_STOPWORDS = frozenset(
    "a an and at by de del di en for from in of on or the to und with i ii iii".split()
)

_WORD = re.compile(r"[a-z0-9+#]+")

# Bigrams must not cross these. Job titles are full of appended qualifiers —
# "Software Engineer, New Grad - Commercial" — and a naive word-pair scan invents
# phrases like "grad commercial" that appear nowhere in the text, then confidently
# suggests them as rules. Segmenting on punctuation first is what keeps suggestions
# to phrases a human would actually recognize.
_BOUNDARY = re.compile(r"[^a-z0-9+#]*[,\-–—()\[\]/|:;]+[^a-z0-9+#]*")


def _phrases(title: str) -> set[str]:
    """Unigrams and within-segment bigrams from a title.

    Bigrams matter more than unigrams here: the rule worth writing is
    "forward deployed", not "forward" (which catches nothing useful on its own) or
    "deployed" (which would also hit legitimate deployment-engineering roles).

    Bigrams are built from the raw word sequence and then filtered, rather than from
    the stopword-stripped one — dropping "of" from "Engineer of Operations" first
    would fuse "engineer operations" into a phrase the title never contained.
    """
    out: set[str] = set()
    for segment in _BOUNDARY.split(title.lower()):
        words = _WORD.findall(segment)
        out |= {w for w in words if len(w) > 2 and w not in _STOPWORDS}
        out |= {
            f"{a} {b}"
            for a, b in zip(words, words[1:])
            if a not in _STOPWORDS and b not in _STOPWORDS
        }
    return out


@dataclass(frozen=True)
class Suggestion:
    phrase: str
    rejected: int
    examples: tuple[str, ...]
    target_list: str = "exclude_titles"


def suggest_rules(
    decisions: Sequence,
    criteria: Optional[Criteria] = None,
    min_count: int = 3,
    limit: int = 10,
) -> list[Suggestion]:
    """Phrases that appear in several *still-unfixed* rejected titles and in no accept.

    Pure string counting — no model. The zero-in-accepted test does most of the work:
    'engineer' and 'software' appear in titles you accepted, so they can never be
    suggested, and no hand-maintained blocklist is needed to protect them.

    When `criteria` is given, rejects the rules *already* handle are excluded from the
    count. That is what makes the list self-terminating: it describes the remaining
    gap rather than the whole history, so it empties out as you fix things instead of
    re-proposing variations on rules you have already written. Without it, accepting
    "forward deployed" would just promote "deployed software" to the top of the list.
    """
    rejected: Counter[str] = Counter()
    accepted: set[str] = set()
    examples: dict[str, list[str]] = {}

    for row in decisions:
        title = row["title"]
        phrases = _phrases(title)
        if row["decision"] == "reject":
            if criteria is not None:
                # Already handled? Then it is not evidence of a missing rule.
                verdict = match(
                    Posting(row["company"], row["ats_job_id"], title, ""), criteria
                )
                if verdict.decision is Decision.REJECT:
                    continue
            rejected.update(phrases)
            for p in phrases:
                examples.setdefault(p, [])
                if len(examples[p]) < 3 and title not in examples[p]:
                    examples[p].append(title)
        else:
            accepted |= phrases

    known: set[str] = set()
    if criteria is not None:
        for key in _LIST_KEYS:
            known |= {t.lower() for t in getattr(criteria, key, [])}

    out = [
        Suggestion(phrase=p, rejected=n, examples=tuple(examples.get(p, ())))
        for p, n in rejected.most_common()
        if n >= min_count and p not in accepted and p not in known
    ]

    # Prefer the more specific phrase: if "forward deployed" is suggested — or is
    # already a rule — drop the bare "forward" that co-occurs with it and adds nothing.
    multiword = {s.phrase for s in out if " " in s.phrase}
    multiword |= {k for k in known if " " in k}
    covered = {w for p in multiword for w in p.split()}
    out = [s for s in out if " " in s.phrase or s.phrase not in covered]

    return out[:limit]


# -- replay over the whole posting corpus ------------------------------------------
# The transitions that block. A posting the old rules matched and the new rules no
# longer match is the one change `eval` exists to catch; with no `decisions` to replay,
# "the rules used to say match" is the nearest thing to a judgment there is. Moving an
# `uncertain` row is not on this list — `uncertain` is not a ruling (see `Outcome`).
GATED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (Decision.MATCH.value, Decision.REJECT.value),
    (Decision.MATCH.value, Decision.UNCERTAIN.value),
})

_DECISION_ORDER = (Decision.MATCH.value, Decision.UNCERTAIN.value, Decision.REJECT.value)

# Every off-diagonal pair, in a fixed order, so a report prints the same six lines
# whether or not anything moved — "all zeros" is a statement worth being able to read.
TRANSITIONS: tuple[tuple[str, str], ...] = tuple(
    (a, b) for a in _DECISION_ORDER for b in _DECISION_ORDER if a != b
)


@dataclass(frozen=True)
class ReplaySample:
    company: str
    ats_job_id: str
    title: str
    old_reason: str
    new_reason: str


@dataclass
class ReplayReport:
    """What a criteria change would do to every stored posting.

    `transitions` counts *effective* verdicts — after overrides — because that is what
    the pages would show. A pinned row cannot move there by construction, so a pinned
    row whose rules verdict did move is counted under `held` instead, keyed by the rules
    transition it is being held against. That is the row a criteria edit may need
    `store.clear_override` for, and only the caller can decide whether it does.
    """

    total: int = 0
    pinned: int = 0
    before: Counter = field(default_factory=Counter)
    after: Counter = field(default_factory=Counter)
    transitions: Counter = field(default_factory=Counter)
    held: Counter = field(default_factory=Counter)
    samples: dict = field(default_factory=dict)
    held_samples: dict = field(default_factory=dict)

    def gated(self, intended: Iterable[tuple[str, str]] = ()) -> dict[tuple[str, str], int]:
        """The blocking transitions that occurred and were not declared intended."""
        allowed = set(intended)
        return {
            t: n for t, n in self.transitions.items()
            if n and t in GATED_TRANSITIONS and t not in allowed
        }


def _replay_posting(row) -> Posting:
    keys = row.keys()
    return Posting(
        company=row["company"],
        ats_job_id=row["ats_job_id"],
        title=row["title"],
        url=(row["url"] or "") if "url" in keys else "",
        location=(row["location"] or "") if "location" in keys else "",
    )


def replay(
    postings: Iterable,
    old: Criteria,
    new: Criteria,
    overrides: dict,
    samples: int = 20,
) -> ReplayReport:
    """Diff every posting's verdict under `old` and `new` criteria.

    The empty-corpus form of `evaluate`: where that replays rules against what you
    decided, this replays them against what the *previous* rules decided, over every
    posting stored — open and closed, every origin. Pure, like the rest of this module:
    rows in (anything with `.keys()` and item access), a report out. The caller owns the
    connection, which is how it stays read-only.

    `samples` bounds the examples kept per transition, not the counting.
    """
    report = ReplayReport()
    for row in postings:
        posting = _replay_posting(row)
        old_rules = match(posting, old)
        new_rules = match(posting, new)
        old_eff = apply_override(old_rules, overrides)
        new_eff = apply_override(new_rules, overrides)
        a, b = old_eff.decision.value, new_eff.decision.value

        report.total += 1
        report.before[a] += 1
        report.after[b] += 1
        pinned = (posting.company, posting.ats_job_id) in overrides
        report.pinned += pinned

        moved = (old_rules.decision.value, new_rules.decision.value)
        sample = ReplaySample(
            posting.company, posting.ats_job_id, posting.title,
            old_rules.reason, new_rules.reason,
        )
        if a != b:
            report.transitions[(a, b)] += 1
            kept = report.samples.setdefault((a, b), [])
            if len(kept) < samples:
                kept.append(sample)
        elif pinned and moved[0] != moved[1]:
            report.held[moved] += 1
            kept = report.held_samples.setdefault(moved, [])
            if len(kept) < samples:
                kept.append(sample)
    return report


def parse_transition(text: str) -> tuple[str, str]:
    """`"uncertain->match"` (or `→`) as a pair of decision values. ValueError otherwise."""
    for arrow in ("->", "→"):
        if arrow in text:
            a, _, b = (part.strip().lower() for part in text.partition(arrow))
            break
    else:
        raise ValueError(f"not a transition (want 'old->new'): {text!r}")
    valid = {d.value for d in Decision}
    if a not in valid or b not in valid or a == b:
        raise ValueError(f"not a transition between two verdicts: {text!r}")
    return a, b
