"""LLM provider interface + registry.

Deliberately the same shape as `jobtracker/sources/base.py`, including the rule that
matters most: **adapters are pure**. A provider builds a request body and parses a
response payload; it never opens a socket. All I/O lives in `llm/client.py`, the way
all ATS I/O lives in `fetch.py`. That is what makes providers testable against a
recorded payload with no network and no mocking of HTTP internals.

Adding a provider is one file plus one import in `__init__.py` — same as adding an ATS.

Everything here is local-first. There is no hosted-API provider and no API-key handling
anywhere in this package; a provider is an address you point at.
"""

from __future__ import annotations

from typing import Optional


class Provider:
    """One local inference server's wire format."""

    name: str = ""

    # -- request construction ------------------------------------------------------
    def chat_url(self, base_url: str) -> str:
        """Full URL for a chat/completion call."""
        raise NotImplementedError

    def models_url(self, base_url: str) -> Optional[str]:
        """Where to discover served model names, if the provider exposes that.

        Returning a URL is what lets the user configure only an address: the client
        asks the server what it is serving rather than making them repeat it.
        """
        return None

    def parse_models(self, raw: object) -> list[str]:
        return []

    def build_request(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int = 512,
    ) -> dict:
        """Build the JSON body. `schema` constrains the output to that JSON Schema."""
        raise NotImplementedError

    # -- response parsing ----------------------------------------------------------
    def parse_response(self, raw: object) -> Optional[str]:
        """Pull the assistant's text out of a response payload, or None if absent."""
        raise NotImplementedError


_REGISTRY: dict[str, Provider] = {}


def register(provider: Provider) -> Provider:
    _REGISTRY[provider.name] = provider
    return provider


def get_provider(name: str) -> Optional[Provider]:
    return _REGISTRY.get(name)


def provider_names() -> list[str]:
    return sorted(_REGISTRY)
