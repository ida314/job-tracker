"""The stylesheets, read as data.

A CSS mistake here has no failure mode of its own: the page renders, nothing logs, and the
result can pass for a design decision. So these read the sheets rather than a render.
"""
import re
from pathlib import Path

from jobtracker import dashboard, server

SOURCES = [Path(dashboard.__file__), Path(server.__file__)]


def test_every_css_variable_used_is_defined():
    """`.qq`, `.qa` and `.cocard input` used `var(--line)` for months, which is defined
    nowhere: the whole `border:` shorthand computes to `0 none`, so the fields on
    /posting and /companies had no visible edge. Inline styles count too, so this reads
    the modules rather than the sheets."""
    defined = set(re.findall(r"(--[\w-]+)\s*:", dashboard._CSS))
    used = set()
    for path in SOURCES:
        used |= set(re.findall(r"var\((--[\w-]+)", path.read_text()))
    assert used and used <= defined, sorted(used - defined)


SHEETS = {name: getattr(server, name) for name in dir(server)
          if name.endswith("_CSS") and name.startswith("_")}
SHEETS["dashboard._CSS"] = dashboard._CSS


def _without_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _without_hover_queries(css: str) -> str:
    """Drop each `@media (hover: hover) {…}`, counting braces so a rule inside it closes
    nothing early."""
    out, i = [], 0
    for m in re.finditer(r"@media\s*\(hover:\s*hover\)\s*\{", css):
        if m.start() < i:
            continue
        out.append(css[i:m.start()])
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        i = j
    return "".join(out) + css[i:]


def test_hover_only_applies_to_a_pointer_that_can_hover():
    """On touch `:hover` latches after a tap. `button:hover{opacity:.7}` was the expensive
    one: a button you had just tapped went on looking disabled."""
    for name, css in SHEETS.items():
        assert ":hover" not in _without_hover_queries(_without_comments(css)), name


def test_a_disabled_button_looks_disabled():
    """The generic rule sets `color: inherit`, which beats the browser's own grey, so with no
    state of its own a disabled button was identical to a live one — and still dimmed on
    hover, as if it would do something."""
    assert "button:disabled{opacity:.5;cursor:default}" in server._EXTRA_CSS
    assert "button:not(:disabled):hover" in server._EXTRA_CSS
    assert "button:hover" not in server._EXTRA_CSS
