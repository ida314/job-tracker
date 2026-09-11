r"""Cover letters: a LaTeX template with slots, and the rules for filling them.

Pure, in the sense `resume/base.py` and every `tasks/` module are pure: it reads text,
rewrites text, and decides nothing about when. `resume/assemble.py` still owns the one
subprocess, and `tasks/coverletter.py` owns the model call.

Why this is not a second `tailor`
---------------------------------
`tailor` edits a document you wrote. Its whole guard is built around that: `current_line`
is a verbatim quote from your resume, `apply_edits` replaces only a line it was handed,
and `latex.sanitize` lets a suggestion keep exactly the macros the line it replaces
already ran. The model writes LaTeX, so the guard has to read LaTeX.

A cover letter is the opposite shape. There is no line to quote, because the paragraph
does not exist yet — the model is composing prose into a slot, which is the thing
`tailor` is written to prevent. So the bound cannot be "the same skeleton comes back";
it has to be that **the model never writes LaTeX at all**.

That is what `escape_text` is for, and it is a stronger guarantee than an allowlist
rather than a weaker one. A backslash in a composed paragraph becomes
`\textbackslash{}`; `{`, `}`, `$`, `&`, `#`, `^`, `_`, `%` and `~` all become their
escaped literals. There is no string a model can return that reaches the engine as a
control sequence, so there is no list of dangerous commands to keep current and no
`\csname` to compose one out of characters. The allowlist in `resume/latex.py` exists
because `tailor` genuinely needs to pass `\resumeItem{...}` through; nothing here does.

The template owns the letter, and its comments are the brief
------------------------------------------------------------
A slot is not something you mark up for this program. It is what the template already
looks like: a run of `%` comments saying what a paragraph should do, followed by a
paragraph of placeholder prose in SHOUTING CAPS.

    % Paragraph 2:
    % Use the strongest and most directly relevant professional experience.
    % ...
    In my work at MOST RELEVANT COMPANY OR EXPERIENCE, I MOST RELEVANT ACCOMPLISHMENT.

The comment becomes that slot's brief in the prompt, and the paragraph beneath it is
what gets replaced. Two consequences worth stating because they are the design:

* **You steer the letter by editing the template**, in the template's own idiom, with no
  second configuration file to keep in step. Rewriting a `% Paragraph 3:` comment is how
  you change what paragraph 3 argues.
* **A paragraph with no placeholder is not a slot.** The closing line of the shipped
  template — "Thank you for your consideration" — carries a comment and no CAPS, so it is
  already written and is passed through byte for byte. The rule is mechanical and it
  means fixed prose stays fixed.

Everything outside `\begin{document}` is out of reach entirely, which puts the preamble,
the packages and your own contact details where no slot can address them — the same
by-construction safety `apply_edits` gets from refusing to insert.

What is filled deterministically, and what is not
-------------------------------------------------
`COMPANY`, `JOB TITLE` and `DATE` are facts this program holds. They are substituted by
`fill`, from the posting row and the run's `today`, and the model is never asked for them
— a letter addressed to the wrong employer is the one failure a cover letter cannot
survive, and it is not a thing to ask a language model to get right when the database
already knows. The model composes paragraphs and nothing else.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The facts `fill` substitutes wherever they appear, in the order they are applied.
# Longest first so a token that contains another cannot be half-replaced; today none do,
# and relying on that rather than stating it is how that stops being true.
COMPANY_TOKEN = "COMPANY"
TITLE_TOKEN = "JOB TITLE"
DATE_TOKEN = "DATE"

# A placeholder is a run of two-or-more-letter capitalized words. Two letters rather than
# three because "OR" and "AI" are both real words in these briefs; the run has to be all
# caps throughout, which is what keeps it off ordinary prose and off `\LetterSubject`-
# style CamelCase macro names.
_PLACEHOLDER = re.compile(r"[A-Z]{2,}(?:[ ,/&-]+[A-Z]{2,})*")

# TeX's ten reserved characters, and what each has to become in text a model composed.
# The backslash is first and is the one that matters: with it escaped there is no control
# sequence left to allow or refuse, which is the whole security argument in this module.
_ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "^": r"\textasciicircum{}",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
}
_ESCAPE_RE = re.compile("|".join(re.escape(c) for c in _ESCAPES))

_WS = re.compile(r"\s+")


class TemplateError(ValueError):
    """The template cannot be used, phrased for the person who has to fix it."""


@dataclass(frozen=True)
class Slot:
    """One paragraph the model is asked to write.

    `brief` is the template's own comment, verbatim minus the `%` markers — it is the
    instruction, and it is the reason there is no prompt to edit in this repo when you
    want a different letter. `body` is the placeholder prose being replaced, kept so the
    prompt can show the shape that was asked for and so `fill` can find it again.
    """

    key: str
    brief: str
    body: str
    start: int
    end: int

    @property
    def placeholders(self) -> list:
        """The SHOUTING CAPS spans in this slot's placeholder prose, in order."""
        return _PLACEHOLDER.findall(self.body)


@dataclass(frozen=True)
class Template:
    """A parsed cover-letter template: the source, and the slots inside its document."""

    text: str
    slots: list = field(default_factory=list)

    @property
    def hash(self) -> str:
        """Over the whole source, so any edit re-asks every posting.

        The same call `tailor` makes about the resume, for the same reason: the template
        is half the question. Change a brief and the letters written under the old one
        are answers to something you no longer asked.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    @property
    def keys(self) -> list:
        return [s.key for s in self.slots]


def escape_text(text: str) -> str:
    r"""Plain text, as LaTeX that types it literally.

    Every reserved character becomes its escape, `\` included, so the result carries no
    control sequence whatever the input was. Curly quotes and dashes are left alone —
    they are UTF-8 and the shipped template declares `\input{glyphtounicode}` and a UTF-8
    engine, so they set correctly and a model that writes one is not doing anything
    wrong.
    """
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], text or "")


def flat(text: str) -> str:
    """Whitespace-normalized, for containment checks. The helper `inbox` and `tailor` use."""
    return _WS.sub(" ", (text or "")).strip()


def parse(text: str) -> Template:
    """Read a template's slots. Raises `TemplateError` if it is not one.

    A slot is a run of `%` comment lines followed by a run of ordinary lines that
    contains a placeholder, found only between `\\begin{document}` and `\\end{document}`.
    Blocks whose prose carries no placeholder are fixed text and are not slots; blocks
    carrying `\\begin` or `\\end` are structure and are never slots however they are
    commented.
    """
    if not text or not text.strip():
        raise TemplateError("the cover-letter template is empty")
    if _code_offset(text, "\\documentclass") < 0:
        raise TemplateError(
            "that does not look like a LaTeX document — no \\documentclass"
        )

    # Found in code, never in a comment, and that distinction is load-bearing rather than
    # fastidious. A template worth reading explains itself, and the natural way to explain
    # this one is to write "everything outside \begin{document} is out of reach" in a
    # comment at the top. A plain `text.find` takes that mention as the document start,
    # which drags the whole preamble into slot range — so the sentence describing the
    # safety property is the thing that removes it. The example template did exactly this
    # and turned `\newcommand{\YourName}{YOUR NAME}` into a slot.
    begin = _code_offset(text, "\\begin{document}")
    end = _code_offset(text, "\\end{document}")
    if begin < 0 or end < 0 or end <= begin:
        raise TemplateError(
            "the template has no \\begin{document}...\\end{document} body to fill"
        )

    slots: list = []
    for comment, body, start, stop in _blocks(text, begin, end):
        if "\\begin" in body or "\\end" in body:
            continue
        if not _PLACEHOLDER.search(body):
            # Prose with nothing to fill in is prose you already wrote. The shipped
            # template's closing line lands here, and passes through untouched.
            continue
        slots.append(Slot(
            key=f"p{len(slots) + 1}",
            brief=comment,
            body=body,
            start=start,
            end=stop,
        ))
    return Template(text=text, slots=slots)


def _uncommented(line: str) -> str:
    """The part of a line TeX will actually read — everything before an unescaped `%`."""
    found = re.search(r"(?<!\\)%", line)
    return line[:found.start()] if found else line


def _code_offset(text: str, marker: str) -> int:
    """Where `marker` first appears outside a comment, or -1.

    Line by line rather than by stripping the whole document, so the offset returned is
    into the original text and every span `parse` hands out stays valid against it.
    """
    at = 0
    for line in text.splitlines(keepends=True):
        found = _uncommented(line).find(marker)
        if found >= 0:
            return at + found
        at += len(line)
    return -1


def _blocks(text: str, begin: int, end: int):
    """`(comment, body, start, stop)` for each comment-then-prose block in the body.

    Offsets are into the whole source, so `fill` can splice without re-finding anything
    — a body paragraph is ordinary English and two of them could easily be equal, which
    is exactly the case a `str.replace` would get wrong and an offset cannot.
    """
    lines = text.splitlines(keepends=True)
    # Where each line starts, so a block's span is arithmetic rather than a search.
    offsets: list = []
    at = 0
    for line in lines:
        offsets.append(at)
        at += len(line)

    comment: list = []
    body: list = []
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if offsets[i] < begin or offsets[i] >= end:
            continue
        stripped = line.strip()
        if stripped.startswith("%"):
            if body:
                # A comment after prose closes the block before it.
                yield "\n".join(comment), "".join(body), start, offsets[i]
                comment, body, start = [], [], None
            comment.append(stripped.lstrip("%").strip())
            continue
        if not stripped:
            if body:
                yield "\n".join(comment), "".join(body), start, offsets[i]
                comment, body, start = [], [], None
            elif comment:
                # A blank line between a comment and its prose breaks the pairing; the
                # comment was a section marker, not a brief.
                comment = []
            continue
        if not comment and not body:
            continue
        if start is None:
            start = offsets[i]
        body.append(line)
    if body:
        # A block running to the end of the document body. Bounded by `end` rather than
        # by the last line, so a slot can never be spliced over `\end{document}`.
        yield "\n".join(comment), "".join(body), start, end


def fill(
    template: Template,
    paragraphs: dict,
    company: str,
    title: str,
    today: str,
) -> str:
    """The template with its slots written and its facts substituted.

    Assembled segment by segment, and the substitution runs on **the template's own text
    only** — never on a paragraph the model composed. That is not tidiness. `COMPANY` is
    an ordinary English word, so a letter that says "we discussed COMPANY at length"
    would otherwise have that word rewritten into the employer's name by a step the model
    is not supposed to be part of. Walking the whole document once after splicing is the
    obvious way to write this and it is the way that has that hole; taking the template's
    segments and the composed paragraphs as separate pieces is what closes it.

    A slot with no paragraph keeps its placeholder prose — substituted, because that
    prose *is* template text. Deliberate: a letter with a visible `RELEVANT AREA OR
    THEME` in it is obviously unfinished, where a silently dropped paragraph reads as a
    letter somebody meant to be short. `coverletter.parse_letter` is all-or-nothing so
    this stays a safety net rather than a state anything reaches on purpose.
    """
    facts = (
        (TITLE_TOKEN, title),
        (COMPANY_TOKEN, company),
        (DATE_TOKEN, format_date(today)),
    )

    def substitute(chunk: str) -> str:
        for token, value in facts:
            chunk = chunk.replace(token, escape_text(value or ""))
        return chunk

    out: list = []
    at = 0
    for slot in sorted(template.slots, key=lambda s: s.start):
        out.append(substitute(template.text[at:slot.start]))
        composed = (paragraphs or {}).get(slot.key)
        if composed and composed.strip():
            out.append(escape_text(composed.strip()) + "\n")
        else:
            out.append(substitute(template.text[slot.start:slot.end]))
        at = slot.end
    out.append(substitute(template.text[at:]))
    return "".join(out)


def format_date(today: str) -> str:
    """`2026-09-11` as `September 11, 2026`, or unchanged if it is not a date.

    Unchanged rather than "today": the same call `normalize_posted_at` makes. A date this
    cannot read is a template that will show what it was given, which is visible; a date
    quietly replaced by the clock is a letter dated wrongly and nothing says so.
    """
    from datetime import date

    try:
        parsed = date.fromisoformat((today or "").strip())
    except ValueError:
        return today or ""
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def load(path: Optional[Path] = None) -> tuple:
    """`(template, error)` for the template at `path`, never both.

    Read by the caller and handed to the task on the context, for the reason
    `_load_resume` is: **a task never opens a file**, so its queue cannot depend on a
    path being mounted at unit time.
    """
    from . import config

    target = Path(path) if path else config.COVERLETTER_TEX
    if not target.is_file():
        return None, (
            f"no cover-letter template at {target} — write one, or set "
            f"$JOBTRACKER_COVERLETTER_TEX"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"the cover-letter template did not load: {exc}"
    try:
        parsed = parse(text)
    except TemplateError as exc:
        return None, str(exc)
    if not parsed.slots:
        return None, (
            f"{target.name} declares no paragraphs to fill — a slot is a % comment "
            f"followed by prose containing a PLACEHOLDER in capitals"
        )
    return parsed, None


def letter_stem(company: str, ats_job_id: str) -> str:
    """The basename, without suffix, of one posting's cover letter.

    Derived and never stored, exactly as `resume.tailored_stem` is, and for the same
    reason: the file's own existence is what "this posting has a letter" means, so the
    four callers that reach for it have to agree on the expression. `resumes.stored_name`
    slugs to `[a-z0-9_]`, which is what lets the download route take these two strings
    straight off a query string.
    """
    from . import resumes

    base = resumes.stored_name(company, ats_job_id, "").rstrip(".") or "letter"
    return f"{base}_letter"


def letter_path(company: str, ats_job_id: str) -> Path:
    """Where `coverletter build` writes that posting's PDF, and where everything looks."""
    from . import config

    return config.LETTERS_DIR / f"{letter_stem(company, ats_job_id)}.pdf"
