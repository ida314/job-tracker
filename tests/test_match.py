"""The gate suite: prove the corrected criteria classify known titles correctly."""

import pytest

from jobtracker import config
from jobtracker.criteria import load_criteria
from jobtracker.match import location_rank, match
from jobtracker.models import Decision, Posting


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


def _p(title, location=""):
    return Posting("Acme", "1", title, "https://example.com/1", location)


MATCH_CASES = [
    "Software Engineer, New Grad",
    "New Grad Software Engineer (2027)",
    "University Graduate, Distributed Systems",
    "Associate Backend Engineer",
    "Associate Software Engineer",
    "Software Engineer I",
    "Early Career Platform Engineer",
    "Junior Site Reliability Engineer",
    "New Grad Software Developer",
]

REJECT_CASES = [
    ("Senior Software Engineer", "excluded_title:senior"),
    ("Sr. Backend Engineer", "excluded_title:sr."),
    ("Staff Platform Engineer", "excluded_title:staff"),
    ("Software Engineer II", "excluded_title:ii"),
    ("Software Engineer III", "excluded_title:iii"),
    ("Engineering Manager", "excluded_title:manager"),
    ("New Grad Frontend Engineer", "excluded_role:frontend"),
    ("New Grad Machine Learning Engineer", "excluded_role:machine learning"),
    ("New Grad iOS Engineer", "excluded_role:ios"),
    ("Software Engineering Intern", "excluded_title:intern"),
    # Entry-level but non-engineering business roles -> REJECT, not MATCH.
    ("Finance Associate", "non_engineering_role:level=associate"),
    ("Operations Associate", "non_engineering_role:level=associate"),
    ("Customer Success Associate (New Grad)", "excluded_role:customer success"),
    ("Associate Solutions Consultant", "excluded_role:solutions consultant"),
]

UNCERTAIN_CASES = [
    "Software Engineer",
    "Backend Engineer",
    "Distributed Systems Engineer",
    "Platform Engineer",
]


@pytest.mark.parametrize("title", MATCH_CASES)
def test_match(criteria, title):
    v = match(_p(title, "New York"), criteria)
    assert v.decision is Decision.MATCH, f"{title!r} -> {v.decision} ({v.reason})"


@pytest.mark.parametrize("case", REJECT_CASES)
def test_reject(criteria, case):
    title, expected_reason = case[0], case[1]
    location = case[2] if len(case) > 2 else "New York"
    v = match(_p(title, location), criteria)
    assert v.decision is Decision.REJECT, f"{title!r} -> {v.decision} ({v.reason})"
    assert v.reason == expected_reason


@pytest.mark.parametrize("title", UNCERTAIN_CASES)
def test_uncertain(criteria, title):
    v = match(_p(title, "New York"), criteria)
    assert v.decision is Decision.UNCERTAIN, f"{title!r} -> {v.decision} ({v.reason})"


def test_ii_does_not_fire_inside_iii(criteria):
    # 'Engineer III' must reject on iii, not on ii, and must not be missed.
    v = match(_p("Software Engineer III", "NYC"), criteria)
    assert v.reason == "excluded_title:iii"


def test_swe_i_not_matched_by_engineer_ii(criteria):
    # 'Software Engineer II' must not be read as the level token 'Software Engineer I'.
    v = match(_p("Software Engineer II", "NYC"), criteria)
    assert v.decision is Decision.REJECT and v.reason == "excluded_title:ii"


def test_verdict_carries_identity(criteria):
    v = match(_p("Senior Engineer"), criteria)
    assert v.company == "Acme" and v.ats_job_id == "1" and v.decided_by == "rules"


# -- location: ranks, never gates --------------------------------------------------
def test_location_never_rejects(criteria):
    """Geography stopped being a gate on 2026-07-22. Nothing is disqualified for it."""
    for loc in ("London, UK", "Bengaluru, India", "Toronto, ON", "Shanghai, China"):
        v = match(_p("Software Engineer, New Grad", loc), criteria)
        assert v.decision is Decision.MATCH, f"{loc} -> {v.decision} ({v.reason})"
        assert "location" not in v.reason


@pytest.mark.parametrize(
    "location,expected",
    [
        ("New York, NY", 0),
        ("Remote / New York, NY (HQ)", 0),
        ("Brooklyn, NY", 0),
        ("Seattle, WA, US; San Francisco, CA, US", 1),
        ("Remote - US", 1),
        ("Wisconsin (relocation)", 1),
        ("Remote", 2),
        ("", 2),
        (None, 2),
        ("London, UK", 3),
        ("Remote Spain", 3),
        ("Brno, Czech Republic", 3),
    ],
)
def test_location_rank(criteria, location, expected):
    assert location_rank(location, criteria) == expected


def test_country_name_beats_ambiguous_state_code(criteria):
    """'CA' is both California and Canada, so non-US must be checked before US."""
    assert location_rank("Toronto, Canada", criteria) == 3
    assert location_rank("Toronto, ON", criteria) == 3
    assert location_rank("Palo Alto, CA", criteria) == 1


def test_unknown_outranks_explicit_non_us(criteria):
    """A bare 'Remote' is likelier to be US-eligible than one that names Bengaluru."""
    assert location_rank("Remote", criteria) < location_rank("Bengaluru, India", criteria)
