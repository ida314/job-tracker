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
