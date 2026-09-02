"""LaTeX: the first resume format, and the only one today.

Reading it needs nothing — a .tex file is text, which is the whole reason the resume's
source of truth is LaTeX rather than the PDF it compiles to. Writing it needs care, and
that care is `sanitize` below.

Why a control-sequence allowlist is a security control
------------------------------------------------------
TeX is a programming language with filesystem access. `\\input` reads a file into the
document, `\\write`/`\\openout` create one, `\\catcode` and `\\csname` rewrite what the
rest of the source even means, and under `--shell-escape` `\\write18` runs a shell
command. A resume is assembled from text a language model composed, so "the model writes
a suggestion into a document we then compile" is, without a guard, "the model writes a
program we then run".

So `sanitize` refuses any control sequence not on a small list of the ones a resume line
actually needs — emphasis, escapes, and the odd spacing macro. An allowlist rather than a
blocklist for the usual reason: the blocklist is a guess about what is dangerous, and
`\\csname` composes new command names out of characters, so guessing wrong is quiet.

This is belt *and* braces. `assemble.py` also compiles with shell-escape explicitly off,
in a scratch directory, under a timeout. Either alone would probably do; the combination
is what makes "probably" not matter, and `apply_edits` refusing to touch a line it was not
given verbatim means the preamble is out of reach besides.
"""

from __future__ import annotations

import re
import shutil
from typing import Optional

from .base import ResumeFormat, register

# The engine, and the only one this looks for. Tectonic fetches exactly the packages a
# document needs and caches them, which is what makes it ~30MB in an image rather than
# the ~500MB a useful TeX Live subset costs.
ENGINE = "tectonic"

# Control sequences a resume line legitimately contains. Formatting, escaped literals,
# and list structure — nothing that reads a file, writes one, or defines a macro.
ALLOWED_COMMANDS = frozenset({
    # emphasis and weight
    "textbf", "textit", "emph", "underline", "texttt", "textsc", "textsuperscript",
    # structure a line can carry
    "item", "\\",
    # escaped literals: the characters TeX reserves
    "%", "&", "_", "#", "$", "{", "}", "~", "^",
    # spacing and punctuation
    ",", ";", ":", "!", " ", "-", "ldots", "dots", "quad", "qquad",
})

# A control sequence is a backslash followed by letters, or by exactly one non-letter.
_COMMAND = re.compile(r"\\([A-Za-z]+|.)")

# Grouped so a stray brace cannot silently swallow the rest of the document.
_OPEN = re.compile(r"(?<!\\)\{")
_CLOSE = re.compile(r"(?<!\\)\}")


class Latex(ResumeFormat):
    name = "latex"
    suffix = ".tex"

    def unavailable_reason(self) -> Optional[str]:
        """Reading and editing need nothing; only assembling needs the engine.

        Named as an absence rather than reported as a failure, because a box with no TeX
        installed still gets every suggestion — it just cannot build the PDF, and a
        missing capability that announces itself is the whole point.
        """
        if not shutil.which(ENGINE):
            return (
                f"no {ENGINE} on PATH — suggestions still work, "
                f"but nothing can be assembled into a PDF"
            )
        return None

    def is_editable(self, text: str) -> bool:
        return "\\begin{document}" in text or "\\documentclass" in text

    def sanitize(self, suggestion: str) -> Optional[str]:
        """The suggestion, or None if it may not go into a document we compile.

        Three refusals, and the first is the one that matters. See the module docstring
        for why this is an allowlist.
        """
        if not suggestion or not suggestion.strip():
            return None
        for match in _COMMAND.finditer(suggestion):
            if match.group(1) not in ALLOWED_COMMANDS:
                return None
        # Unbalanced braces do not execute anything, but they do change where the group
        # a line sits in ends — which can silently reformat or swallow what follows.
        if len(_OPEN.findall(suggestion)) != len(_CLOSE.findall(suggestion)):
            return None
        # A comment character would comment out the rest of the physical line, including
        # anything the original line had after the part being replaced.
        if re.search(r"(?<!\\)%", suggestion):
            return None
        return suggestion

    def command(self, source_name: str) -> list:
        """Tectonic, with shell-escape off and the network told to stay put.

        `--keep-logs=false` and `--print` are deliberately absent: the log is what says
        why a compile failed, and `assemble` reads it into the error it raises.
        """
        return [
            ENGINE,
            "-X", "compile",
            "--keep-intermediates=false",
            "--untrusted",          # refuses shell-escape and \write18 outright
            "--outfmt", "pdf",
            source_name,
        ]


register(Latex())
