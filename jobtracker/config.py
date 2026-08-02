"""Paths and loaders. The one place filesystem locations are resolved.

DB path comes from $JOBTRACKER_DB so the container can point it at a mounted volume
(/data/state.db) while local dev uses ./data/state.db. Curated inputs (companies.yaml,
criteria.yaml) live next to the package root and are read-only at runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .models import Company

# Repo root = parent of the jobtracker/ package directory.
ROOT = Path(__file__).resolve().parent.parent

COMPANIES_YAML = Path(os.environ.get("JOBTRACKER_COMPANIES", ROOT / "companies.yaml"))
CRITERIA_YAML = Path(os.environ.get("JOBTRACKER_CRITERIA", ROOT / "criteria.yaml"))
PROFILE_YAML = Path(os.environ.get("JOBTRACKER_PROFILE", ROOT / "profile.yaml"))

_DEFAULT_DB = ROOT / "data" / "state.db"
DB_PATH = Path(os.environ.get("JOBTRACKER_DB", _DEFAULT_DB))


def load_companies(path: str | Path | None = None) -> list[Company]:
    """Parse companies.yaml into Company objects. Fails loudly on a malformed entry."""
    path = Path(path) if path is not None else COMPANIES_YAML
    if not path.exists():
        raise FileNotFoundError(
            f"companies file not found: {path} — run `jobtracker migrate` first"
        )

    with path.open() as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a top-level list of companies")

    companies: list[Company] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entry {i} is not a mapping")
        name = entry.get("name")
        ats = entry.get("ats")
        if not name or not ats:
            raise ValueError(f"{path}: entry {i} missing required name/ats")
        if name in seen:
            raise ValueError(f"{path}: duplicate company name {name!r}")
        seen.add(name)
        companies.append(
            Company(
                name=str(name),
                ats=str(ats),
                slug=str(entry.get("slug") or ""),
                tier=entry.get("tier"),
                category=str(entry.get("category") or ""),
                check_method=str(entry.get("check_method") or "manual"),
                expected_board_name=(
                    str(entry["expected_board_name"])
                    if entry.get("expected_board_name")
                    else None
                ),
                careers_page=str(entry.get("careers_page") or ""),
                board_url=str(entry.get("board_url") or ""),
                notes=str(entry.get("notes") or ""),
            )
        )
    return companies


def ensure_data_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
