"""The one writer for `companies.yaml`.

That file is curated data: human-authored, git-tracked, and read as a diff. Nothing on a
schedule may write it (DESIGN.md §2.3) — every writer is a foreground action somebody
took, which means `migrate` (once), `add-company`, `verify-slugs --write`,
`repair --write`, and the add form on `/companies` under `serve`.

Two operations, and they are not the same shape:

  * **Editing** an existing entry — `rewrite_companies`, used by repair and verify-slugs.
    Line-oriented, because a PyYAML round trip re-folds long strings to its own width and
    a one-line slug change then produces a diff touching ten other companies' `notes:`
    prose. Measured on the real file.
  * **Appending** a new one — `insert_entry`. `edit_entry` deliberately cannot do this
    (an unknown name is a `KeyError`), so the new block is rendered on its own and
    spliced in as text. Same reason, same result: every pre-existing line survives
    byte-for-byte.

Everything here is pure — text and dataclasses in, text out, with no I/O beyond reading
the file the caller names. That is what lets `cli` and `server` both depend on it, and it
is why the diff a human reviews and the bytes that land are produced by one function.
"""

from __future__ import annotations

import difflib
import logging
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

import yaml

from .models import Company
from .sources import api_sources

log = logging.getLogger("jobtracker.curation")

# Canonical field order on disk. `name` leads because it opens the block; the rest
# mirrors migrate._KEEP, which wrote all 100 existing entries, plus `board_url` — which
# sits where `careers_page` does, since a feed has one or the other and never both.
FIELD_ORDER = (
    "name",
    "ats",
    "slug",
    "tier",
    "category",
    "check_method",
    "careers_page",
    "board_url",
    "notes",
    "expected_board_name",
)

# Every `ats` value the live file uses. The four with adapters are `api`-capable; the
# rest name a portal we can only check by hand. `config.load_companies` deliberately does
# NOT enforce this — it has to keep loading whatever is already on disk — but a *new*
# entry naming something else is a typo, and typos here fail silently.
ATS_VALUES = frozenset(
    {"greenhouse", "lever", "ashby", "aggregator", "workday", "gem", "bespoke", "unknown"}
)
CHECK_METHODS = frozenset({"api", "manual", "aggregator"})
TIER_RANGE = range(1, 8)
# Bounded so a runaway paste hits a real message. `server._read_json` returns `{}` for a
# body over MAX_BODY, which would otherwise surface as "name is required" — a
# correct-looking error for entirely the wrong reason.
MAX_NOTES = 4000


# -- validating a new entry ----------------------------------------------------------
def validate_new(company: Company, existing: Sequence[Company]) -> list[str]:
    """Every reason this entry should not be added. Empty means it is coherent.

    Deliberately stricter than `config.load_companies`, which checks only that `name` and
    `ats` are non-empty and that names are unique. The loader must stay permissive so an
    existing file keeps loading; a new entry gets the strict pass, because the failures it
    catches are all silent ones — `check_method: api` on an ats with no adapter is a board
    that is skipped every night behind a single log line, which reads exactly like a board
    with nothing open. That is DESIGN.md §3.4, at the moment of data entry.

    Note this says nothing about whether the board is real. That is verification, and it
    needs a socket; this is pure.
    """
    errors: list[str] = []
    name = (company.name or "").strip()

    if not name:
        errors.append("name is required")
    if "\n" in (company.name or ""):
        errors.append(
            "a name may not contain a line break — the writer finds entries by their "
            '"- name:" line'
        )
    if name:
        for other in existing:
            if other.name == name:
                errors.append(f"{name!r} is already tracked")
                break
            if other.name.strip().lower() == name.lower():
                errors.append(
                    f"{name!r} collides with {other.name!r}, already on the list — the "
                    "name is the key postings, verdicts and applications are stored under"
                )
                break

    if not (company.ats or "").strip():
        errors.append("ats is required")
    elif company.ats not in ATS_VALUES:
        errors.append(f"ats must be one of: {', '.join(sorted(ATS_VALUES))}")

    if company.check_method not in CHECK_METHODS:
        errors.append("check_method must be api, manual or aggregator")

    if company.check_method == "api":
        if company.ats not in api_sources():
            errors.append(
                f"check_method: api needs an ats with an adapter "
                f"({', '.join(sorted(api_sources()))}). {company.ats!r} has none, so this "
                "board would be skipped every night with nothing but a log line"
            )
        if not (company.slug or "").strip():
            errors.append(
                'an api entry needs a slug — fetch_company reports "empty slug" and the '
                "board is never checked"
            )

    # Only for `api`. On a `manual` entry `slug` is documentation, not an identifier —
    # Red Hat carries "redhat / wd5 / jobs" and Nvidia "nvidia / wd5 /
    # NVIDIAExternalCareerSite", which are Workday tenant triples a human reads. Applying
    # the shape rule to those would make this stricter than the file it validates.
    if company.check_method == "api" and company.slug and any(
        c in company.slug for c in " \t/:"
    ):
        errors.append(
            "a slug is the board identifier only, not a URL — "
            '"greenhouse/stripe" should be ats: greenhouse, slug: stripe'
        )

    if company.slug and company.ats:
        for other in existing:
            if other.ats == company.ats and other.slug == company.slug:
                errors.append(
                    f"{company.ats}/{company.slug} is already tracked as {other.name!r} — "
                    "two names on one board split its postings across two diff namespaces"
                )
                break

    # Deliberately NOT an error: an aggregator with no board_url is skipped rather than
    # failing the run, and the unconfirmed Ouckah/CVrve feed is parked in exactly that
    # state on purpose (`aggregator.py`, and CLAUDE.md's "Aggregator sources"). The page
    # says so next to the field instead, because a rule stricter than the file it
    # validates is a rule that will be deleted the first time it fires.

    for field in ("careers_page", "board_url"):
        value = (getattr(company, field) or "").strip()
        if value and urlparse(value).scheme not in ("http", "https"):
            errors.append(f"{field} must be an http(s) URL")

    if company.tier is not None and company.tier not in TIER_RANGE:
        errors.append(
            f"tier must be a whole number from {TIER_RANGE.start} to {TIER_RANGE.stop - 1}"
        )

    if "\n" in (company.category or ""):
        errors.append("category may not contain a line break")
    if len(company.notes or "") > MAX_NOTES:
        errors.append(f"notes is longer than {MAX_NOTES} characters")

    return errors


# -- appending a new entry -----------------------------------------------------------
def entry_fields(company: Company) -> dict:
    """The company as the mapping that belongs in companies.yaml.

    `name`, `ats` and `expected_board_name` are always emitted — the last one as `null`
    when unset, which is what all 100 existing entries do and what makes "nobody has
    verified this board" a visible state rather than a missing key. Everything else is
    written only when it has a value, which is what reproduces the several key orders
    already on disk (an aggregator carries `board_url` and no `slug`, `tier` or
    `careers_page`; a bespoke portal carries `careers_page` and no `slug`).
    """
    fields: dict = {"name": company.name, "ats": company.ats}
    for key in FIELD_ORDER:
        if key in ("name", "ats", "expected_board_name"):
            continue
        value = getattr(company, key, None)
        if value not in (None, "", []):
            fields[key] = value
    fields["expected_board_name"] = company.expected_board_name or None
    return fields


def render_entry(company: Company) -> str:
    """One entry as the YAML text that belongs in companies.yaml.

    Dumped as a ONE-element list, so PyYAML formats this block and only this block. The
    other hundred entries are never parsed, never re-dumped, and therefore never
    re-folded — which is the whole reason this exists instead of `safe_load` +
    `safe_dump` over the document.

    `width=100` matches what wrote every existing entry, so a long `notes:` folds the way
    its neighbours did.
    """
    return yaml.safe_dump(
        [entry_fields(company)],
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )


def insert_entry(text: str, company: Company) -> str:
    """companies.yaml text with one new entry spliced in, in tier order.

    The file is ordered by tier — 26 tier-1 entries, then tier-2, down to tier-7, with the
    three untiered aggregator feeds at the end — and that ordering is load-bearing for
    anyone reading the file. A bare append would put a new tier-2 company after the
    tier-7 lakehouse entries and the ordering would rot one addition at a time.

    The rule is **insert before the first entry that sorts after me**: the first block
    with a higher tier, or the first block with no tier at all, whichever comes first.
    Not "after the last block with my tier, else end of file" — that looks equivalent and
    is not, because a tier the file does not use yet has no last block, and the fallback
    would drop a tier-5 company below the untiered feeds.

    An entry with no tier sorts after every tiered one, so it lands at the end. That is
    where all three untiered entries already are; it is the rule, not a fallback.
    """
    block = render_entry(company)
    lines = text.splitlines(keepends=True)
    at = _insert_point(lines, company.tier)
    if at is None:
        # Every writer here emits a trailing newline, but a hand-edited file might not,
        # and a block starting mid-line would corrupt the entry above it.
        if text and not text.endswith("\n"):
            text += "\n"
        return text + block
    return "".join(lines[:at]) + block + "".join(lines[at:])


def _insert_point(lines: list[str], tier: Optional[int]) -> Optional[int]:
    """Index of the line the new block goes before, or None for end-of-file."""
    if tier is None:
        return None
    for start in _entry_starts(lines):
        other = _tier_of(lines, start)
        if other is None or other > tier:
            return start
    return None


def _entry_starts(lines: list[str]) -> list[int]:
    """Index of every line that opens an entry. Everything above the first is the header,
    which is a comment block and must never be inserted into."""
    return [i for i, line in enumerate(lines) if line.startswith("- ")]


def _tier_of(lines: list[str], start: int) -> Optional[int]:
    """The `tier:` of the entry beginning at `start`, read off its own lines.

    Deliberately not `yaml.safe_load` over the whole document: placement decided by one
    parse and splicing done against the raw text would be two readings of the same bytes
    that can disagree. `edit_entry` locates entries the same way, for the same reason.
    """
    for j in range(start + 1, _entry_end(lines, start)):
        if lines[j].startswith("  tier:"):
            value = yaml.safe_load(lines[j].split(":", 1)[1].strip())
            return value if isinstance(value, int) else None
    return None


# -- editing an existing entry -------------------------------------------------------
def rendered_companies(path: str | Path, updates: dict[str, dict]) -> str:
    """companies.yaml with `updates` applied, as text. Does not touch the file.

    Edits the lines it changes rather than round-tripping the document through PyYAML,
    which re-folds long strings to its own width: a one-line repair produced a diff
    touching ten other companies. Only single-line scalar fields are supported, which is
    all a repair changes (`ats`, `slug`, `expected_board_name`). Rendering and writing
    are split so the diff a human reviews and the bytes that land come from the same
    function.
    """
    text = Path(path).read_text()
    for name, fields in updates.items():
        text = edit_entry(text, name, fields)
    return text


def edit_entry(text: str, name: str, fields: dict) -> str:
    """Replace scalar fields on one `- name: X` block, in place."""
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i in _entry_starts(lines)
            # Parse the scalar rather than string-comparing it: a name needing quotes
            # in YAML would otherwise never match itself.
            if lines[i].startswith("- name:")
            and yaml.safe_load(lines[i].split(":", 1)[1].strip()) == name
        ),
        None,
    )
    if start is None:
        raise KeyError(f"{name!r} not found in companies.yaml")
    end = _entry_end(lines, start)

    for key, value in fields.items():
        rendered = "  " + yaml.safe_dump(
            {key: value}, default_flow_style=False, allow_unicode=True, width=10**6
        ).rstrip("\n") + "\n"
        at = next(
            (j for j in range(start, end) if lines[j].startswith(f"  {key}:")), None
        )
        if at is None:
            lines.insert(start + 1, rendered)
            end += 1
            continue
        # A scalar can still be folded across continuation lines; replacing only the
        # first would leave the tail of the old value behind as garbage.
        stop = at + 1
        while stop < end and lines[stop].startswith("    "):
            stop += 1
        lines[at:stop] = [rendered]
        end -= stop - at - 1

    return "".join(lines)


def _entry_end(lines: list[str], start: int) -> int:
    return next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("- ")), len(lines)
    )


def rewrite_companies(path: str | Path, updates: dict[str, dict]) -> None:
    """Apply field updates to companies.yaml in place.

    The one editor the foreground commands share (`verify-slugs --write`, `repair
    --write`). No *scheduled* run may call this: companies.yaml is curated data, and
    keeping machine state out of it is DESIGN.md §2.3.
    """
    Path(path).write_text(rendered_companies(path, updates))


def has_inline_comments(path: str | Path) -> bool:
    """Does this file carry `#` comments below its header block?

    `migrate` round-trips the document through PyYAML, which preserves values and
    discards comments. Today that is harmless — companies.yaml carries only its header,
    and the per-entry prose lives in `notes:`, which is a value — so this is the guard
    that keeps it harmless if someone later writes a `#` note next to an entry.

    It does NOT apply to `insert_entry`, which rewrites no existing line and so has no
    comment to lose. Adding the guard there "for consistency" would refuse a write that
    is provably safe.

    Skips the *leading comment block* rather than matching `_HEADER` verbatim. Matching
    the constant would mean any future edit to the header text turns the real file's own
    header into "inline comments" and blocks every write, which is a trap disguised as a
    safety check.
    """
    lines = Path(path).read_text().splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return any(line.lstrip().startswith("#") for line in lines[i:])


# -- the diff a human reads ----------------------------------------------------------
def diff(label: str, before: str, after: str) -> str:
    """A unified diff between two renderings of the file.

    One helper for both writers, so "the diff comes from the same text that was written"
    holds for an edit and for an append alike.
    """
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=label,
            tofile=label,
            n=3,
        )
    )


def companies_diff(path: str | Path, updates: dict[str, dict]) -> str:
    """A unified diff of a proposed edit, from the same renderer that writes it."""
    path = Path(path)
    return diff(str(path), path.read_text(), rendered_companies(path, updates))
