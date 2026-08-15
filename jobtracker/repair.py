"""Slug repair: recover a board identifier from a company's careers page.

DESIGN.md §8 gave the model three bounded roles. This is the last one, and the only
one where the model is a *fallback* rather than the mechanism. Boards move — Mercury
and Vercel migrated Ashby → Greenhouse, HubSpot's live board is `hubspotjobs` and not
`hubspot` — and today `health.py` detects that and stops. Someone then opens the careers
page by hand, finds the new identifier, and edits `companies.yaml`. That was fifteen
entries of manual work in the 2026-07-09 audit.

The flow, per broken board:

    board_health says IDENTITY_DRIFT / persistent FETCH_FAILED / alerting SUSPECT_EMPTY
        -> read company.careers_page                      (one page, under the limiter)
        -> extract_candidates(html)                       (deterministic regex)
        -> [fallback, only if that found nothing] local model reads the page
        -> verify EVERY candidate with a real API fetch   (existing Source adapters)
        -> propose (ats, slug, expected_board_name) for a human to review

Two properties hold the whole thing up.

**Nothing is proposed on the strength of a string.** A candidate is a guess until an
actual fetch of that board comes back with postings and an identity that checks out.
This is CLAUDE.md's "slugs are verified, never guessed" written as code, and the two
failure modes it names are rejections here: `zero_jobs` is the `greenhouse/hubspot`
case (a real board that is permanently empty), `wrong_company` is `ashby/cedar` (a live
board belonging to someone else entirely).

**The model reads; verification decides.** A model candidate is a `Candidate` like any
other and goes through the identical gate. It cannot propose anything a regex hit could
not have proposed — it can only find one on a page the regexes could not parse. Every
failure path leaves the board exactly as broken as it was, which is the §7.3 principle
applied a third time.

This module is pure: no sockets, no database. `repair_boards` takes the page reader and
the verifier as arguments, so the whole flow is exercised against recorded HTML and
recorded FetchResults with no HTTP mocking — the same shape as `resolve_postings` and
`judge_matches`.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

from .health import REPAIR_FAILURE_THRESHOLD, identity_matches, needs_repair
from .models import BoardHealth, Company, FetchResult
from .sources import get_source

log = logging.getLogger("jobtracker.repair")

# Candidates probed per board per run. Each one costs a real API fetch against a
# rate-limited ATS host, and a careers page listing more than a handful of distinct
# board slugs is a page we have misread rather than a board we are about to find.
MAX_CANDIDATES_PER_BOARD = 6

# Page text handed to the model. Careers pages carry navigation, footers and cookie
# banners; the board link is near the top of the content if it is anywhere.
MAX_PAGE_CHARS = 12000


# -- types ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    """A board identifier someone might have moved to. Not yet believed."""

    ats: str  # greenhouse | lever | ashby
    slug: str  # verbatim, case preserved
    found_by: str = "regex"  # regex | llm
    pattern: str = ""  # which rule produced it, for the log
    evidence: str = ""  # the URL it was read out of, shown to the reviewer


@dataclass(frozen=True)
class Verification:
    """The verdict on one candidate, given a real fetch of that board."""

    accepted: bool
    reason: str  # ok | unchanged | unreachable | zero_jobs | no_identity | wrong_company
    evidence_kind: str = ""  # identity | provenance — see judge_candidate
    job_count: int = 0
    board_name: str = ""
    sample_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairProposal:
    """A verified claim that a board moved, awaiting a human."""

    company: str
    from_ats: str
    from_slug: str
    to_ats: str
    to_slug: str
    board_name: str
    job_count: int
    sample_titles: tuple[str, ...]
    evidence_kind: str
    found_by: str
    evidence: str
    trigger: str


@dataclass(frozen=True)
class RepairTarget:
    company: Company
    health: BoardHealth
    trigger: str  # the health status that qualified it


@dataclass(frozen=True)
class Outcome:
    """A detected board that produced no proposal, and why. The honest half."""

    company: str
    trigger: str
    reason: str  # no_careers_page | page_unreadable | no_candidates
    #              | all_rejected | unchanged
    detail: str = ""


@dataclass
class RepairStats:
    targets: int = 0
    proposed: int = 0
    candidates_tried: int = 0
    rejected: int = 0
    asked_model: int = 0
    no_candidate: int = 0

    def summary(self) -> str:
        return (
            f"{self.targets} board(s) detected · {self.proposed} proposed · "
            f"{self.candidates_tried} candidate(s) verified "
            f"({self.rejected} rejected) · model asked {self.asked_model}×"
        )


# -- deterministic extraction --------------------------------------------------------
# Ordered most-specific first. Two rules govern every pattern here:
#
#   * The capture class never includes '/', so `jobs.ashbyhq.com/ramp/8f3c-uuid` yields
#     `ramp` and not a path. A slug is the first segment, always.
#   * Case is never touched. CLAUDE.md records that `lever/Onehouse` resolves and
#     `lever/onehouse` 404s — Lever slugs are case-sensitive, so the captured bytes are
#     the candidate.
_GH = r"[A-Za-z0-9_-]+"
_SLUG = r"[A-Za-z0-9._-]+"

_PATTERNS: list[tuple[str, str, "re.Pattern[str]"]] = [
    ("gh_api", "greenhouse", re.compile(rf"boards-api\.greenhouse\.io/v1/boards/({_GH})")),
    # The embed, in both spellings: `job_board?for=X` and `job_board/js?for=X`. `for`
    # may not be the first parameter, hence the optional prefix.
    ("gh_embed", "greenhouse", re.compile(rf"job_board(?:/js)?\?(?:[^\"'\s<>]*?&)?for=({_GH})")),
    ("gh_job_boards", "greenhouse", re.compile(rf"job-boards\.greenhouse\.io/({_GH})")),
    ("gh_boards", "greenhouse", re.compile(rf"boards\.greenhouse\.io/({_GH})")),
    ("lever_api", "lever", re.compile(rf"api\.lever\.co/v0/postings/({_SLUG})")),
    ("lever", "lever", re.compile(rf"jobs\.lever\.co/({_SLUG})")),
    ("ashby_api", "ashby", re.compile(rf"api\.ashbyhq\.com/posting-api/job-board/({_SLUG})")),
    ("ashby", "ashby", re.compile(rf"jobs\.ashbyhq\.com/({_SLUG})")),
]

# Path segments that are part of the vendor's URL structure, not anybody's board. The
# broad host patterns above will happily capture these — `boards.greenhouse.io/embed`
# is a substring of every Greenhouse embed URL ever written — and a run that "repaired"
# a board to `embed` would look like a successful fix right up until the next fetch.
_NOT_SLUGS = frozenset(
    {
        "embed", "js", "job_board", "job-board", "job-boards", "jobs", "boards",
        "posting-api", "postings", "api", "v0", "v1", "search", "index.html",
        "robots.txt", "favicon.ico",
    }
)


def extract_candidates(page_html: str) -> list[Candidate]:
    """Board identifiers linked from a careers page, best guess first. Pure.

    Tolerates anything: a page that is JavaScript, an error document, or empty returns
    `[]` rather than raising, the same rule `sources/aggregator.py` follows. An empty
    list is a real answer — it is what sends the board to the model fallback.
    """
    if not page_html:
        return []

    # Entities first. `job_board?for=acme&amp;b=123` is the ordinary on-page spelling,
    # and a pattern reading `[^&]*` against the raw text stops in the middle of `&amp;`.
    text = html_mod.unescape(page_html)

    found: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for name, ats, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            slug = match.group(1)
            if slug.lower() in _NOT_SLUGS:
                continue
            key = (ats, slug)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Candidate(
                    ats=ats,
                    slug=slug,
                    found_by="regex",
                    pattern=name,
                    evidence=match.group(0)[:200],
                )
            )
    return found


def condense_page(page_html: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Reduce a careers page to something worth putting in a prompt. Pure.

    Keeps every href and src as its own line. That is not incidental: the thing being
    looked for *is* a URL, so a text-only reduction — strip the tags, keep the prose —
    would delete the answer and then ask the model to find it.
    """
    if not page_html:
        return ""
    text = html_mod.unescape(page_html)
    # Script bodies are where a JS-rendered board config hides, and they are also
    # megabytes of bundled framework. So harvest URLs from the *whole* document —
    # attributes and script bodies alike — and only then throw the code away. Taking
    # links from attributes alone would delete the one case the model exists for: a
    # page whose board URL lives in a JS config object rather than an href.
    links = re.findall(r"(?:href|src|data-url)\s*=\s*[\"']([^\"']+)[\"']", text)
    links += re.findall(r"https?://[^\s\"'<>()\\]+", text)
    body = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1>", " ", text)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()

    lines = [f"link: {url}" for url in dict.fromkeys(links)]
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        return joined[:max_chars]
    return (joined + "\n\n" + body)[:max_chars]


def candidate_is_grounded(candidate: Candidate, page_text: str) -> bool:
    """Did the model's slug actually appear on the page, as an identifier?

    The anti-hallucination gate, and the reason the model may propose at all. The one
    thing it must never do is derive an identifier from the company name — that is the
    guess CLAUDE.md forbids, and it is by far the most likely way an obliging model
    answers a page that contains no board link.

    A bare substring test is not enough for that, because the company name is usually
    all over its own careers page: "acme" occurs in `acme.example` on every line, so
    every invented slug would ground. So the slug has to appear where an identifier
    appears — after a `/` or an `=`, and not as the prefix of a longer token.

    Case-sensitive, because slugs are: `onehouse` read off a page that says `Onehouse`
    is a different board, and it is the one that 404s.
    """
    if not candidate.slug:
        return False
    pattern = re.compile(r"[/=]" + re.escape(candidate.slug) + r"(?![A-Za-z0-9._-])")
    return bool(pattern.search(page_text or ""))


# -- verification --------------------------------------------------------------------
def judge_candidate(
    company: Company, candidate: Candidate, result: FetchResult
) -> Verification:
    """Should this candidate be proposed? Pure, over an already-fetched board.

    The caller does the fetch — `dataclasses.replace(company, ats=…, slug=…)` through
    the existing `Fetcher.fetch_company` — so this whole rule is testable against a
    recorded payload, and the fetch itself inherits the per-host limiter, the retries
    and the adapter's own identity resolution.

    The order matters and each step is a bug someone already hit:

      unchanged     The page still advertises the board that is failing. Not a
                    rejection — it is a real finding, and a useful one: the board did
                    not move, it is down. Proposing a no-op change would bury that.
      unreachable   Failure is not evidence (§7.3).
      zero_jobs     THE greenhouse/hubspot RULE. A real-but-dead board answers 200 with
                    an empty array forever. Note the deliberate asymmetry with
                    health.evaluate(), which tolerates an empty board: an established
                    board has history in board_health that separates "always empty"
                    from "emptied on Tuesday", and a candidate has none. Adopting one
                    would also convert a loud FETCH_FAILED into a quiet SUSPECT_EMPTY —
                    trading a visible break for an invisible one.
      no_identity   The identity endpoint gave us nothing. This check MUST come before
                    the next one: `identity_matches` returns True when either side is
                    empty ("don't cry drift on missing data"), which is right for the
                    nightly loop and catastrophic here, where it would silently turn
                    "the identity endpoint 500'd" into "verified".
      wrong_company THE ashby/cedar RULE, using the same `identity_matches` the nightly
                    loop uses so the two cannot drift apart about what identity means.

    On the evidence_kind split — this is the trap in this file. Only Greenhouse has an
    `identity_url`: a *different endpoint* from the one the slug was used on, returning
    a board name. Agreement there is real evidence. Ashby and Lever derive identity by
    reading the org slug back out of the first job URL, which for a candidate slug just
    restates the candidate — `ashby/cedar` sails through that comparison. So for those
    two the evidence is provenance: the link was read out of HTML served by the
    company's own curated careers page, never constructed from its name. That is a
    genuine claim and a weaker one, so it is labelled rather than dressed up, and the
    proposal carries sample titles for the human to do what DESIGN.md §7.2 asks — read
    a few job titles.
    """
    if candidate.ats == company.ats and candidate.slug == company.slug:
        return Verification(False, "unchanged")

    if not result.ok or result.error:
        return Verification(False, "unreachable")

    if not result.postings:
        return Verification(False, "zero_jobs", board_name=result.observed_board_name or "")

    titles = tuple(p.title for p in result.postings[:3] if p.title)
    observed = result.observed_board_name or ""
    source = get_source(candidate.ats)
    has_identity_endpoint = (
        source is not None and source.identity_url(candidate.slug) is not None
    )

    if has_identity_endpoint:
        if not observed:
            return Verification(False, "no_identity", job_count=len(result.postings))
        expected = company.expected_board_name or company.name
        if not identity_matches(expected, observed):
            return Verification(
                False,
                "wrong_company",
                job_count=len(result.postings),
                board_name=observed,
                sample_titles=titles,
            )
        kind = "identity"
    else:
        kind = "provenance"

    return Verification(
        True,
        "ok",
        evidence_kind=kind,
        job_count=len(result.postings),
        board_name=observed,
        sample_titles=titles,
    )


def build_proposal(
    company: Company,
    candidate: Candidate,
    verification: Verification,
    trigger: str,
) -> Optional[RepairProposal]:
    """A rejected verification yields no proposal. There is no partial credit here."""
    if not verification.accepted:
        return None
    return RepairProposal(
        company=company.name,
        from_ats=company.ats,
        from_slug=company.slug,
        to_ats=candidate.ats,
        to_slug=candidate.slug,
        board_name=verification.board_name,
        job_count=verification.job_count,
        sample_titles=verification.sample_titles,
        evidence_kind=verification.evidence_kind,
        found_by=candidate.found_by,
        evidence=candidate.evidence,
        trigger=trigger,
    )


# -- detection -----------------------------------------------------------------------
def detect(
    companies: list[Company],
    healths: list[BoardHealth],
    threshold: int = REPAIR_FAILURE_THRESHOLD,
) -> tuple[list[RepairTarget], list[Outcome]]:
    """Which boards to work this run, and which were skipped with a reason. Pure.

    Two exclusions that are rules rather than optimizations:

      * `check_method: manual` companies are never touched. They have health rows like
        anything else, but the standing rule in CLAUDE.md is that they are never
        scraped, and a careers page is a scrape.
      * `aggregator` feeds are excluded because they have no slug to repair — their
        board_url is the feed itself, and a broken one is a curation problem.

    A detected board with no `careers_page` comes back as an Outcome rather than being
    dropped. That is an actionable gap in the curated data, not a silence.
    """
    by_name = {c.name: c for c in companies}
    targets: list[RepairTarget] = []
    outcomes: list[Outcome] = []

    for health in healths:
        company = by_name.get(health.company)
        if company is None:
            continue
        if company.check_method != "api" or get_source(company.ats) is None:
            continue
        if not needs_repair(health, threshold):
            continue
        trigger = health.status.value
        if not company.careers_page:
            outcomes.append(
                Outcome(company.name, trigger, "no_careers_page",
                        "no careers_page in companies.yaml to read")
            )
            continue
        targets.append(RepairTarget(company, health, trigger))

    return targets, outcomes


# -- the orchestrator ----------------------------------------------------------------
PageReader = Callable[[str], "tuple[Optional[str], Optional[str]]"]
Verifier = Callable[[Company, Candidate], FetchResult]


def repair_boards(
    targets: list[RepairTarget],
    read_page: PageReader,
    verify: Verifier,
    client=None,
    max_candidates: int = MAX_CANDIDATES_PER_BOARD,
) -> tuple[list[RepairProposal], list[Outcome], RepairStats]:
    """Work each target: read, extract, verify, propose. I/O comes in as arguments.

    `read_page(url) -> (text, error)` and `verify(company, candidate) -> FetchResult`
    are injected for the same reason `resolve_postings` takes a client: it makes the
    entire decision path — including the model fallback and every rejection — testable
    against recorded HTML with no network and no HTTP mocking.

    `client` is optional and its absence is not an error. Unlike `resolve`, the model
    is the fallback here, not the mechanism: with none configured the regex path runs
    normally and boards it could not crack are reported as `no_candidates`.
    """
    proposals: list[RepairProposal] = []
    outcomes: list[Outcome] = []
    stats = RepairStats(targets=len(targets))

    for target in targets:
        company, trigger = target.company, target.trigger
        log.info(
            "repairing %s (%s, currently %s/%s)",
            company.name, trigger, company.ats, company.slug,
        )

        page, error = read_page(company.careers_page)
        if error or not page:
            outcomes.append(
                Outcome(company.name, trigger, "page_unreadable",
                        error or "careers page was empty")
            )
            continue

        candidates = extract_candidates(page)
        log.debug("%s: %d regex candidate(s)", company.name, len(candidates))

        proposal, rejections = _try_candidates(
            company, trigger, candidates[:max_candidates], verify, stats
        )

        # The model only sees pages the regexes could not answer. A page that yielded a
        # candidate which then verified is done; a page whose candidates were all
        # rejected still goes to the model, because "the link we found is dead" is
        # exactly the case where a second, differently-worded link may be further down.
        if proposal is None and client is not None:
            stats.asked_model += 1
            page_text = condense_page(page)
            guess = _ask_model(client, company, page_text)
            already = {(c.ats, c.slug) for c in candidates}
            if guess is not None and (guess.ats, guess.slug) not in already:
                proposal, more = _try_candidates(
                    company, trigger, [guess], verify, stats
                )
                rejections.extend(more)

        if proposal is not None:
            proposals.append(proposal)
            stats.proposed += 1
            log.info(
                "%s: proposing %s/%s — %s, %d jobs (%s evidence, found by %s)",
                company.name, proposal.to_ats, proposal.to_slug,
                proposal.board_name or "no board name", proposal.job_count,
                proposal.evidence_kind, proposal.found_by,
            )
            continue

        stats.no_candidate += 1
        outcomes.append(_no_proposal_outcome(company.name, trigger, candidates, rejections))

    return proposals, outcomes, stats


def _try_candidates(
    company: Company,
    trigger: str,
    candidates: list[Candidate],
    verify: Verifier,
    stats: RepairStats,
) -> tuple[Optional[RepairProposal], list[tuple[Candidate, Verification]]]:
    """Verify candidates in order, stopping at the first accepted one."""
    rejections: list[tuple[Candidate, Verification]] = []
    for candidate in candidates:
        stats.candidates_tried += 1
        verification = judge_candidate(company, candidate, verify(company, candidate))
        if verification.accepted:
            return build_proposal(company, candidate, verification, trigger), rejections
        stats.rejected += 1
        rejections.append((candidate, verification))
        # WARNING, not DEBUG. A plausible-looking wrong answer that got caught is the
        # most interesting event this subsystem produces — it is the line you want in
        # the log on the night ashby/cedar nearly got proposed.
        log.warning(
            "%s: rejected %s/%s (%s) — %s",
            company.name, candidate.ats, candidate.slug, candidate.pattern or candidate.found_by,
            verification.reason,
        )
    return None, rejections


def _ask_model(client, company: Company, page_text: str) -> Optional[Candidate]:
    """One model call, and every way it can go wrong returns None.

    Grounding is checked here rather than inside the client so that the rule lives next
    to the thing it protects: a slug the model did not read off the page is a slug it
    invented, and the most likely source of an invention is the company name.
    """
    guess = client.find_board(company.name, company.careers_page, page_text)
    if guess is None:
        return None
    candidate = Candidate(
        ats=guess.ats,
        slug=guess.slug,
        found_by="llm",
        pattern="llm",
        evidence=guess.evidence,
    )
    if not candidate_is_grounded(candidate, page_text):
        log.warning(
            "%s: discarding model slug %r — it does not appear on the page it was shown",
            company.name, candidate.slug,
        )
        return None
    return candidate


def _no_proposal_outcome(
    company: str,
    trigger: str,
    candidates: list[Candidate],
    rejections: list[tuple[Candidate, Verification]],
) -> Outcome:
    if not candidates and not rejections:
        return Outcome(company, trigger, "no_candidates",
                       "no board link found on the careers page")
    reasons = [v.reason for _, v in rejections]
    # Every candidate being the slug we already have is a distinct, useful finding: the
    # company still advertises this board, so it did not move — it is broken.
    if reasons and all(r == "unchanged" for r in reasons):
        return Outcome(company, trigger, "unchanged",
                       "the careers page still advertises this exact board")
    detail = ", ".join(
        f"{c.ats}/{c.slug}: {v.reason}" for c, v in rejections
    )
    return Outcome(company, trigger, "all_rejected", detail)


# -- rendering -----------------------------------------------------------------------
def render(
    proposals: list[RepairProposal],
    outcomes: list[Outcome],
    stats: RepairStats,
) -> str:
    """The human-readable half. The diff is rendered by the caller, which has the file."""
    lines: list[str] = []
    lines.append(f"# Slug repair — {stats.summary()}")
    lines.append("")

    if proposals:
        lines.append(f"## Verified proposals ({len(proposals)})")
        lines.append("")
        for p in proposals:
            lines.append(f"### {p.company}  [{p.trigger}]")
            lines.append(f"    {p.from_ats}/{p.from_slug}  ->  {p.to_ats}/{p.to_slug}")
            lines.append(
                f"    verified: {p.job_count} jobs"
                + (f", board name {p.board_name!r}" if p.board_name else "")
            )
            lines.append(f"    found by: {p.found_by}  ·  from: {p.evidence}")
            if p.sample_titles:
                lines.append("    titles:   " + " · ".join(p.sample_titles))
            if p.evidence_kind == "provenance":
                # Say it every time. This is the one class of proposal a human should
                # not rubber-stamp, and the reason is not obvious from the diff.
                lines.append(
                    "    ⚠ identity is PROVENANCE only — this ATS has no board-name "
                    "endpoint, so the check rests on the link being on the company's "
                    "own careers page. Read the titles above before applying."
                )
            lines.append("")
    else:
        lines.append("_No verified proposals._")
        lines.append("")

    if outcomes:
        lines.append(f"## Not repaired ({len(outcomes)})")
        lines.append("")
        for o in outcomes:
            lines.append(f"- **{o.company}** [{o.trigger}] `{o.reason}` — {o.detail}")
        lines.append("")

    return "\n".join(lines)
