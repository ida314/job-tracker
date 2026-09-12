"""The extra questions an application form asks, and how a template seeds them.

Pure functions over rows — no SQL, no I/O, no HTML. The same split `applications.py` and
`rank.py` make, and for the same reason: two callers merge this list (the page that
renders it and the freeze that records it), and they must not be able to disagree about
what "the questions on this posting" means.

A resume and a cover letter cover most of an application. What is left is the handful of
short questions every ATS asks in its own wording — work authorization, sponsorship, why
this company, expected graduation — and before this module there was nowhere to put the
answers, so they were retyped into every form and forgotten afterwards.

Two tables hold them, and the split is the point:

* **`question_template`** is the list you maintain once, on /settings, with a default
  answer. It is a *checklist*, not an answer bank: nothing reads it to fill a form, and
  nothing in this repo sends it anywhere.
* **`posting_answers`** is what you actually put for one posting.

`merge` puts them on the page together **without writing anything**, because a GET never
mutates (server.py's invariant). A template row only becomes a `posting_answers` row when
you save it — which is also why the freeze ignores template rows you never touched. A
default you never typed is not something you submitted.

Not the answer bank: `answers.yaml`'s `answers:` block belongs to the mothballed prefill
half, which fills employer forms programmatically and is switched off
(`config.PREFILL_ENABLED`). Its companion `prefill_gaps` is populated by DOM scraping that
is off with it, so a template derived from either would be empty forever. These questions
are typed by you, into forms you fill by hand.
"""

from __future__ import annotations

import hashlib
import re

# How many words of a question survive into its key. Long enough that two genuinely
# different questions rarely collide, short enough that the id stays readable in a URL and
# in a JSON snapshot you might read years later.
_KEY_WORDS = 8


def normalize(question: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed.

    The same shape `store.normalize_label` and `answers.normalize_label` use. Duplicated
    here on purpose rather than imported: both of those belong to the prefill half, and a
    live module importing a mothballed one is how a switched-off feature comes back.
    """
    return re.sub(r"[^a-z0-9]+", " ", (question or "").lower()).strip()


def mint_qid(question: str, taken: set[str] | None = None) -> str:
    """A stable key for one question, unique within `taken`.

    Minted from the text **once** and never re-derived, which is what lets you fix a
    question's wording without orphaning every answer already given to it. That is the
    whole difference from `store.manual_job_id`, whose determinism is the feature: here
    the caller stores the result and passes it back on every later save.

    A question that normalizes to nothing — pure punctuation — falls back to a digest, or
    every such question on one posting would collapse onto the same row. The numeric
    suffix does the same job for two questions that merely slug alike.
    """
    words = normalize(question).split()[:_KEY_WORDS]
    base = "_".join(words) or "q_" + hashlib.sha1(
        (question or "").encode()
    ).hexdigest()[:8]
    taken = taken or set()
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    # A thousand questions slugging alike on one posting is not a state worth a branch
    # anywhere else; a digest of the whole text ends it.
    return f"{base}-{hashlib.sha1((question or '').encode()).hexdigest()[:8]}"


def merge(stored, template) -> list[dict]:
    """The question list one posting's page renders, newest-wins, without writing.

    `stored` is `store.posting_answers(...)`; `template` is `store.template_questions()`.
    A stored row wins over a template row with the same `qid` — the template seeds, it
    never owns — and template rows the posting has no answer for follow, carrying
    `from_template: True` and the template's default in `answer`.

    Ordering is computed here and never written back. Sorting on `(ordinal, qid)` across
    the merged list rather than "stored first, then template" is what stops a stored row
    at ordinal 3 and a template row at ordinal 3 from swapping places under the cursor
    after a save; a newly saved row takes `max(ordinal) + 1` so saving never moves
    anything above it.

    `saved` is what the page needs to know whether `×` has anything to delete and whether
    the freeze will see this row at all.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for row in stored or ():
        seen.add(row["qid"])
        rows.append({
            "qid": row["qid"],
            "question": row["question"],
            "answer": row["answer"] or "",
            "ordinal": row["ordinal"],
            "from_template": False,
            "saved": True,
        })
    for row in template or ():
        if row["qid"] in seen:
            continue
        rows.append({
            "qid": row["qid"],
            "question": row["question"],
            # The default, rendered for you to accept or change. It is NOT an answer
            # until you save it, and `answered` below is what the freeze reads.
            "answer": row["answer"] or "",
            "ordinal": row["ordinal"],
            "from_template": True,
            "saved": False,
        })
    rows.sort(key=lambda r: (r["ordinal"], r["qid"]))
    return rows


def answered(stored) -> list[dict]:
    """What the freeze records: saved rows carrying a non-empty answer.

    Two exclusions, both deliberate. A template row you never saved is not here because it
    is not in `stored` — CLAUDE.md's "nothing may put a value in a field the user did not
    give for it", applied inside the one artifact whose job is to be true about what you
    sent. A saved row with an empty answer is not here either: keeping the question but
    recording no reply would read afterwards as "I left this blank", which is a claim
    about the form, not about you.

    The page still renders both; being visible and being submitted are different things.
    """
    return [
        {"qid": r["qid"], "question": r["question"], "answer": r["answer"]}
        for r in stored or ()
        if (r["answer"] or "").strip()
    ]
