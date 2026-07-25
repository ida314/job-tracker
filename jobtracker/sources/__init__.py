"""Source adapters. Importing this package registers every ATS in the registry."""

from .base import Source, api_sources, get_source, register  # noqa: F401
from . import greenhouse, ashby, lever, aggregator  # noqa: F401  (side effect: register())

__all__ = ["Source", "get_source", "api_sources", "register"]
