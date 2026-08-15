"""Slug repair: extraction, the verification gate, detection, and the orchestrator.

No network anywhere in this file. `repair.py` is pure and `repair_boards` takes its
page reader and its verifier as arguments, so the entire decision path — including the
model fallback and every rejection — runs against recorded HTML and recorded
FetchResults.

The tests that matter most are the rejections. This subsystem's whole reason to exist
is that a slug which *looks* right is not a slug that *is* right, so the two failure
modes CLAUDE.md names — `ashby/cedar` and `greenhouse/hubspot` — get named tests, and
they must stay red-on-regression forever.
"""

import pytest

from jobtracker import repair
from jobtracker.health import REPAIR_FAILURE_THRESHOLD, evaluate
from jobtracker.models import (
    BoardHealth,
    Company,
    FetchResult,
    HealthStatus,
    Posting,
)


def _company(**kw):
    base = dict(
        name="Acme",
        ats="greenhouse",
        slug="acme",
        check_method="api",
        careers_page="https://acme.example/careers",
    )
    base.update(kw)
    return Company(**base)


def _result(n=3, name="Acme", ok=True, error=None, ats="greenhouse", slug="acme2"):
    postings = [
        Posting("Acme", str(i), f"Software Engineer {i}", "u") for i in range(n)
    ]
    return FetchResult(
        "Acme", ats, slug, ok=ok, status_code=200 if ok else 500,
        observed_board_name=name, postings=postings, error=error,
    )


# -- extraction ----------------------------------------------------------------------
def test_greenhouse_embed_script():
    html = (
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=hubspotjobs">'
        "</script>"
    )
    got = repair.extract_candidates(html)
    assert (got[0].ats, got[0].slug) == ("greenhouse", "hubspotjobs")


def test_greenhouse_job_boards_link():
    got = repair.extract_candidates('<a href="https://job-boards.greenhouse.io/acme">Jobs</a>')
    assert ("greenhouse", "acme") in [(c.ats, c.slug) for c in got]


def test_html_entities_are_unescaped_before_matching():
    """`job_board?for=acme&amp;b=123` is the ordinary on-page spelling.

    A pattern reading against the raw text stops inside `&amp;` and captures nothing,
    or worse captures `acme&amp;b=123`.
    """
    got = repair.extract_candidates(
        '<script src="https://boards.greenhouse.io/embed/job_board?b=123&amp;for=acme">'
    )
    assert [(c.ats, c.slug) for c in got] == [("greenhouse", "acme")]


def test_lever_slug_case_is_preserved():
    """CLAUDE.md: `lever/Onehouse` resolves and `lever/onehouse` 404s.

    Lever slugs are case-sensitive. Normalizing here would produce a candidate that
    cannot possibly verify, on a board that genuinely exists.
    """
    got = repair.extract_candidates('<a href="https://jobs.lever.co/Onehouse">Careers</a>')
    assert [(c.ats, c.slug) for c in got] == [("lever", "Onehouse")]


def test_ashby_takes_the_first_path_segment_only():
    got = repair.extract_candidates(
        '<a href="https://jobs.ashbyhq.com/ramp/8f3c-1d2e-uuid">A role</a>'
    )
    assert [(c.ats, c.slug) for c in got] == [("ashby", "ramp")]


@pytest.mark.parametrize(
    "html",
    [
        '<a href="https://boards.greenhouse.io/embed">x</a>',
        '<a href="https://api.ashbyhq.com/posting-api/job-board">x</a>',
        '<a href="https://jobs.lever.co/">x</a>',
        '<script src="https://boards.greenhouse.io/embed/job_board/js"></script>',
    ],
)
def test_url_structure_is_never_mistaken_for_a_slug(html):
    """A board "repaired" to `embed` looks like a fix until the next fetch."""
    assert repair.extract_candidates(html) == []


def test_duplicates_collapse_and_order_is_stable():
    html = (
        '<a href="https://boards.greenhouse.io/acme">1</a>'
        '<a href="https://job-boards.greenhouse.io/acme">2</a>'
        '<a href="https://jobs.ashbyhq.com/acme-labs">3</a>'
    )
    got = [(c.ats, c.slug) for c in repair.extract_candidates(html)]
    assert got == [("greenhouse", "acme"), ("ashby", "acme-labs")]


@pytest.mark.parametrize("html", ["", "<html><body>We are hiring!</body></html>", "{}"])
def test_a_page_with_no_board_link_yields_nothing(html):
    """Not an error — this is exactly the case the model fallback exists for."""
    assert repair.extract_candidates(html) == []


# -- condense / grounding ------------------------------------------------------------
def test_condense_keeps_links_and_drops_script_bodies():
    html = (
        '<script>var big = "'
        + "x" * 500
        + '";</script><a href="https://jobs.lever.co/acme">Careers</a><p>Join us</p>'
    )
    text = repair.condense_page(html)
    assert "https://jobs.lever.co/acme" in text
    assert "var big" not in text
    assert "Join us" in text


def test_a_slug_absent_from_the_page_is_not_grounded():
    """The anti-hallucination gate: a model that infers a slug from the company name.

    This is the single most likely way an obliging model answers a page with no board
    on it, and it is precisely the guess CLAUDE.md forbids. Note that the company name
    IS on the page — in its own domain — which is why a bare substring test would let
    the invention through.
    """
    page = "link: https://acme.example/jobs\nWe are hiring at Acme"
    assert not repair.candidate_is_grounded(repair.Candidate("greenhouse", "acme", "llm"), page)
    assert repair.candidate_is_grounded(
        repair.Candidate("greenhouse", "acme", "llm"),
        page + "\nlink: https://boards.greenhouse.io/acme",
    )


def test_a_slug_that_is_only_the_prefix_of_a_longer_one_is_not_grounded():
    page = "link: https://boards.greenhouse.io/acmecorp"
    assert not repair.candidate_is_grounded(repair.Candidate("greenhouse", "acme", "llm"), page)
    assert repair.candidate_is_grounded(
        repair.Candidate("greenhouse", "acmecorp", "llm"), page
    )


def test_grounding_is_case_sensitive():
    page = "link: https://jobs.lever.co/Onehouse"
    assert repair.candidate_is_grounded(repair.Candidate("lever", "Onehouse", "llm"), page)
    assert not repair.candidate_is_grounded(repair.Candidate("lever", "onehouse", "llm"), page)


# -- verification: the two named failure modes ---------------------------------------
def test_hubspot_a_real_but_dead_board_is_rejected():
    """A 200 with zero jobs is not "no openings" and is certainly not a repair.

    `greenhouse/hubspot` is a real board named "HubSpot Product" that always returns
    an empty array; the live board is `hubspotjobs`. Adopting the dead one would swap
    a loud FETCH_FAILED for a quiet SUSPECT_EMPTY — a visible break for an invisible.
    """
    company = _company(name="HubSpot", slug="hubspot-old", expected_board_name="HubSpot")
    candidate = repair.Candidate("greenhouse", "hubspot", "regex")
    result = _result(n=0, name="HubSpot Product")
    verification = repair.judge_candidate(company, candidate, result)

    assert verification.accepted is False
    assert verification.reason == "zero_jobs"
    assert repair.build_proposal(company, candidate, verification, "fetch_failed") is None


def test_cedar_a_live_board_belonging_to_someone_else_is_rejected():
    """`ashby/cedar` returns real postings for an unrelated real-estate Cedar.

    Status codes cannot catch this and neither can a job count. Only the identity
    assertion can, which is why a Greenhouse candidate is checked against a board name
    that comes from a different endpoint than the slug.
    """
    company = _company(name="Cedar", slug="cedar-old", expected_board_name="Cedar Health")
    candidate = repair.Candidate("greenhouse", "cedar", "regex")
    result = _result(n=5, name="Cedar Real Estate Group")
    verification = repair.judge_candidate(company, candidate, result)

    assert verification.accepted is False
    assert verification.reason == "wrong_company"
    assert repair.build_proposal(company, candidate, verification, "identity_drift") is None


def test_a_missing_board_name_is_never_read_as_agreement():
    """`identity_matches` returns True when either side is empty.

    Right for the nightly loop — "don't cry drift on missing data" — and catastrophic
    here, where it would turn "the identity endpoint 500'd" into "verified". The
    emptiness check must come first.
    """
    company = _company(name="Acme", slug="acme-old", expected_board_name="Acme")
    candidate = repair.Candidate("greenhouse", "acme", "regex")
    result = _result(n=500, name=None)
    verification = repair.judge_candidate(company, candidate, result)

    assert verification.accepted is False
    assert verification.reason == "no_identity"


def test_ashby_self_reference_is_provenance_not_identity():
    """Ashby/Lever identity is read back out of the job URL, so it restates the slug.

    Comparing them proves nothing. The claim is accepted on provenance — the link came
    off the company's own careers page — and is *labelled* that way rather than dressed
    up as an identity match.
    """
    company = _company(name="Ramp", ats="ashby", slug="ramp-old", expected_board_name="ramp-old")
    candidate = repair.Candidate("ashby", "ramp", "regex")
    result = _result(n=12, name="ramp", ats="ashby", slug="ramp")
    verification = repair.judge_candidate(company, candidate, result)

    assert verification.accepted is True
    assert verification.evidence_kind == "provenance"

    proposal = repair.build_proposal(company, candidate, verification, "fetch_failed")
    assert proposal.evidence_kind == "provenance"
    # And the human reviewing it is told, every time.
    text = repair.render([proposal], [], repair.RepairStats())
    assert "PROVENANCE only" in text


def test_a_greenhouse_match_is_identity_evidence():
    company = _company(name="HubSpot", slug="hubspot", expected_board_name="HubSpot Product")
    candidate = repair.Candidate("greenhouse", "hubspotjobs", "regex")
    result = _result(n=214, name="HubSpot")
    verification = repair.judge_candidate(company, candidate, result)

    assert verification.accepted is True
    assert verification.evidence_kind == "identity"
    assert verification.job_count == 214
    assert len(verification.sample_titles) == 3

    proposal = repair.build_proposal(company, candidate, verification, "suspect_empty")
    assert (proposal.from_ats, proposal.from_slug) == ("greenhouse", "hubspot")
    assert (proposal.to_ats, proposal.to_slug) == ("greenhouse", "hubspotjobs")
    assert proposal.board_name == "HubSpot"


def test_an_unreachable_candidate_is_rejected():
    company = _company(slug="acme-old")
    verification = repair.judge_candidate(
        company,
        repair.Candidate("greenhouse", "acme", "regex"),
        _result(n=0, ok=False, error="HTTP 404"),
    )
    assert (verification.accepted, verification.reason) == (False, "unreachable")


def test_the_slug_we_already_have_is_reported_as_unchanged():
    """The page still advertises this board: it did not move, it is broken.

    A distinct and useful finding, so it is not folded into "rejected".
    """
    company = _company(slug="acme")
    verification = repair.judge_candidate(
        company, repair.Candidate("greenhouse", "acme", "regex"), _result(n=9)
    )
    assert (verification.accepted, verification.reason) == (False, "unchanged")


# -- detection -----------------------------------------------------------------------
def _detect_one(company, health):
    return repair.detect([company], [health])


def test_identity_drift_qualifies_on_the_first_observation():
    targets, _ = _detect_one(_company(), BoardHealth("Acme", HealthStatus.IDENTITY_DRIFT))
    assert [t.company.name for t in targets] == ["Acme"]
    assert targets[0].trigger == "identity_drift"


@pytest.mark.parametrize(
    "failures,expected", [(0, False), (1, False), (REPAIR_FAILURE_THRESHOLD, True)]
)
def test_fetch_failed_needs_persistence(failures, expected):
    """One bad night is a network. Rewriting a hand-verified slug over it is not."""
    health = BoardHealth("Acme", HealthStatus.FETCH_FAILED, consecutive_failures=failures)
    targets, _ = _detect_one(_company(), health)
    assert bool(targets) is expected


def test_a_permanently_empty_board_is_never_repaired():
    """The dbt Labs / Root Insurance regression.

    Correct slugs, genuinely zero reqs, SUSPECT_EMPTY every night forever. "Repairing"
    them would corrupt hand-verified data to fix nothing.
    """
    health = evaluate(
        _company(name="dbt Labs"),
        FetchResult("dbt Labs", "greenhouse", "dbtlabsinc", ok=True, status_code=200,
                    observed_board_name="dbt Labs"),
        None, "2026-08-03", ever_nonempty=False,
    )
    assert health.status is HealthStatus.SUSPECT_EMPTY and health.alerting is False
    targets, _ = _detect_one(_company(name="dbt Labs"), health)
    assert targets == []


def test_a_board_that_emptied_after_being_populated_does_qualify():
    """Mercury and Vercel both left a live-but-dead board behind when they migrated.

    A dead board never presents as FETCH_FAILED — it answers 200 with an empty array
    forever — so excluding this trigger would put the canonical case out of reach.
    """
    health = BoardHealth("Acme", HealthStatus.SUSPECT_EMPTY, consecutive_empty_runs=2,
                         alerting=True)
    targets, _ = _detect_one(_company(), health)
    assert [t.trigger for t in targets] == ["suspect_empty"]


@pytest.mark.parametrize("method", ["manual", "aggregator"])
def test_never_scraped_companies_are_never_targets(method):
    """CLAUDE.md's standing rule. A careers page is a scrape, and an aggregator feed
    has no slug to repair in the first place."""
    company = _company(check_method=method)
    targets, outcomes = _detect_one(company, BoardHealth("Acme", HealthStatus.IDENTITY_DRIFT))
    assert targets == [] and outcomes == []


def test_a_missing_careers_page_is_reported_not_dropped():
    company = _company(careers_page="")
    targets, outcomes = _detect_one(company, BoardHealth("Acme", HealthStatus.IDENTITY_DRIFT))
    assert targets == []
    assert [o.reason for o in outcomes] == ["no_careers_page"]


def test_a_healthy_board_is_not_a_target():
    targets, outcomes = _detect_one(_company(), BoardHealth("Acme", HealthStatus.OK))
    assert targets == [] and outcomes == []


# -- the orchestrator ----------------------------------------------------------------
_PAGE = '<a href="https://job-boards.greenhouse.io/acmecorp">Open roles</a>'


class _Client:
    """A stub local model. Counts calls so "was it asked?" is directly assertable."""

    def __init__(self, guess=None):
        self.guess = guess
        self.calls = 0

    def find_board(self, company_name, careers_url, page_text):
        self.calls += 1
        return self.guess


def _targets(company=None, trigger="fetch_failed"):
    company = company or _company()
    return [repair.RepairTarget(company, BoardHealth(company.name,
                                                     HealthStatus.FETCH_FAILED), trigger)]


def test_a_verified_regex_candidate_becomes_a_proposal():
    calls = []

    def verify(company, candidate):
        calls.append((candidate.ats, candidate.slug))
        return _result(n=40, name="Acme")

    proposals, outcomes, stats = repair.repair_boards(
        _targets(), lambda url: (_PAGE, None), verify
    )
    assert [(p.to_ats, p.to_slug) for p in proposals] == [("greenhouse", "acmecorp")]
    assert calls == [("greenhouse", "acmecorp")]
    assert outcomes == [] and stats.proposed == 1


def test_the_model_is_not_consulted_when_a_regex_candidate_verifies():
    """The model is the fallback, not the mechanism. Spending a call on a page a regex
    already answered is the cost this ordering exists to avoid."""
    client = _Client(guess=object())
    proposals, _, stats = repair.repair_boards(
        _targets(), lambda url: (_PAGE, None), lambda c, k: _result(n=40), client=client
    )
    assert len(proposals) == 1
    assert client.calls == 0 and stats.asked_model == 0


def test_a_model_candidate_passes_through_the_same_gate():
    """The page the regexes cannot parse: the board URL is in a JS config object,
    behind a CDN host no pattern here knows. That is the case the model exists for —
    and its answer is still only a candidate."""

    class _Guess:
        ats, slug, evidence = "lever", "acmeco", "https://cdn.example/board/acmeco"

    page = (
        '<script>window.__CFG__={"jobs":"https://cdn.example/board/acmeco"};</script>'
        "<p>Careers at Acme</p>"
    )
    assert repair.extract_candidates(page) == []  # the regexes genuinely cannot
    client = _Client(guess=_Guess())
    proposals, _, stats = repair.repair_boards(
        _targets(),
        lambda url: (page, None),
        lambda c, k: _result(n=7, name="acmeco", ats="lever", slug="acmeco"),
        client=client,
    )
    assert [(p.to_ats, p.to_slug, p.found_by) for p in proposals] == [
        ("lever", "acmeco", "llm")
    ]
    assert client.calls == 1 and stats.asked_model == 1


def test_a_model_candidate_that_fails_verification_produces_nothing():
    class _Guess:
        ats, slug, evidence = "greenhouse", "cedar", "x"

    page = "link: https://boards.greenhouse.io/cedar"
    proposals, outcomes, _ = repair.repair_boards(
        _targets(_company(name="Cedar", expected_board_name="Cedar Health")),
        lambda url: (page, None),
        # A live board — belonging to somebody else.
        lambda c, k: _result(n=5, name="Cedar Real Estate"),
        client=_Client(guess=_Guess()),
    )
    assert proposals == []
    assert outcomes[0].reason == "all_rejected"


def test_an_ungrounded_model_slug_is_discarded_before_any_fetch():
    class _Guess:
        ats, slug, evidence = "greenhouse", "acme", "invented"

    verified = []
    proposals, outcomes, _ = repair.repair_boards(
        _targets(),
        lambda url: ("We are hiring. No board here.", None),
        lambda c, k: verified.append(k) or _result(),
        client=_Client(guess=_Guess()),
    )
    assert proposals == [] and verified == []
    assert outcomes[0].reason == "no_candidates"


def test_an_unreadable_careers_page_is_reported_and_costs_no_model_call():
    client = _Client(guess=object())
    proposals, outcomes, _ = repair.repair_boards(
        _targets(), lambda url: (None, "HTTP 404"), lambda c, k: _result(), client=client
    )
    assert proposals == []
    assert (outcomes[0].reason, outcomes[0].detail) == ("page_unreadable", "HTTP 404")
    assert client.calls == 0


def test_a_page_advertising_the_current_board_reports_unchanged():
    page = '<a href="https://job-boards.greenhouse.io/acme">Open roles</a>'
    proposals, outcomes, _ = repair.repair_boards(
        _targets(), lambda url: (page, None), lambda c, k: _result(n=5)
    )
    assert proposals == []
    assert outcomes[0].reason == "unchanged"


def test_no_model_configured_still_runs_the_regex_path():
    proposals, outcomes, stats = repair.repair_boards(
        _targets(), lambda url: (_PAGE, None), lambda c, k: _result(n=3), client=None
    )
    assert len(proposals) == 1 and stats.asked_model == 0
    assert outcomes == []


def test_render_shows_the_evidence_a_reviewer_needs():
    company = _company(name="HubSpot", slug="hubspot", expected_board_name="HubSpot Product")
    candidate = repair.Candidate(
        "greenhouse", "hubspotjobs", "regex", "gh_embed",
        "boards.greenhouse.io/embed/job_board/js?for=hubspotjobs",
    )
    verification = repair.judge_candidate(company, candidate, _result(n=214, name="HubSpot"))
    text = repair.render(
        [repair.build_proposal(company, candidate, verification, "suspect_empty")],
        [repair.Outcome("Acme", "fetch_failed", "no_candidates", "nothing on the page")],
        repair.RepairStats(targets=2, proposed=1),
    )
    assert "greenhouse/hubspot  ->  greenhouse/hubspotjobs" in text
    assert "214 jobs" in text
    assert "for=hubspotjobs" in text  # the evidence URL
    assert "Software Engineer 0" in text  # sample titles
    assert "no_candidates" in text  # the honest half


# -- the companies.yaml writer -------------------------------------------------------
# Applying a repair is the only path in this subsystem that touches curated data, so
# it is the one that must not lose anything.
_YAML = """- name: HubSpot
  ats: greenhouse
  slug: hubspot
  tier: 3
  check_method: api
  careers_page: https://www.hubspot.com/careers
  notes: Big board; new-grad roles open in the fall.
  expected_board_name: HubSpot Product
- name: Stripe
  ats: greenhouse
  slug: stripe
  tier: 1
  check_method: api
  careers_page: https://stripe.com/jobs/search
  expected_board_name: Stripe
"""


@pytest.fixture
def companies_file(tmp_path):
    from jobtracker.migrate import _HEADER

    path = tmp_path / "companies.yaml"
    path.write_text(_HEADER + _YAML)
    return path


def test_the_writer_changes_only_the_targeted_entry(companies_file):
    from jobtracker import config
    from jobtracker.cli import _rewrite_companies
    from jobtracker.migrate import _HEADER

    _rewrite_companies(
        companies_file,
        {"HubSpot": {"slug": "hubspotjobs", "expected_board_name": "HubSpot"}},
    )
    text = companies_file.read_text()
    assert text.startswith(_HEADER)  # the four header lines survive the round-trip

    loaded = {c.name: c for c in config.load_companies(companies_file)}
    assert (loaded["HubSpot"].slug, loaded["HubSpot"].expected_board_name) == (
        "hubspotjobs", "HubSpot",
    )
    # Untouched: the other entry, and this entry's hand-written prose.
    assert loaded["Stripe"].slug == "stripe"
    assert "new-grad roles open in the fall" in loaded["HubSpot"].notes


def test_the_diff_comes_from_the_same_renderer_that_writes(companies_file):
    """A diff generated by a different code path than the write is a diff of something
    else. Reviewing it would be theatre."""
    from jobtracker.cli import _companies_diff, _rendered_companies

    updates = {"HubSpot": {"slug": "hubspotjobs"}}
    diff = _companies_diff(companies_file, updates)
    assert "-  slug: hubspot\n" in diff and "+  slug: hubspotjobs\n" in diff

    before = companies_file.read_text()
    assert _rendered_companies(companies_file, updates) != before
    assert companies_file.read_text() == before  # rendering is not writing


def test_inline_comments_block_a_write(companies_file):
    """Every writer here round-trips through PyYAML, which discards comments. Today
    that is harmless; this is what stops a repair silently eating a note someone
    added tomorrow."""
    from jobtracker.cli import _has_inline_comments

    assert _has_inline_comments(companies_file) is False
    companies_file.write_text(
        companies_file.read_text().replace(
            "- name: Stripe", "# verified by hand 2026-07-09\n- name: Stripe"
        )
    )
    assert _has_inline_comments(companies_file) is True


def test_the_write_touches_only_the_lines_it_changes(companies_file):
    """The reviewable-diff property, and the reason the writer is line-oriented.

    Round-tripping through PyYAML re-folds every long string to its own width, so
    changing one `slug:` also re-wraps hand-written `notes:` prose on unrelated
    entries. Measured on the real file, a one-line repair produced a diff touching ten
    other companies — and a diff you have to search for the change in is one nobody
    reads.
    """
    from jobtracker.cli import _rewrite_companies

    before = companies_file.read_text().splitlines()
    _rewrite_companies(companies_file, {"HubSpot": {"slug": "hubspotjobs"}})
    after = companies_file.read_text().splitlines()

    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(before) == len(after)
    assert changed == [("  slug: hubspot", "  slug: hubspotjobs")]


def test_a_missing_field_is_inserted_rather_than_lost(companies_file):
    from jobtracker import config
    from jobtracker.cli import _rewrite_companies

    companies_file.write_text(
        companies_file.read_text().replace("  expected_board_name: Stripe\n", "")
    )
    _rewrite_companies(companies_file, {"Stripe": {"expected_board_name": "Stripe"}})
    loaded = {c.name: c for c in config.load_companies(companies_file)}
    assert loaded["Stripe"].expected_board_name == "Stripe"
    assert loaded["HubSpot"].expected_board_name == "HubSpot Product"  # untouched


def test_clearing_a_field_writes_null_not_a_deletion(companies_file):
    from jobtracker import config
    from jobtracker.cli import _rewrite_companies

    _rewrite_companies(companies_file, {"HubSpot": {"expected_board_name": None}})
    assert "  expected_board_name: null\n" in companies_file.read_text()
    loaded = {c.name: c for c in config.load_companies(companies_file)}
    assert loaded["HubSpot"].expected_board_name is None
