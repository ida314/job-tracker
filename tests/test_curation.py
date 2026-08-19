"""The companies.yaml writer.

No sockets and no server: `curation` is pure text-and-dataclasses in, text out, which is
what lets `cli` and `server` share one appender. The property under test throughout is
that adding an entry touches nothing else — a PyYAML round trip re-folds `notes:` prose
on unrelated companies, and that is precisely the diff noise this module exists to avoid.
"""

from __future__ import annotations

import collections

import pytest
import yaml

from jobtracker import config, curation
from jobtracker.migrate import _HEADER
from jobtracker.models import Company

_YAML = """- name: Stripe
  ats: greenhouse
  slug: stripe
  tier: 1
  category: backend-scaleup
  check_method: api
  careers_page: https://stripe.com/jobs/search
  notes: A deliberately long note, long enough that PyYAML would fold it across more than
    one line if it were ever re-dumped, which is the whole point of this fixture.
  expected_board_name: Stripe
- name: Elastic
  ats: greenhouse
  slug: elastic
  tier: 2
  check_method: api
  expected_board_name: Elastic
- name: Bloomberg
  ats: bespoke
  tier: 3
  check_method: manual
  expected_board_name: null
- name: Some Feed
  ats: aggregator
  check_method: aggregator
  board_url: https://example.invalid/README.md
  expected_board_name: null
"""


@pytest.fixture
def text():
    return _HEADER + _YAML


def _company(**kw):
    kw.setdefault("slug", "")
    return Company(**kw)


def _names(doc: str):
    return [e["name"] for e in yaml.safe_load(doc)]


# -- the anti-reflow property --------------------------------------------------------
def test_appending_touches_no_other_line(text):
    """The reason this module exists. Every pre-existing line must survive byte for byte;
    a round trip through safe_dump re-wraps Stripe's notes and shows up in the diff."""
    after = curation.insert_entry(
        text, _company(name="New Co", ats="ashby", slug="newco", tier=2,
                       check_method="api")
    )
    before_lines = text.splitlines()
    after_lines = after.splitlines()
    assert not (collections.Counter(before_lines) - collections.Counter(after_lines))
    assert len(after_lines) == len(before_lines) + 6


def test_the_header_survives_an_append(text):
    after = curation.insert_entry(text, _company(name="X", ats="bespoke", tier=1))
    assert after.startswith(_HEADER)


# -- placement -----------------------------------------------------------------------
def test_an_entry_lands_at_the_end_of_its_own_tier(text):
    after = curation.insert_entry(
        text, _company(name="New Two", ats="ashby", slug="n", tier=2, check_method="api")
    )
    assert _names(after) == ["Stripe", "Elastic", "New Two", "Bloomberg", "Some Feed"]


def test_an_entry_whose_tier_is_new_still_sorts_into_place(text):
    """The trap in the obvious rule. "After the last entry with my tier, else end of
    file" has no last entry for a tier the file does not use yet — so a tier-4 company
    would fall past every tiered entry and land beneath the untiered aggregator feeds.
    The fixture has tiers 1, 2 and 3, so tier 4 exercises exactly that path."""
    after = curation.insert_entry(text, _company(name="Tier Four", ats="bespoke", tier=4))
    assert _names(after) == ["Stripe", "Elastic", "Bloomberg", "Tier Four", "Some Feed"]


def test_an_entry_joining_an_existing_tier_lands_at_the_end_of_it(text):
    after = curation.insert_entry(text, _company(name="Also Three", ats="bespoke", tier=3))
    assert _names(after) == ["Stripe", "Elastic", "Bloomberg", "Also Three", "Some Feed"]


def test_an_untiered_entry_lands_after_every_tiered_one(text):
    after = curation.insert_entry(
        text, _company(name="Another Feed", ats="aggregator",
                       check_method="aggregator", board_url="https://x.invalid/R.md")
    )
    assert _names(after)[-1] == "Another Feed"


def test_a_tier_below_everything_lands_first(text):
    after = curation.insert_entry(text, _company(name="Zero", ats="bespoke", tier=1))
    assert _names(after)[1] == "Zero"


# -- what gets written ---------------------------------------------------------------
def test_the_entry_keeps_the_canonical_field_order():
    block = curation.render_entry(
        _company(name="X", ats="greenhouse", slug="x", tier=2, category="c",
                 check_method="api", careers_page="https://x.invalid/",
                 notes="n", expected_board_name="X")
    )
    keys = [ln.split(":")[0].strip("- ") for ln in block.splitlines() if not ln.startswith("    ")]
    assert keys == ["name", "ats", "slug", "tier", "category", "check_method",
                    "careers_page", "notes", "expected_board_name"]


def test_an_aggregator_carries_board_url_and_no_empty_slug():
    """An aggregator has no slug, tier or careers_page — writing them as empty strings
    would put four meaningless keys on every feed. Only truthy values are emitted."""
    block = curation.render_entry(
        _company(name="Feed", ats="aggregator", check_method="aggregator",
                 board_url="https://x.invalid/README.md")
    )
    assert "board_url:" in block
    for absent in ("slug:", "tier:", "careers_page:", "category:", "notes:"):
        assert absent not in block, absent


def test_expected_board_name_is_written_as_null_never_omitted():
    """All 100 live entries carry the key. Omitting it when unset would make "nobody has
    verified this board" a missing key rather than a visible state."""
    block = curation.render_entry(_company(name="X", ats="bespoke"))
    assert "expected_board_name: null" in block


def test_appending_to_a_header_only_file_produces_a_valid_document():
    """`add-company` used to raise TypeError here — safe_load returns None for a file
    holding only comments, and `any(... for e in None)` blows up. This is the one case
    where you most want the writer to work."""
    after = curation.insert_entry(_HEADER, _company(name="First", ats="bespoke", tier=1))
    assert yaml.safe_load(after) == [
        {"name": "First", "ats": "bespoke", "tier": 1,
         "check_method": "manual", "expected_board_name": None}
    ]


def test_a_file_with_no_trailing_newline_does_not_glue_the_block_on(text):
    after = curation.insert_entry(text.rstrip("\n"), _company(name="X", ats="bespoke"))
    assert "\n- name: X\n" in after
    assert len(yaml.safe_load(after)) == 5


def test_the_diff_comes_from_the_text_that_was_written(text):
    after = curation.insert_entry(text, _company(name="New", ats="bespoke", tier=2))
    d = curation.diff("companies.yaml", text, after)
    assert "+- name: New" in d
    # Nothing else moved, so the diff has exactly one hunk.
    assert d.count("@@") == 2


# -- validation ----------------------------------------------------------------------
def _existing():
    return config.load_companies()


def test_every_entry_in_the_real_companies_yaml_passes_validation():
    """The rule that keeps the rules honest. `validate_new` is deliberately stricter than
    `config.load_companies`, which has to keep loading whatever is on disk — but stricter
    than the live file means a rule that will be deleted the first time it fires. Two
    were already wrong when this was written: `slug` on a manual Workday entry is a
    tenant triple a human reads, and the unconfirmed aggregator feed has no board_url on
    purpose."""
    companies = _existing()
    for i, c in enumerate(companies):
        assert curation.validate_new(c, companies[:i] + companies[i + 1:]) == [], c.name


@pytest.mark.parametrize("kw, fragment", [
    (dict(name="", ats="greenhouse"), "name is required"),
    (dict(name="X\nY", ats="greenhouse"), "line break"),
    (dict(name="X", ats=""), "ats is required"),
    (dict(name="X", ats="banana"), "ats must be one of"),
    (dict(name="X", ats="greenhouse", check_method="sometimes"), "check_method must be"),
    (dict(name="X", ats="workday", slug="x", check_method="api"), "needs an ats with an adapter"),
    (dict(name="X", ats="greenhouse", check_method="api"), "needs a slug"),
    (dict(name="X", ats="greenhouse", slug="a/b", check_method="api"), "not a URL"),
    (dict(name="X", ats="bespoke", tier=9), "tier must be"),
    (dict(name="X", ats="bespoke", careers_page="javascript:alert(1)"), "http(s) URL"),
    (dict(name="X", ats="bespoke", board_url="ftp://x.invalid"), "http(s) URL"),
    (dict(name="X", ats="bespoke", notes="n" * 5000), "longer than"),
])
def test_incoherent_entries_are_rejected(kw, fragment):
    errors = curation.validate_new(_company(**kw), [])
    assert any(fragment in e for e in errors), errors


def test_a_slug_on_a_manual_entry_is_documentation_not_an_identifier():
    """Red Hat carries `redhat / wd5 / jobs` and Nvidia a similar Workday triple. The
    shape rule is about board identifiers, so it applies to `api` entries only."""
    assert curation.validate_new(
        _company(name="Red Hat 2", ats="workday", slug="redhat / wd5 / jobs"), []
    ) == []


def test_a_duplicate_name_is_rejected_and_so_is_one_differing_only_in_case():
    existing = [_company(name="Stripe", ats="greenhouse", slug="stripe")]
    assert "already tracked" in curation.validate_new(
        _company(name="Stripe", ats="ashby", slug="s2"), existing)[0]
    assert "collides" in curation.validate_new(
        _company(name="  stripe ", ats="ashby", slug="s2"), existing)[0]


def test_a_duplicate_ats_slug_pair_is_rejected():
    """Two names on one board split its postings across two diff namespaces — every
    posting is keyed by (company, ats_job_id), so the same req would be stored twice and
    close twice."""
    existing = [_company(name="Stripe", ats="greenhouse", slug="stripe")]
    errors = curation.validate_new(
        _company(name="Stripe Payments", ats="greenhouse", slug="stripe"), existing)
    assert any("already tracked as 'Stripe'" in e for e in errors), errors


def test_an_aggregator_without_a_board_url_is_allowed():
    """Not an oversight. A feed with no URL is skipped rather than fetched, and the
    unconfirmed Ouckah/CVrve entry is parked in exactly that state on purpose."""
    assert curation.validate_new(
        _company(name="Unwired Feed", ats="aggregator", check_method="aggregator"), []
    ) == []
