"""The only module in this package that touches the network.

Transport moved to the `sir-client` SDK on 2026-08-13. What did *not* move is the one
behavioural rule that makes every model pass safe to enable — **every failure returns
None**, and None means the caller learned nothing, which leaves the posting exactly
where it already was. A model that is down, slow, misconfigured, swapped out, or
returning nonsense costs you the answer you did not have before; it can never produce a
wrong one.

That is why nothing here raises for an unreachable server, and why the `except` clauses
are as wide as they are. Pipeline correctness must not depend on an inference server
being up.

Why the SDK replaced the old `Provider` registry
------------------------------------------------
The registry existed so a second wire format could be added without touching the client.
The router *is* that indirection now: it presents one OpenAI-compatible endpoint, decides
which model is resident, and forwards bodies untouched. Keeping a second dispatch layer
in front of it would have been two abstractions doing one job. What survives is
`wire.py`, because knowing what the backend accepts is exactly the knowledge the router
refuses to hold (see its `registry.py`).

The SDK is async-only, on the reasoning that a sync wrapper you cannot call from a loop
is a footgun. That is why `tasks/runner.py` is async and why `browser.py` — Playwright's
sync API, which must not run inside a loop — stays in a separate module.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from opentelemetry import trace

from . import wire

# The SDK and its http client are imported defensively so that a checkout without them
# still runs everything that needs no model — `check`, `rematch`, `report`, `dashboard`,
# and `rank`'s scoring phase. Absent, `is_configured()` is False and the task queue
# reports itself as having no router, which is the same state as a router that is down
# and is handled by the same code path.
#
# This is what keeps "the model is optional" structurally true rather than aspirational.
# It used to be true because there was nothing to install at all; now it is true because
# nothing imports the SDK until something actually wants an answer.
try:  # pragma: no cover - exercised by the absence, not the presence
    import httpx
    from sir_client import AsyncClient, Registry, SirClientError

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    AsyncClient = Registry = None  # type: ignore[assignment]

    class SirClientError(Exception):  # type: ignore[no-redef]
        """Stand-in so the except clauses below stay well-formed without the SDK."""

    SDK_AVAILABLE = False

log = logging.getLogger("jobtracker.llm")
tracer = trace.get_tracer("jobtracker.llm")

# Job descriptions run long and the tail is boilerplate — benefits, EEO statements,
# "about us". Level signals ("recent graduate", "0-2 years") live near the top, in the
# requirements section. Truncating keeps the prompt inside any local context window
# without losing what we are reading for.
MAX_DESCRIPTION_CHARS = 6000

# How long the SDK is given to produce a completion. Generous because the router may
# have to evict and load a model before this request starts generating at all — the
# whole reason it hands back a job to poll instead of holding a socket open.
DEFAULT_TIMEOUT = 180.0

ENV_URL = "JOBTRACKER_LLM_URL"
ENV_MODEL = "JOBTRACKER_LLM_MODEL"


def resolve_base_url(cli_url: Optional[str] = None) -> Optional[str]:
    """Where the router is, from the flag, our env var, or the SDK's own.

    `$SIR_BASE_URL` is honoured so a machine that already points other services at the
    router does not have to repeat itself. `$SIR_ENDPOINTS` alone is also workable — it
    routes per model — and returns None here, which the client handles by letting the
    SDK's registry answer.
    """
    for candidate in (cli_url, os.environ.get(ENV_URL), os.environ.get("SIR_BASE_URL")):
        if candidate and candidate.strip():
            return candidate.strip().rstrip("/")
    return None


def is_configured(cli_url: Optional[str] = None) -> bool:
    """True if there is any address to talk to, and anything to talk to it with."""
    if not SDK_AVAILABLE:
        return False
    return bool(resolve_base_url(cli_url) or os.environ.get("SIR_ENDPOINTS"))


class LlmClient:
    """A configured route to the inference router. Construct once per run.

    Async, and safe to share across concurrent tasks — the SDK's client is, and the
    only mutable state here is the resolved model name, which is written once.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        sdk: Optional[AsyncClient] = None,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._model = model or os.environ.get(ENV_MODEL) or None
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout = timeout
        # An injected SDK client (or http client) belongs to the caller; that is how the
        # tests point this at an in-process app instead of a socket.
        self._sdk = sdk
        self._http = http
        self._owns_http = http is None
        self._owns_sdk = sdk is None

    # -- lifecycle -----------------------------------------------------------------
    def _client(self) -> Optional[AsyncClient]:
        """Build the SDK client on first use. None if it cannot be configured.

        Lazy so that constructing an `LlmClient` reads no environment and opens no
        socket — `work --dry-run` builds one and must stay inert.
        """
        if self._sdk is not None:
            return self._sdk
        if not SDK_AVAILABLE:
            log.warning(
                "sir-client is not installed — no model work will run. "
                "pip install -e ../stupid-inference-router/clients/python"
            )
            return None
        try:
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=self.timeout)
            registry = (
                Registry(default=self.base_url)
                if self.base_url
                else Registry.from_env()  # raises on a malformed $SIR_ENDPOINTS
            )
            self._sdk = AsyncClient(registry, http=self._http, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 — misconfiguration is a normal state
            log.warning("llm client could not be configured: %s", exc)
            return None
        return self._sdk

    async def aclose(self) -> None:
        if self._sdk is not None and self._owns_sdk:
            await self._sdk.aclose()
        if self._http is not None and self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "LlmClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- model discovery -----------------------------------------------------------
    def _discovery_urls(self) -> list[str]:
        if self.base_url:
            return [self.base_url]
        sdk = self._client()
        if sdk is None:
            return []
        urls: list[str] = []
        for tag in sdk.registry.models:
            url = sdk.registry.resolve(tag)
            if url not in urls:
                urls.append(url)
        return urls

    async def _served_models(self) -> Optional[list[str]]:
        """Ask the router what it can serve. None means it could not be reached."""
        sdk = self._client()
        if sdk is None:
            return None
        urls = self._discovery_urls()
        if not urls:
            return None
        if self._http is None:
            # Only reachable when an SDK client was injected without an http client.
            if httpx is None:
                return None
            self._http = httpx.AsyncClient(timeout=self.timeout)
        names: list[str] = []
        for url in urls:
            try:
                resp = await self._http.get(f"{url}/v1/models", timeout=self.timeout)
                if resp.status_code != 200:
                    log.debug("model discovery at %s returned %s", url, resp.status_code)
                    return None
                names.extend(wire.model_ids(resp.json()))
            except Exception as exc:  # noqa: BLE001 — unreachable is a normal state
                log.debug("model discovery failed at %s: %s", url, exc)
                return None
        return names

    async def resolve_model(self) -> Optional[str]:
        """The model tag to send, discovered from the router if not configured.

        The router rejects a tag it does not serve, so asking beats guessing. Cached:
        one call per run, not per unit of work.
        """
        if self._model:
            return self._model
        names = await self._served_models()
        if not names:
            return None
        self._model = names[0]
        if len(names) > 1:
            log.info("router offers %d models; using %r", len(names), self._model)
        return self._model

    async def probe(self) -> bool:
        """Confirm the router is actually reachable before working the queue.

        This deliberately contacts the server **even when a model tag was configured**.
        Short-circuiting on a configured name would let `probe` report "ready" against a
        box that is switched off, and the failure would then surface as one silent miss
        per unit for the rest of the run — precisely what this exists to stop.
        """
        names = await self._served_models()
        if names is None:
            log.warning(
                "llm unreachable at %s — the queue will be left as-is",
                self.base_url or "the configured endpoints",
            )
            return False
        if not names:
            log.warning("%s is up but serving no models", self.base_url)
            return False

        if self._model and self._model not in names:
            # Not fatal — the router may accept an alias the models list does not show —
            # but it is the likeliest explanation if every call then fails.
            log.warning(
                "configured model %r is not in the router's list (%s)",
                self._model,
                ", ".join(names[:3]),
            )
        elif not self._model:
            self._model = names[0]

        log.info("llm ready: %s (model=%s)", self.base_url or "via registry", self._model)
        return True

    # -- the one call ----------------------------------------------------------------
    async def complete(
        self,
        system: str,
        user: str,
        schema: dict,
        schema_name: str = "verdict",
        idempotency_key: Optional[str] = None,
        max_tokens: int = 512,
    ) -> Optional[str]:
        """Ask one schema-constrained question. None on any failure whatsoever.

        This is the whole model interface. Every task is a system prompt, a schema, and
        a parser; nothing above this layer knows there is an HTTP request involved.
        """
        sdk = self._client()
        if sdk is None:
            return None
        model = await self.resolve_model()
        if not model:
            return None

        body = wire.chat_body(
            model=model,
            system=system,
            user=user,
            schema=schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
        )

        with tracer.start_as_current_span("llm.complete") as span:
            # Low cardinality only: the model and the schema name are bounded, the
            # postings they are asked about are not.
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.schema", schema_name)
            try:
                # `resubmit_on_loss` needs the idempotency key to do anything at all —
                # the SDK silently stays at one attempt without it. With both, a router
                # restart mid-flight costs one retry instead of one lost answer.
                completion: Any = await sdk.run(
                    model,
                    body,
                    timeout=self.timeout,
                    idempotency_key=idempotency_key,
                    resubmit_on_loss=bool(idempotency_key),
                )
            except SirClientError as exc:
                # ModelNotRouted, TransportError, JobLost, JobFailed, JobCancelled,
                # RequestTimeout — every one of them means "no answer", never "wrong
                # answer", so they are all handled identically.
                log.debug("llm call failed (%s): %s", type(exc).__name__, exc)
                span.set_attribute("llm.outcome", "error")
                return None
            except Exception as exc:  # noqa: BLE001 — httpx errors are not wrapped
                log.debug("llm call failed: %s", exc)
                span.set_attribute("llm.outcome", "error")
                return None

            text = wire.content_of(completion)
            span.set_attribute("llm.outcome", "ok" if text else "empty")
            return text
