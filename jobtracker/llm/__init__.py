"""Local LLM providers. Importing this package registers every provider.

Local-first by design: a provider is an address you point at, and there is no
API-key handling anywhere in this package. Adding one means a new module here plus a
line in the import below — the same two steps as adding an ATS to `sources/`.
"""

from .base import Provider, get_provider, provider_names, register  # noqa: F401
from . import vllm  # noqa: F401  (side effect: register())
from .client import LevelVerdict, LlmClient, RankJudgment  # noqa: F401

__all__ = [
    "Provider",
    "get_provider",
    "provider_names",
    "register",
    "LlmClient",
    "LevelVerdict",
    "RankJudgment",
]
