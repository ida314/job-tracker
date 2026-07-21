"""One-time migration: backend-newgrad-2027-tracker.md -> companies.yaml.

Parses the `## Tier N` sections and `### Name` blocks of the v1 markdown into the
curated companies.yaml schema. Drops the machine-authored / derived fields
(board_url, status, last_checked, last_posting_seen); keeps name/ats/slug/tier/
category/check_method/careers_page/notes. expected_board_name is left unset — it is
bootstrapped later by `verify-slugs`, since the markdown never recorded it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_TIER_RE = re.compile(r"^##\s+Tier\s+(\d+)", re.IGNORECASE)
_SECTION_RE = re.compile(r"^##\s+")
_HEADING_RE = re.compile(r"^###\s+(.*\S)\s*$")
_FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.*)$")

# Fields carried into companies.yaml, in output order. Everything else is dropped.
_KEEP = ["ats", "slug", "category", "check_method", "careers_page", "notes"]


def parse_markdown(text: str) -> list[dict]:
    companies: list[dict] = []
    current_tier: int | None = None
    current: dict | None = None

    for line in text.splitlines():
        tier_m = _TIER_RE.match(line)
        if tier_m:
            current_tier = int(tier_m.group(1))
            current = None
            continue
        if _SECTION_RE.match(line) and not line.startswith("###"):
            # A non-tier top-level section (Aggregator sources, Maintenance…).
            if not _TIER_RE.match(line):
                current_tier = None
            current = None
            continue

        heading_m = _HEADING_RE.match(line)
        if heading_m:
            current = {"name": heading_m.group(1).strip(), "tier": current_tier}
            companies.append(current)
            continue

        field_m = _FIELD_RE.match(line)
        if field_m and current is not None:
            key, value = field_m.group(1), field_m.group(2).strip()
            current[key] = value

    return companies


def to_yaml_entries(parsed: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for c in parsed:
        entry: dict = {"name": c["name"], "ats": c.get("ats", "unknown")}
        if c.get("slug"):
            entry["slug"] = c["slug"]
        if c.get("tier") is not None:
            entry["tier"] = c["tier"]
        for key in _KEEP:
            if key in ("ats", "slug"):
                continue
            val = c.get(key)
            if val:
                entry[key] = val
        entry["expected_board_name"] = None
        entries.append(entry)
    return entries


_HEADER = (
    "# Curated target list. Human-authored, git-tracked, NEVER machine-written.\n"
    "# Generated once from backend-newgrad-2027-tracker.md by `jobtracker migrate`.\n"
    "# No status / last_checked here — run state lives in state.db (DESIGN.md §5).\n"
    "# expected_board_name is bootstrapped by `jobtracker verify-slugs --write`.\n\n"
)


def migrate(markdown_path: str | Path, out_path: str | Path) -> list[dict]:
    text = Path(markdown_path).read_text()
    entries = to_yaml_entries(parse_markdown(text))
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=100)
    Path(out_path).write_text(_HEADER + body)
    return entries
