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
    """Let a recorded human judgment win over the rules for one specific posting.

    Applied in the caller path (`check`, `rematch`) rather than inside `match()`, so
    the matcher stays pure and the override is visible in the verdict's reason.
    """
    ov = overrides.get((verdict.company, verdict.ats_job_id))
    if ov is None:
        return verdict
    reason = ov["reason"] or "manual"
    return Verdict(
        verdict.company,
        verdict.ats_job_id,
        Decision(ov["decision"]),
        f"override:{reason}",
        "human",
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
