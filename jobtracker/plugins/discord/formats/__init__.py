"""Message formats. Importing this package registers each one.

Adding a format is one module here plus one line below — the same two steps as adding an
ATS to `sources/` or a task to `tasks/`, and the same rule: the module is pure.
"""

from .base import Format, FormatContext, ParsedJob, formats, register  # noqa: F401
from . import cscareers, generic  # noqa: F401  (side effect: register())
