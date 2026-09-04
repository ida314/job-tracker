"""Which technologies `tailor` is allowed to write onto your resume.

Curation, not observation (DESIGN.md §3.3), and the fourth file of that kind after
companies.yaml, criteria.yaml and profile.yaml: human-authored, and every writer is
something somebody did on purpose — an edit, or a click on the Settings page.

Why it exists
-------------
`tailor` is the one bounded role that composes prose, and its grounding rules keep the
*requirement* and the *resume line* honest without saying anything at all about the
technology names inside a suggestion. The prompt asks it not to add a technology you do
not already have; a prompt is a request, not a bound. A description that says "we use
Kafka" is exactly the input that talks a model into writing Kafka onto your resume, and
the person who then has to defend that in an interview is you.

So there are two mechanisms here and they answer two different questions:

  ``allowed``   goes into the prompt verbatim. This is the steering half — what you know,
                stated once, in your own words, so the model has a vocabulary to work in
                rather than the job description's.
  ``denied``    is a refusal, applied in Python. A suggestion containing a denied term is
                dropped in `parse_edits`, at every posting, forever. It grows one click at
                a time, from terms you were actually shown and actually ruled on.

The asymmetry is the point, and it is the same one `overrides` has over `criteria.yaml`:
the list that *widens* what may be written is prose the model reads, and the list that
*narrows* it is code the model cannot argue with.

An empty file means unrestricted
--------------------------------
`allowed` being empty is read as "no list yet", never as "nothing is allowed". Reading an
absence as a decision is the mistake this repo keeps naming — it is `manual` asserting a
company has no JSON board when nobody had looked, and it would here mean that installing
this feature silently switched tailoring off. The Settings card says which state you are
in rather than leaving you to infer it from an empty box.

`denied` empty means what it says: you have not ruled anything out.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml

_TOP_KEYS = {"allowed", "denied"}

# A term is a technology name, not a sentence. The cap is generous for the longest real
# thing anyone writes ("Amazon Elastic Kubernetes Service") and small enough that a
# pasted paragraph is refused as what it is.
MAX_TERM_CHARS = 60

# How many terms either list may hold. `allowed` is pasted into a prompt, so it is
# bounded for the same reason `MAX_RESUME_CHARS` is: a list nobody bounded is a context
# window somebody else's data gets to fill.
MAX_TERMS = 400


class RefusedTerm(ValueError):
    """The term may not go in either list, and nothing was written."""


@dataclass(frozen=True)
class Keywords:
    """The two lists, and every question anything asks about them."""

    allowed: tuple = ()
    denied: tuple = ()

    @property
    def restricted(self) -> bool:
        """Is there a list at all? Empty `allowed` is 'not configured', never 'none'."""
        return bool(self.allowed)

    @property
    def hash(self) -> str:
        """Identity of the question `tailor` is asked, as far as keywords decide it.

        Over both lists, because both change what may be written — `allowed` through the
        prompt and `denied` through the refusal. Case- and order-insensitive, so
        re-typing the file in a different order does not re-ask every posting, which is
        `profile.prose_hash`'s rule about reordering the prose blocks.
        """
        blob = "|".join(sorted(t.casefold() for t in self.allowed))
        blob += "!!" + "|".join(sorted(t.casefold() for t in self.denied))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def allows(self, term: str) -> bool:
        return _fold(term) in {_fold(t) for t in self.allowed}

    def denies(self, term: str) -> bool:
        return _fold(term) in {_fold(t) for t in self.denied}

    def known(self, term: str) -> bool:
        """Has this term been ruled on, either way? An undecided term is what gets asked."""
        return self.allows(term) or self.denies(term)

    def prompt_block(self) -> str:
        """The `allowed` list as the model sees it. Empty when there is no list.

        Returning "" rather than a sentence about there being no list is deliberate: the
        caller decides what an unconfigured system tells the model, and a prompt that
        says "the candidate has no skills" is worse than one that says nothing.
        """
        return "\n".join(f"- {t}" for t in self.allowed)


def _fold(term: str) -> str:
    return " ".join((term or "").split()).casefold()


def normalize(term: str) -> str:
    """The term as it will be stored: whitespace collapsed, case left alone.

    Case is preserved because these are proper nouns you will read back — `PostgreSQL`
    and `gRPC` are how they are spelled — while every comparison in this module folds.
    """
    return " ".join((term or "").split())


def validate_term(term: str) -> str:
    """The normalized term, or `RefusedTerm` naming what is wrong with it."""
    out = normalize(term)
    if not out:
        raise RefusedTerm("empty term")
    if len(out) > MAX_TERM_CHARS:
        raise RefusedTerm(
            f"that is {len(out)} characters — a term is a technology name, not a "
            f"sentence (limit {MAX_TERM_CHARS})"
        )
    if "\n" in out or "\r" in out:
        raise RefusedTerm("a term is one line")
    return out


# Word boundaries, but not `\b` — that breaks on exactly the terms this is for. `\b`
# after the `+` in "C++" sits between two non-word characters and never matches, and
# "Node.js" would match inside "Node.json". So the boundary is stated as "not adjacent to
# another character a technology name can contain".
_TERM_CHAR = r"[A-Za-z0-9_+#.]"


def _pattern(term: str) -> re.Pattern:
    return re.compile(
        rf"(?<!{_TERM_CHAR}){re.escape(normalize(term))}(?!{_TERM_CHAR})",
        re.IGNORECASE,
    )


def occurs(term: str, text: str) -> bool:
    """Does `term` appear in `text` as a term, rather than inside a longer word?"""
    if not term or not text:
        return False
    return _pattern(term).search(text) is not None


def terms_in(terms: Iterable[str], text: str) -> list:
    """Every term of `terms` that occurs in `text`, in the order given, deduplicated."""
    seen: set = set()
    out: list = []
    for term in terms:
        if _fold(term) in seen:
            continue
        if occurs(term, text):
            seen.add(_fold(term))
            out.append(term)
    return out


def flag_terms(flagged: Iterable) -> list:
    """The term strings out of a flag list, which reaches this module in two shapes.

    Stored flags are `{term, evidence, why}` dicts read back out of a JSON column; a test
    or a caller that has already unpacked them passes strings. Accepting both here rather
    than at each call site keeps every reader of `resume_suggestions.flagged` from having
    to know which it holds.
    """
    out: list = []
    for item in flagged or []:
        term = item.get("term") if isinstance(item, dict) else item
        if isinstance(term, str) and term.strip():
            out.append(term)
    return out


def blocking_terms(suggestion: str, flagged: Iterable, kw: Keywords) -> list:
    """Terms in this suggestion you have not said yes to. Undecided *and* denied.

    Live rather than stored, and that is the whole mechanism: a proposal is written once,
    your ruling happens afterwards, and the same stored row has to read as blocked before
    you decide and applicable after. Storing the answer on the edit would freeze the
    question at the moment it was asked.

    **`allows`, not `known`, and the difference is a real hole this closes.** `parse_edits`
    drops an edit carrying a denied term, which covers every proposal made *after* the
    ruling. It cannot cover the ones already stored: an edit written while Kafka was an
    open question is sitting in the table when you press Exclude, and asking `known` here
    would call that term settled and let the edit compile — turning "never write this
    again" into "never write this again, except in the edits that prompted the ruling".
    Measured, and the reason this docstring is longer than the function.

    Whether a blocked term is a *question* or a *refusal* is a separate reading, and
    `describe_blocked` is where that is decided. Nothing may infer it from this list —
    labelling a permanent refusal "waiting on you" sends somebody to Settings to look for
    a question that is not there.
    """
    return terms_in(
        [t for t in flag_terms(flagged) if not kw.allows(t)], suggestion or ""
    )


def describe_blocked(terms: Iterable, kw: Keywords) -> tuple:
    """`(undecided, denied)` — which of these blocked terms you still owe an answer on.

    Two states that look identical in a list of strings and mean opposite things to the
    person reading the page: one is a question, the other is a decision they already made.
    Rendering them the same way is how a permanent refusal comes to read as an open task.
    """
    terms = list(terms)
    return ([t for t in terms if not kw.known(t)],
            [t for t in terms if kw.denies(t)])


def split_edits(edits, flagged: Iterable, kw: Keywords) -> tuple:
    """`(applicable, blocked)` — blocked as `(edit, terms)` pairs.

    The one derivation of "which of these may be compiled", shared by the CLI, the build
    endpoint and both pages. Two copies of this expression is how the button and the
    terminal come to disagree about which document went out under your name — the rule
    `resume.tailored_stem` already carries.
    """
    flagged = flag_terms(flagged)
    ok: list = []
    blocked: list = []
    for edit in edits:
        terms = blocking_terms(getattr(edit, "suggestion", "") or "", flagged, kw)
        (blocked.append((edit, terms)) if terms else ok.append(edit))
    return ok, blocked


# -- the file ------------------------------------------------------------------------
def load_keywords(path: str | Path) -> Keywords:
    """Parse keywords.yaml. A missing file is an empty one, which means unrestricted.

    Missing is a normal state — this is a file you grow by clicking, so a fresh checkout
    has none — and is therefore not an error, unlike `profile.yaml`. A *malformed* one is,
    for `load_settings`' reason: a config format nothing validates is a comment, and
    reading a typo as "no keywords" would silently unrestrict the thing this file exists
    to restrict.
    """
    path = Path(path)
    if not path.exists():
        return Keywords()

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML — {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: expected a top-level mapping, got {type(data).__name__}"
        )
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown keys: {sorted(unknown)}")

    lists = {}
    for key in ("allowed", "denied"):
        raw = data.get(key) or []
        if not isinstance(raw, list):
            raise ValueError(f"{path}: '{key}' must be a list of terms")
        if len(raw) > MAX_TERMS:
            raise ValueError(f"{path}: '{key}' holds {len(raw)} terms (limit {MAX_TERMS})")
        seen: set = set()
        out: list = []
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                raise ValueError(f"{path}: '{key}' holds a {type(item).__name__}, not a term")
            try:
                term = validate_term(str(item))
            except RefusedTerm as exc:
                raise ValueError(f"{path}: '{key}': {exc}") from exc
            if _fold(term) in seen:
                continue
            seen.add(_fold(term))
            out.append(term)
        lists[key] = tuple(out)

    both = {_fold(t) for t in lists["allowed"]} & {_fold(t) for t in lists["denied"]}
    if both:
        # Not a style complaint: `allows` and `denies` would both answer True, and every
        # caller asking one of them would get a different answer about the same word.
        raise ValueError(
            f"{path}: {sorted(both)} is in both allowed and denied — a term is one or "
            f"the other"
        )
    return Keywords(**lists)


_HEADER = """\
# Technologies `tailor` may write onto your resume. See docs/tailor.md.
#
# Curation: human-authored, and written by the Settings page on a click you made.
#
#   allowed  goes into the model's prompt verbatim — the vocabulary it works in.
#            EMPTY MEANS UNRESTRICTED, not "nothing". There is no list yet.
#   denied   is a refusal applied in Python. A suggestion containing one of these is
#            dropped at every posting, forever, whatever the prompt says.
#
# Changing either list re-asks every posting: the two are hashed into `tailor`'s unit
# key, so a proposal made under the old list is not one made under the new.

"""


def render(kw: Keywords) -> str:
    """The whole file, from scratch. Only used to create one — see `edit`."""
    out = [_HEADER]
    for key, terms in (("allowed", kw.allowed), ("denied", kw.denied)):
        # An empty list is written inline. `key:` with nothing under it parses as None,
        # which the loader tolerates but a person reading the file cannot tell from a
        # list somebody truncated.
        out.append(f"{key}: []\n\n" if not terms
                   else f"{key}:\n" + "".join(f"  - {t}\n" for t in terms) + "\n")
    return "".join(out)


def edit(text: str, key: str, term: str, remove: bool = False) -> str:
    """Add or remove one term in `text`, touching no other line.

    Text surgery rather than a YAML round trip, which is `answers.insert_answer`'s rule
    for the same reason: this file is mostly the comments explaining what the two lists
    mean, and `yaml.safe_dump` deletes every one of them. The header above is the whole
    documentation of a semantic — empty meaning unrestricted — that a person reading the
    file at 2am has no other way to learn.

    A term is written under `key`, and removed from *both* lists when `remove` is set, so
    "forget this ruling" is one operation rather than a question about where it landed.
    """
    if key not in _TOP_KEYS:
        raise RefusedTerm(f"unknown list {key!r}")
    term = validate_term(term)
    lines = (text or "").splitlines(keepends=True)
    if not any(_block_head(ln) for ln in lines):
        lines = render(Keywords()).splitlines(keepends=True)
    blocks = _scan(lines)

    if remove:
        drop = {
            i
            for spans in blocks.values()
            for i in spans["items"]
            if _fold(_item_value(lines[i])) == _fold(term)
        }
        return "".join(ln for i, ln in enumerate(lines) if i not in drop)

    for name, spans in blocks.items():
        for i in spans["items"]:
            if _fold(_item_value(lines[i])) == _fold(term):
                if name == key:
                    raise RefusedTerm(f"{term!r} is already in {key}")
                # Refused here rather than at the swap: writing it would produce a file
                # that fails its own loader, which surfaces as `RefusedWrite` naming a
                # rule the person clicking never saw.
                raise RefusedTerm(
                    f"{term!r} is in {name} — remove it from there first"
                )

    spans = blocks.get(key)
    if spans is None:
        out = list(lines)
        if out and out[-1].strip():
            out.append("\n")
        out.append(f"{key}:\n  - {term}\n")
        return "".join(out)

    out = list(lines)
    head = spans["head"]
    if spans["inline"]:
        # `allowed: []` — the marker has to go, or the file carries a value and a block
        # for one key and PyYAML reads the first.
        out[head] = f"{key}:\n"
    # After the last item, or immediately after the head when there are none. Never at
    # the end of the block's trailing blank lines, which is where a comment about the
    # *next* key lives.
    at = (spans["items"][-1] + 1) if spans["items"] else head + 1
    out.insert(at, f"  - {term}\n")
    return "".join(out)


def _scan(lines: list) -> dict:
    """Where each list lives: its head line, and the index of every item under it."""
    blocks: dict = {}
    current = None
    for i, line in enumerate(lines):
        head = _block_head(line)
        if head is not None:
            current = head
            blocks[head] = {
                "head": i,
                "items": [],
                "inline": bool(line.split(":", 1)[1].strip()),
            }
            continue
        if current is None:
            continue
        if _is_item(line):
            blocks[current]["items"].append(i)
        elif line.strip() and not line.lstrip().startswith("#"):
            # Any other content ends the block — a new top-level key, or garbage.
            current = None
    return blocks


def _block_head(line: str) -> Optional[str]:
    """The list this line opens, or None. Only a column-0 key opens one."""
    if line[:1].isspace() or line.lstrip().startswith("#"):
        return None
    name = line.split(":", 1)[0].strip() if ":" in line else ""
    return name if name in _TOP_KEYS else None


def _is_item(line: str) -> bool:
    return line.lstrip().startswith("- ") or line.strip() == "-"


def _item_value(line: str) -> str:
    return line.lstrip()[1:].strip().strip("\"'")
