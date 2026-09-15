"""Relative ages resolved to a day.

The failure this file is really about is the one that looks like success: an age that
cannot be parsed must come back as None, because the alternative — reading a missing
date as "posted now" — inverts the ranking the date exists to inform, and does it
silently.
"""

from jobtracker.reldate import relative_day

TODAY = "2026-09-15"


def test_the_compact_form_the_readme_feeds_use():
    assert relative_day("2d", TODAY) == "2026-09-13"
    assert relative_day("1w", TODAY) == "2026-09-08"
    assert relative_day("3mo", TODAY) == "2026-06-17"
    assert relative_day("1y", TODAY) == "2025-09-15"


def test_the_prose_form_a_rendered_page_uses():
    """YC dates its listings "about 1 year" and "7 days", never as a timestamp."""
    assert relative_day("7 days", TODAY) == "2026-09-08"
    assert relative_day("about 1 year", TODAY) == "2025-09-15"
    assert relative_day("about 2 months ago", TODAY) == "2026-07-17"
    assert relative_day("a month ago", TODAY) == "2026-08-16"


def test_anything_under_a_day_is_today():
    """One day is the resolution of the field being written. Pretending to more
    precision than the source has would be the only other option."""
    assert relative_day("3h", TODAY) == TODAY
    assert relative_day("about 5 hours ago", TODAY) == TODAY


def test_a_bound_is_floored_at_the_most_recent_day_it_could_mean():
    """Workday says "30+ Days Ago", which is a bound and not a date."""
    assert relative_day("30+ days ago", TODAY) == "2026-08-16"


def test_what_is_not_an_age_is_none_and_never_today():
    for raw in (None, "", "   ", "yesterday", "July 31, 2026", "2026-08-01",
                "soon", "12 parsecs", "-3d"):
        assert relative_day(raw, TODAY) is None, raw


def test_an_unreadable_run_date_refuses_rather_than_reaching_for_a_clock():
    """`today` is a parameter everywhere this is used, so that a parse gives the same
    answer twice. A bad one must not quietly become the real date."""
    assert relative_day("2d", "not-a-date") is None
    assert relative_day("2d", None) is None
