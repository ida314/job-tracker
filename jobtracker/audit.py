"""Audit the model's pins: which `llm` overrides can still be justified.

A one-off, by-hand pass (`jobtracker audit-pins`), never scheduled. The `level` task pins
every verdict it reaches, because `check` would otherwise re-derive the title's verdict
and erase it overnight. Pins written before readings were grounded have nothing behind
them: an `entry` pin on a job board row whose "description" is assembled metadata read a
level off text that states none, and a `not_entry` reject is invisible because no page
renders a reject. This module decides which of those pins stay.

    entry,     source 'feed'          keep iff the rules alone reach the pin's verdict
    not_entry, any source             keep iff its quote occurs in the title or description
    entry,     'ats'/'prose', quoted  keep iff its quote occurs in the title or description
    entry,     'ats'/'prose', bare    keep: real prose was read, before quotes were stored

It is pure, like `match()`: rows in, findings out. The caller does the clearing and the
re-derivation, and selects only `decided_by = 'llm'` rows — a human ruling never reaches
this function, so nothing here can decide to remove one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .criteria import Criteria
from .match import match
from .models import Posting
from .tasks.level import grounded

KEEP = "keep"
CLEAR = "clear"

# `llm:entry+<detail>[:<evidence>]`, where detail is `role:<token>`, `eng:generic` or
# `non_engineering` (`tasks.level._entry_reason`). The detail's own colon is why this is
# a pattern and not a split: `llm:entry+eng:generic` carries no evidence at all.
_ENTRY = re.compile(r"^llm:entry\+(?:role:[^:]*|eng:[^:]*|[^:]*)(?::(?P<evidence>.*))?$", re.S)
_NOT_ENTRY = "llm:not_entry:"


@dataclass(frozen=True)
class PinFinding:
    company: str
    ats_job_id: str
    title: str
    decision: str          # the pinned verdict: 'match' | 'reject' | 'uncertain'
    kind: str              # 'entry' | 'not_entry' | 'unknown'
    action: str            # KEEP | CLEAR
    why: str               # one short phrase, printed beside the title


def parse_reason(reason: str) -> tuple[str, str]:
    """`(kind, evidence)` read off an `llm` override's reason.

    `evidence` is "" for a pin that stored none — every pre-grounding `entry` pin.
    """
    reason = reason or ""
    if reason.startswith(_NOT_ENTRY):
        return "not_entry", reason[len(_NOT_ENTRY):]
    m = _ENTRY.match(reason)
    if m:
        return "entry", m.group("evidence") or ""
    return "unknown", ""


def _quoted(evidence: str, title: str, description: Optional[str]) -> bool:
    """Does the stored quote occur, whitespace-normalized and casefolded, on the page?

    `level.grounded` is the check the reading passed when it was made, so the audit and
    the task cannot disagree about what "occurs" means. Title and description are
    checked separately: a quote spanning the two was never on either.
    """
    return grounded(evidence, title or "") or grounded(evidence, description or "")


def classify_pin(row, criteria: Criteria) -> PinFinding:
    """One `llm` override joined to its posting -> what to do with the pin."""
    title = row["title"] or ""
    decision = row["decision"]
    kind, evidence = parse_reason(row["reason"])
    source = row["description_source"]

    def finding(action: str, why: str) -> PinFinding:
        return PinFinding(row["company"], row["ats_job_id"], title, decision, kind,
                          action, why)

    if kind == "not_entry":
        if _quoted(evidence, title, row["description"]):
            return finding(KEEP, "quote found")
        return finding(CLEAR, "quote not found" if evidence else "no quote")

    if kind == "entry":
        if source == "feed":
            # Metadata states no level, so the reading had nothing to read. The pin is
            # harmless only where the rules, from the title alone, already agree with it.
            posting = Posting(row["company"], row["ats_job_id"], title, row["url"] or "",
                              location=row["location"] or "")
            rules = match(posting, criteria).decision.value
            if rules == decision:
                return finding(KEEP, "feed row; title agrees")
            return finding(CLEAR, f"feed row; title says {rules}")
        if evidence:
            if _quoted(evidence, title, row["description"]):
                return finding(KEEP, "quote found")
            return finding(CLEAR, "quote not found")
        if source in ("ats", "prose"):
            return finding(KEEP, "prose read before quotes were stored")
        # No quote and no record of what the text was: nothing says prose was read, and
        # nothing says it was not. Keeping is the side that removes nothing on a guess.
        return finding(KEEP, "source unknown")

    # A reason this module does not recognise was written by something it does not know
    # about. Clearing on a parse failure would be removing a pin for want of a regex.
    return finding(KEEP, "unrecognised reason")


def classify_pins(rows: Iterable, criteria: Criteria) -> list[PinFinding]:
    """Every row's finding, in input order."""
    return [classify_pin(row, criteria) for row in rows]
