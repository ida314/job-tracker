"""Import plugins. Importing this package registers every one.

Adding a plugin is one module plus one line below — the same two steps as adding an ATS
to `sources/` or a task to `tasks/`, and the same rule: the plugin module is pure and
`runner.py` owns the socket.
"""

from .base import (  # noqa: F401
    Plugin,
    PluginFetch,
    all_plugins,
    get_plugin,
    plugin_names,
    register,
)
from .runner import collect  # noqa: F401
from . import discord  # noqa: F401  (side effect: register())
