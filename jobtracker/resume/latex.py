r"""LaTeX: the first resume format, and the only one today.

Reading it needs nothing — a .tex file is text, which is the whole reason the resume's
source of truth is LaTeX rather than the PDF it compiles to. Writing it needs care, and
that care is `sanitize` below.

Why a control-sequence allowlist is a security control
------------------------------------------------------
TeX is a programming language with filesystem access. `\input` reads a file into the
document, `\write`/`\openout` create one, `\catcode` and `\csname` rewrite what the
rest of the source even means, and under `--shell-escape` `\write18` runs a shell
command. A resume is assembled from text a language model composed, so "the model writes
a suggestion into a document we then compile" is, without a guard, "the model writes a
program we then run".

So `sanitize` refuses any control sequence not on a small list of the ones a resume line
actually needs — emphasis, escapes, and the odd spacing macro. An allowlist rather than a
blocklist for the usual reason: the blocklist is a guess about what is dangerous, and
`\csname` composes new command names out of characters, so guessing wrong is quiet.

**A resume's own macros are the second half of that list, and without them there is no
first half.** A real resume does not write `\item` — it writes `\resumeItem{...}`,
`\resumeSubheading{...}` and `\href{...}{...}`, macros the document defines in its own
preamble. A fixed global list knows none of them, so it refused *every* bullet rewrite:
measured 2026-09-07 against the live corpus, 93 proposals carried 0 edits and 16 of 17
otherwise-grounded edits died here, each one ordinary prose inside the same
`\resumeItem{}` the line it replaced already used. A guard that cannot pass the only
line shape its input actually has is not strict, it is off.

The widening is bounded by the line, not by the document: a command is allowed because
**the line being replaced already runs it**. So `sanitize` takes that line as `context`,
and two rules fall out of it —

  * a control sequence must be on the global list *or* already in that line, and
  * the line's *skeleton* — its commands and its grouping — must come back unchanged.

The second is what "do not change how this is presented" means in code. Only the prose
between the commands may move; a suggestion cannot add a `\textbf`, drop one, reorder
them, or change how many arguments a macro is handed. It also does most of the security
work for free, since a command can only appear where the original already had it.

`NEVER_ALLOWED` is the exception to the widening, and it is deliberately a blocklist
inside an allowlist: the primitives that read files, write them, or redefine what the
source means are never picked up from a line, however the document uses them. Without it
a resume that legitimately said `\input{skills.tex}` would license a suggestion of
`\input{/etc/passwd}` — the widening is about the *shape* of a line, and these are the
commands whose argument is the whole risk.

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

# Never taken from `context`, whatever the document does with them. These are the
# primitives whose *argument* is the risk rather than their presence: they read a file,
# create one, or rewrite what the rest of the source means. A resume that legitimately
# uses one does not thereby license a model to hand it a different argument.
NEVER_ALLOWED = frozenset({
    # reading and writing the filesystem
    "input", "include", "InputIfFileExists", "openin", "openout", "read", "write",
    "immediate", "special", "shipout", "write18",
    # changing what the source means
    "catcode", "csname", "endcsname", "def", "edef", "gdef", "xdef", "let", "futurelet",
    "newcommand", "renewcommand", "providecommand", "DeclareRobustCommand",
    "expandafter", "noexpand", "afterassignment", "aftergroup",
    # loading code
    "usepackage", "RequirePackage", "documentclass", "input@path",
})

# Control sequences that carry no structure: escaped literals and spacing. They are part
# of the prose rather than part of the layout, so they may come and go freely between a
# line and its replacement — a rewrite that drops a "2\%" is a wording change, not a
# presentation change. Everything else is skeleton.
_CONTENT_COMMANDS = frozenset({
    "%", "&", "_", "#", "$", "{", "}", "~", "^",
    ",", ";", ":", "!", " ", "-", "ldots", "dots", "quad", "qquad", "\\",
})

# A control sequence is a backslash followed by letters, or by exactly one non-letter.
_COMMAND = re.compile(r"\\([A-Za-z]+|.)")

# Grouped so a stray brace cannot silently swallow the rest of the document.
_OPEN = re.compile(r"(?<!\\)\{")
_CLOSE = re.compile(r"(?<!\\)\}")


def _commands(text: str) -> list:
    """Every control sequence in `text`, in order, without its backslash."""
    return [m.group(1) for m in _COMMAND.finditer(text or "")]


def _skeleton(text: str) -> list:
    """The commands that decide how a line is laid out, in order.

    Escapes and spacing are excluded (see `_CONTENT_COMMANDS`): they are prose. What is
    left is the part a suggestion must hand back unchanged.
    """
    return [c for c in _commands(text) if c not in _CONTENT_COMMANDS]


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

    def sanitize(self, suggestion: str, context: Optional[str] = None) -> Optional[str]:
        """The suggestion, or None if it may not go into a document we compile.

        `context` is the line this suggestion replaces. Given one, the resume's own
        macros are allowed — because that line already runs them — and the line's
        skeleton has to come back unchanged. See the module docstring for why the
        global list alone could not pass a single real bullet.

        Omitting `context` keeps the old behaviour exactly: the global allowlist, and no
        opinion about layout. That is the right reading for a caller with no line to
        compare against, and it is why this stays a widening rather than a replacement.
        """
        if not suggestion or not suggestion.strip():
            return None

        allowed = set(ALLOWED_COMMANDS)
        if context:
            # Bounded by the line, and then bounded again: a document that uses `\input`
            # does not get to hand a model's suggestion a different filename.
            allowed |= {c for c in _commands(context) if c not in NEVER_ALLOWED}
        for command in _commands(suggestion):
            if command not in allowed:
                return None

        # "Do not change how this is presented", enforced rather than asked for. The
        # commands that lay the line out must come back in the same order and the same
        # number, so only the prose between them can move — no new emphasis, no dropped
        # emphasis, no reordering. The prompt states this rule too; a prompt is a request.
        if context and _skeleton(suggestion) != _skeleton(context):
            return None

        # Unbalanced braces do not execute anything, but they do change where the group
        # a line sits in ends — which can silently reformat or swallow what follows.
        if len(_OPEN.findall(suggestion)) != len(_CLOSE.findall(suggestion)):
            return None
        # Balanced is not enough once there is a line to compare against: `\resumeItem`
        # takes one argument, and a suggestion handing it two is balanced, skeleton-equal
        # and a compile error. Same group count as the line it replaces.
        if context and (len(_OPEN.findall(suggestion))
                        != len(_OPEN.findall(context))):
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
