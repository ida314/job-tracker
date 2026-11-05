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


def test_no_rounded_table_collapses_its_borders():
    """A radius on a collapsed table is undefined: Chromium drops the border ring and Firefox
    leaves square nubs at the corners. `table` carries the radius, so nothing may collapse."""
    for name, css in SHEETS.items():
        assert "border-collapse:collapse" not in css.replace(" ", ""), name


def test_the_filter_chip_rule_cannot_reach_a_text_token():
    """/tuning and /settings load `dashboard._CSS` and render `.chip` as plain text. The filter
    chip's pointer and `user-select: none` merged onto those, so a token looked clickable and
    could not be selected to copy."""
    assert not re.search(r"(^|[\s,])\.chip\b", _without_comments(dashboard._CSS))
    assert "user-select" in dashboard._CSS.split(".fchip {", 1)[1].split("}", 1)[0]


def test_the_submission_record_is_not_the_page_subtitle():
    """`dashboard._CSS` `.sub` is a grey 13px subtitle, and its colour and size leaked into
    /posting's record of what you sent when that block shared the name."""
    assert not re.search(r"(^|[\s,])\.sub\b", _without_comments(server._POSTING_CSS))


def test_the_filter_chip_inherits_the_page_font():
    """A `<button>` does not inherit a font by default; this one computed to Arial, the only
    control on the page that did."""
    assert "font: inherit" in dashboard._CSS.split(".fchip {", 1)[1].split("}", 1)[0]


def test_the_file_input_button_is_themed():
    """Left to the browser it is a grey native button in its own font, and in dark mode a grey
    slab, beside themed buttons on every pick card."""
    assert "::file-selector-button {" in dashboard._CSS


def test_the_nav_is_styled_on_the_pages_that_load_only_the_base_sheet():
    """`/` and `/jobboards` load `dashboard._CSS` alone, and every page renders `_NAV`."""
    assert ".nav {" in dashboard._CSS
    assert ".nav{" not in server._EXTRA_CSS


def _rules(css: str):
    """`(selector, declarations)` for each rule, descending into at-rules."""
    return re.findall(r"([^{}]+)\{([^{}]*)\}", _without_comments(css))


CONTROL = re.compile(r"(^|[\s,.])(button|input|select|textarea)\b|\.ddl\b|\.apply\b|a\.btn\b|\.kwbox\b")


def test_every_control_takes_the_control_radius():
    """Controls had four radii (7, 6, 5, 4px) depending on which page's sheet drew them."""
    for name, css in SHEETS.items():
        for selector, body in _rules(css):
            found = re.search(r"border-radius:\s*([^;}]+)", body)
            if found and CONTROL.search(selector):
                assert found.group(1).strip() == "var(--radius-control)", (name, selector)


FIELD = re.compile(r"(^|[\s,.])(input|select|textarea)\b|\.answer\b|\.kwbox\b|\.lv\b|\.bankkey\b|\.bankval\b|\.qq\b")


def test_a_field_edge_clears_three_to_one():
    """`currentColor` drew near-black boxes on some pages and `--rule`, about 1.7:1 on the page,
    drew barely visible ones on others. An input's edge is what says it is an input."""
    for name, css in SHEETS.items():
        for selector, body in _rules(css):
            found = re.search(r"border:\s*1px solid ([^;}]+)", body)
            if found and FIELD.search(selector) and "::file-selector-button" not in selector:
                assert found.group(1).strip() == "var(--muted)", (name, selector)


def test_hover_does_not_dim_a_control():
    """Dimming on hover says "less available" at the moment you reach for it, and it is what a
    disabled button looks like. Hover darkens the text and the edge."""
    for name, css in SHEETS.items():
        for selector, body in _rules(css):
            found = re.search(r"opacity:\s*([\d.]+)", body)
            if ":hover" in selector and found:
                assert float(found.group(1)) == 1, (name, selector)
