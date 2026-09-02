"""Resume formats: how to read a resume as text, edit it, and assemble a document.

The fourth hand-wired registry in this package, and deliberately the same shape as the
other three — `sources/` for an ATS, `tasks/` for a model pass, `plugins/` for a feed.
One module plus one import line, and the module is **pure** in the same sense theirs are:
it reads text, rewrites text, and describes a command. The one thing it does not do is
run that command — `assemble.py` owns the subprocess, exactly as `runner.py` owns the
socket.

LaTeX is the only format today. The registry exists because the two halves this splits
into — "what is the source text" and "how does it become a PDF" — are the whole of what a
second format (Typst, Markdown-to-PDF) would answer differently, and because a resume
format is a thing a person picks rather than a thing this system detects.

An edit is grounded, and that is the load-bearing rule
-----------------------------------------------------
`apply_edits` will only replace a line it was given **verbatim**. It does not search, it
does not fuzzy-match, and it does not insert. A model that proposes a change to a line
that is not in your resume changes nothing, which is what keeps the preamble — and
therefore the document's ability to compile at all — out of reach by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Edit:
    """One grounded change to one line of a resume.

    `current_line` must occur verbatim in the resume and `evidence` verbatim in the job
    description; both are checked before an Edit is ever built. `suggestion` is the one
    piece of text the model composes, and it is the reason `sanitize` exists.
    """

    section: str
    current_line: str
    suggestion: str
    evidence: str

    def as_dict(self) -> dict:
        return {
            "section": self.section,
            "current_line": self.current_line,
            "suggestion": self.suggestion,
            "evidence": self.evidence,
        }


class ResumeFormat:
    """One way of holding a resume. Pure — see the module docstring."""

    name: str = ""
    suffix: str = ""

    def unavailable_reason(self) -> Optional[str]:
        """Why this format cannot assemble a document right now, or None.

        Missing *tooling*, not missing work — the `Task.unavailable_reason` distinction.
        A format that can still read and edit text but cannot compile says so here, and
        the caller decides whether that is fatal. It usually is not: proposing edits is
        useful with no toolchain installed at all.
        """
        return None

    def is_editable(self, text: str) -> bool:
        """Does this text look like a document this format can edit?"""
        return True

    def sanitize(self, suggestion: str) -> Optional[str]:
        """The suggestion, or None if it may not be written into a document.

        Every format that compiles is a format whose input is a program, so this is a
        security control rather than a style rule. See `latex.py` for what that means
        concretely.
        """
        return suggestion

    def apply_edits(self, text: str, edits) -> tuple[str, int]:
        """`(new_text, applied)` — verbatim replacement only, never insertion."""
        applied = 0
        out = text
        for edit in edits:
            if edit.current_line and edit.current_line in out:
                out = out.replace(edit.current_line, edit.suggestion, 1)
                applied += 1
        return out, applied

    def command(self, source_name: str) -> list:
        """The argv that turns `source_name` into a document, run in its own directory."""
        raise NotImplementedError


_REGISTRY: dict = {}


def register(fmt: ResumeFormat) -> ResumeFormat:
    if not fmt.name:
        raise ValueError("a resume format needs a name")
    _REGISTRY[fmt.name] = fmt
    return fmt


def get_format(name: str) -> Optional[ResumeFormat]:
    return _REGISTRY.get(name)


def formats() -> list:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def for_path(path) -> Optional[ResumeFormat]:
    """The format that owns this file's suffix, or None."""
    suffix = str(path).lower().rsplit(".", 1)
    if len(suffix) != 2:
        return None
    return next((f for f in formats() if f.suffix == "." + suffix[1]), None)
