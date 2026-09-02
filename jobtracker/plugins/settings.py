"""plugins.yaml: which plugins are on, and how each is configured.

Curation, in the DESIGN.md 3.3 sense — every writer is a command you typed
(`jobtracker plugins enable|disable|set`), and no scheduled run touches it. The token is
not here; it is `$JOBTRACKER_DISCORD_TOKEN`, for the reasons in `config.py`.

Validation is strict, and that is a direct lesson from this project's own history: v1's
config was a fenced YAML block with an unterminated quote that nothing ever parsed, and
DESIGN.md 2.1 draws the conclusion — "a config format nothing validates is not
configuration; it is a comment". So a bad value fails here, at the CLI, in front of you,
rather than at 01:00 in front of nobody.

**Each plugin declares its own settings**, in `defaults()`. There was one global dict here
until the registry grew a second kind, and it did not survive contact with one: every
plugin's config surface was the union of every plugin's keys, so `channel_id` was a valid
setting on a model role and was `.isdigit()`-validated as one. The schema is per-plugin
now, the type of a setting is the type of its default, and semantic rules live on the
plugin in `validate()`.

Shape:

    discord:
      enabled: true
      channel_id: "123456789012345678"
      label: new-grad-jobs
      backfill_days: 14
      expire_after_days: 90

    tailor:
      enabled: true
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

# The one setting every plugin has, whatever its kind. Everything else comes from the
# plugin's own `defaults()`.
COMMON_DEFAULTS: dict = {"enabled": False}


class InvalidSettings(ValueError):
    """plugins.yaml said something that cannot be acted on."""


def _known_names() -> set:
    from .base import plugin_names

    return set(plugin_names())


def defaults_for(name: str) -> dict:
    """One plugin's settings schema, or the common one for a name nobody registered.

    The fallback matters more than it looks. `load_settings` refuses an unknown plugin
    outright, but only when *something* is registered — a caller that has not imported
    the plugins package yet sees an empty registry, and the file must still load rather
    than blow up on an import-order accident.
    """
    from .base import get_plugin

    plugin = get_plugin(name)
    if plugin is None:
        return dict(COMMON_DEFAULTS)
    return {**COMMON_DEFAULTS, **plugin.defaults()}


def _check_type(name: str, key: str, value, default) -> None:
    """The declared type is the default's type. Booleans are checked before ints.

    `isinstance(True, int)` is True in Python, so an `enabled: 3` would sail through an
    int check written the obvious way round and land in the file as a truthy switch
    nobody typed.
    """
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise InvalidSettings(f"{name}: `{key}` must be true or false")
    elif isinstance(default, int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidSettings(f"{name}: `{key}` must be a whole number")


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
        schema = defaults_for(name)
        merged = dict(schema)
        for key, value in entry.items():
            if key not in schema:
                raise InvalidSettings(f"{name}: unknown setting {key!r}")
            merged[key] = value
        for key, default in schema.items():
            _check_type(name, key, merged[key], default)
            if isinstance(default, str):
                merged[key] = "" if merged[key] is None else str(merged[key]).strip()
        # The semantic half, which only the plugin knows: a channel id is numeric, a day
        # count is not negative. It used to live here and fired for every plugin.
        from .base import get_plugin

        plugin = get_plugin(name)
        if plugin is not None:
            plugin.validate(merged)
        out[name] = merged
    return out


def settings_for(name: str, path: Optional[Path] = None) -> dict:
    """One plugin's settings, defaults filled in. Never raises for a missing entry."""
    return {**defaults_for(name), **load_settings(path).get(name, {})}


def coerce(name: str, key: str, value: str):
    """Turn a `key=value` string from the CLI into the type that plugin says it is.

    Typed here rather than at read time so `backfill_days=soon` is refused while you are
    standing there, instead of loading as a string and failing inside a nightly run.

    Takes the plugin name because the schema is per-plugin now: the same key can be an
    int on one plugin and absent on another, and a `coerce` that guessed from the key
    alone would be the global DEFAULTS dict again in a smaller place.
    """
    schema = defaults_for(name)
    if key not in schema:
        raise InvalidSettings(
            f"{name}: unknown setting {key!r} (known: {', '.join(sorted(schema))})"
        )
    default = schema[key]
    converted: object
    if isinstance(default, bool):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            converted = True
        elif low in ("false", "no", "off", "0"):
            converted = False
        else:
            raise InvalidSettings(f"`{key}` must be true or false")
    elif isinstance(default, int):
        try:
            converted = int(value.strip())
        except ValueError:
            raise InvalidSettings(f"`{key}` must be a whole number") from None
    else:
        converted = value.strip()

    # The semantic rules too, not just the type. `backfill_days=-3` and
    # `channel_id=new-grad-jobs` both parse as the right type and are both refusals, and
    # they have to land here rather than three layers down in `safewrite` — the whole
    # point of typing at the CLI is that you are still standing there. `validate` judges
    # one key against defaults for the rest, which is why it must judge keys singly.
    from .base import get_plugin

    plugin = get_plugin(name)
    if plugin is not None:
        plugin.validate({**schema, key: converted})
    return converted


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
    schema = defaults_for(name)
    trimmed = {k: v for k, v in entry.items() if v != schema.get(k)}
    trimmed.setdefault("enabled", entry.get("enabled", False))
    current[name] = trimmed
    _write(Path(path), current)
    return settings_for(name, path)
