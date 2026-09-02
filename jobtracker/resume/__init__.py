"""Resume formats. Importing this package registers every one.

Adding a format is one module plus one import line — the same two steps as adding an ATS
to `sources/`, a task to `tasks/`, or a feed to `plugins/`, and the same rule: the format
module is pure and `assemble.py` owns the subprocess.
"""

from .base import (  # noqa: F401
    Edit,
    ResumeFormat,
    for_path,
    formats,
    get_format,
    register,
)
from .assemble import AssemblyFailed, assemble, write_pdf  # noqa: F401
from . import latex  # noqa: F401  (side effect: register())

__all__ = [
    "Edit",
    "ResumeFormat",
    "AssemblyFailed",
    "assemble",
    "write_pdf",
    "for_path",
    "formats",
    "get_format",
    "register",
]
