"""The model transport: one router, addressed rather than configured.

Local-first by design, and there is still no API-key handling anywhere in this package.
A router is an address you point at.

This used to carry a `Provider` interface and a registry so a second wire format could
be slotted in. `sir` — the inference router — is that indirection now, so the registry
was removed on 2026-08-13 rather than kept as a second dispatch layer in front of one
that already exists. What remains is the split that mattered: `wire.py` is pure and
knows the body shape, `client.py` owns every socket.
"""

from .client import (  # noqa: F401
    SDK_AVAILABLE,
    DEFAULT_TIMEOUT,
    MAX_DESCRIPTION_CHARS,
    LlmClient,
    is_configured,
    resolve_base_url,
)
from .wire import chat_body, content_of, model_ids  # noqa: F401

__all__ = [
    "LlmClient",
    "SDK_AVAILABLE",
    "MAX_DESCRIPTION_CHARS",
    "DEFAULT_TIMEOUT",
    "is_configured",
    "resolve_base_url",
    "chat_body",
    "content_of",
    "model_ids",
]
