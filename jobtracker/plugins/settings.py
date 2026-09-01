"""plugins.yaml: which feeds are on, and how each is pointed at its source.

Curation, in the DESIGN.md 3.3 sense — every writer is a command you typed
(`jobtracker plugins enable|disable|set`), and no scheduled run touches it. The token is
not here; it is `$JOBTRACKER_DISCORD_TOKEN`, for the reasons in `config.py`.

Validation is strict, and that is a direct lesson from this project's own history: v1's
config was a fenced YAML block with an unterminated quote that nothing ever parsed, and
DESIGN.md 2.1 draws the conclusion — "a config format nothing validates is not
configuration; it is a comment". So a bad value fails here, at the CLI, in front of you,
rather than at 01:00 in front of nobody.

Shape:

    discord:
      enabled: true
      channel_id: "123456789012345678"
      label: new-grad-jobs
      backfill_days: 14
      expire_after_days: 90
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

DEFAULTS: dict = {
    "enabled": False,
    # How far back the first read reaches. Only used when there is no stored cursor.
    "backfill_days": 14,
    # A feed announces and never retracts, so its postings close by age. 0 disables it,
    # which is a legitimate choice and means "only ever close for a better-known reason".
    "expire_after_days": 90,
    # Names the posting group, so it reads as "Discord: #new-grad-jobs" in the dashboard.
    "label": "",
    "channel_id": "",
    "guild_id": "",
}

# Ints that may not be negative. `expire_after_days: 0` means "never expire".
_INT_KEYS = ("backfill_days", "expire_after_days")
_STR_KEYS = ("label", "channel_id", "guild_id")


class InvalidSettings(ValueError):
    """plugins.yaml said something that cannot be acted on."""


def _known_names() -> set:
    from .base import plugin_names

    return set(plugin_names())


def load_settings(path: Optional[Path] = None) -> dict:
    """Parse and validate plugins.yaml. An absent file is `{}` — a normal state.

    Doubles as `safewrite`'s validator, which is why it raises rather than repairing:
    a candidate that does not load is never swapped in, and the `.bak` stays good.
    """
    from .. import config

    target = Path(path) if path else config.PLUGINS_YAML
    if not target.exists():
        return {}
    try:
        raw = yaml.safe_load(target.read_text()) or {}
    except yaml.YAMLError as exc:
        raise InvalidSettings(f"{target} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidSettings(f"{target} must be a mapping of plugin name -> settings")

    known = _known_names()
    out: dict = {}
    for name, entry in raw.items():
        if known and name not in known:
            raise InvalidSettings(
                f"unknown plugin {name!r} (known: {', '.join(sorted(known)) or 'none'})"
            )
        if not isinstance(entry, dict):
            raise InvalidSettings(f"{name}: settings must be a mapping")
        merged = dict(DEFAULTS)
        for key, value in entry.items():
            if key not in DEFAULTS:
                raise InvalidSettings(f"{name}: unknown setting {key!r}")
            merged[key] = value
        if not isinstance(merged["enabled"], bool):
            raise InvalidSettings(f"{name}: `enabled` must be true or false")
        for key in _INT_KEYS:
            if not isinstance(merged[key], int) or isinstance(merged[key], bool):
                raise InvalidSettings(f"{name}: `{key}` must be a whole number of days")
            if merged[key] < 0:
                raise InvalidSettings(f"{name}: `{key}` cannot be negative")
        for key in _STR_KEYS:
            merged[key] = "" if merged[key] is None else str(merged[key]).strip()
        if merged["channel_id"] and not merged["channel_id"].isdigit():
            raise InvalidSettings(
                f"{name}: `channel_id` must be the numeric id Discord's "
                f"'Copy Channel ID' gives you, not a channel name"
            )
        if merged["guild_id"] and not merged["guild_id"].isdigit():
            raise InvalidSettings(f"{name}: `guild_id` must be numeric")
        out[name] = merged
    return out


def settings_for(name: str, path: Optional[Path] = None) -> dict:
    """One plugin's settings, defaults filled in. Never raises for a missing entry."""
    return {**DEFAULTS, **load_settings(path).get(name, {})}


def coerce(key: str, value: str):
    """Turn a `key=value` string from the CLI into the type DEFAULTS says it is.

    Typed here rather than at read time so `backfill_days=soon` is refused while you are
    standing there, instead of loading as a string and failing inside a nightly run.
    """
    if key not in DEFAULTS:
        raise InvalidSettings(
            f"unknown setting {key!r} (known: {', '.join(sorted(DEFAULTS))})"
        )
    if key == "enabled":
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise InvalidSettings("`enabled` must be true or false")
    if key in _INT_KEYS:
        try:
            number = int(value.strip())
        except ValueError:
            raise InvalidSettings(f"`{key}` must be a whole number of days") from None
        if number < 0:
            raise InvalidSettings(f"`{key}` cannot be negative")
        return number
    return value.strip()


def _write(path: Path, data: dict) -> None:
    from .. import safewrite

    body = yaml.safe_dump(data, sort_keys=True, default_flow_style=False)
    safewrite.write_text(path, body, load_settings)


def set_enabled(path: Path, name: str, on: bool) -> dict:
    return set_options(path, name, {"enabled": on})


def set_options(path: Path, name: str, options: dict) -> dict:
    """Merge `options` into one plugin's entry and write the file atomically.

    A YAML round-trip, unlike companies.yaml's line-oriented writer. That is a real
    tradeoff and it is acceptable here for a reason that does not hold there: this file
    is a handful of scalar keys with no prose, so there is no long `notes:` string for
    PyYAML to re-fold and no reviewable diff to wreck. `plugins.example.yaml` is the
    documented copy, and it says that comments here do not survive a write.
    """
    known = _known_names()
    if known and name not in known:
        raise InvalidSettings(
            f"unknown plugin {name!r} (known: {', '.join(sorted(known)) or 'none'})"
        )
    current = load_settings(path)
    entry = dict(current.get(name, {}))
    entry.update(options)
    # Store only what differs from the defaults, so the file stays readable and a later
    # change of default is not silently pinned by a value nobody chose.
    trimmed = {k: v for k, v in entry.items() if v != DEFAULTS.get(k)}
    trimmed.setdefault("enabled", entry.get("enabled", False))
    current[name] = trimmed
    _write(Path(path), current)
    return settings_for(name, path)
