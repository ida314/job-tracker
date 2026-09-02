"""A switch in plugins.yaml for every bounded model role in `tasks/`.

Named `roles.py` rather than `tasks.py` on purpose: a `jobtracker/plugins/tasks.py` next
door to `jobtracker/tasks/` turns every `from . import tasks` in this package into
something you have to stop and re-read.

The switches are **derived from the task registry**, not hand-written one per role. That
is the whole point of doing it this way: moving `level`, `judge` and `inbox` fully into
this system is then a change to `_ENABLED_BY_DEFAULT` and some prose, not three new
modules. A role that registers itself in `tasks/` is switchable here the same day.

`_ENABLED_BY_DEFAULT` is what keeps this patch behaviour-preserving. The three roles that
existed before plugins had kinds default to **on**, so a box that never opens plugins.yaml
runs exactly the queue it ran yesterday; `tailor` defaults to **off**, because a new role
that starts working on its own is not "separate from the rest of the system". Neither
default is a claim about which roles matter — it is only about which ones were already
running.
"""

from __future__ import annotations

from .base import TaskPlugin, register

# The roles that predate the plugin switch. On unless you say otherwise, so that adding
# the switch changes nothing for anyone who does not use it.
_ENABLED_BY_DEFAULT = frozenset({"level", "judge", "inbox"})


def register_task_plugins() -> list:
    """Register one switch per task currently in the registry.

    Called at import time from `plugins/__init__.py`, *after* `tasks` has been imported,
    so the registry it reads is the full one. Idempotent by construction — `register`
    keys on the name, so importing this package twice re-registers rather than duplicates.
    """
    from .. import tasks

    made = []
    for task in tasks.all_tasks():
        made.append(register(TaskPlugin(
            name=task.name,
            task_name=task.name,
            summary=task.summary,
            default_enabled=task.name in _ENABLED_BY_DEFAULT,
        )))
    return made


def enabled_task_names(configured: dict) -> set:
    """Which task plugins are switched on, given a loaded plugins.yaml.

    `configured` is `settings.load_settings()`'s output — a plugin absent from it takes
    its own default, which is why this cannot be written as a comprehension over the
    file's keys. A fresh box has no plugins.yaml at all and must still run its queue.
    """
    from .base import KIND_TASK, plugins_of_kind

    on = set()
    for plugin in plugins_of_kind(KIND_TASK):
        entry = configured.get(plugin.name, {})
        want = entry.get("enabled", plugin.defaults().get("enabled", False))
        if want:
            on.add(plugin.task_name)
    return on
