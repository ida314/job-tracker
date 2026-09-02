"""Plugins. Importing this package registers every one.

Two kinds — see `base.py`. An **import** plugin is a feed of postings (Discord); a
**task** plugin is the on/off switch for a bounded model role in `tasks/`.

Adding an import plugin is one module plus one line below — the same two steps as adding
an ATS to `sources/` or a task to `tasks/`, and the same rule: the plugin module is pure
and `runner.py` owns the socket. Adding a task plugin is nothing at all: `roles.py`
derives one per registered task, so a new role in `tasks/` is switchable the same day.
"""

from .base import (  # noqa: F401
    KIND_IMPORT,
    KIND_TASK,
    BasePlugin,
    Plugin,
    PluginFetch,
    TaskPlugin,
    all_plugins,
    get_plugin,
    plugin_names,
    plugins_of_kind,
    register,
)
from .runner import collect  # noqa: F401
from . import discord  # noqa: F401  (side effect: register())
from . import roles  # noqa: F401
from .roles import enabled_task_names  # noqa: F401

# Import plugins register themselves on import; task plugins are derived from the task
# registry, so this runs last — `tasks` has to have finished importing first.
roles.register_task_plugins()
