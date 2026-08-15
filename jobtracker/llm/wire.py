"""The request body, and how to read the answer out of the response.

Pure: no sockets, no SDK. `client.py` owns the transport, exactly as `fetch.py` owns
ATS transport and the `sources/` adapters own the shapes. That split is what lets the
wire format be tested against a recorded payload with nothing mocked.

The router this talks to forwards a body **exactly as written** and reads only `model`
and `stream` out of it. That is a deliberate refusal on its part: the extras backends
accept (`guided_json` against `json_schema`, `ebnf` against `guided_grammar`) drift with
every release, and a translator in the middle would have to be updated in lockstep with
all of them. So knowledge of what the backend accepts stays here, in the service that
already has it — which is why this module survived the move to the SDK unchanged.
"""

from __future__ import annotations

from typing import Optional


def chat_body(
    model: str,
    system: str,
    user: str,
    schema: dict,
    schema_name: str = "verdict",
    max_tokens: int = 512,
) -> dict:
    """An OpenAI-shaped chat request whose output is constrained to `schema`.

    `model` is included even though the SDK sets it from its own argument — the body is
    what gets recorded in tests, and a body that does not say which model it is for is
    harder to read than one that does. The SDK overwrites it with the same value.
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        # 0 for reproducibility: the same posting must classify the same way on a
        # rerun, or `eval` scores noise instead of the model.
        "temperature": 0,
        # Constrained decoding, via the OpenAI-standard `response_format`.
        #
        # This was `guided_json` + `guided_decoding_backend` until 2026-07-24.
        # Both were dropped from vLLM's request schema, and the failure is silent
        # in the worst way: 0.23.1 accepts a body carrying `guided_json`, ignores
        # it, and answers in prose. The parsers then reject every response, so each
        # posting stays UNCERTAIN — the pass runs nightly, costs a description fetch
        # per posting, and resolves nothing. Verified against vllm-0.23.1rc1 on this
        # box: `guided_json` returned "The experience level required is **8+ years**.";
        # `response_format` returned valid JSON.
        #
        # `response_format` is the portable spelling — OpenAI's own, supported by
        # vLLM since 0.6 and by llama.cpp and Ollama's compat servers.
        #
        # Routing through the router does not make this safer. It forwards the body
        # untouched, so a backend that ignores the key still answers in prose and the
        # parsers are still the only thing standing between that and a fabricated
        # verdict. Check it against the server you actually run.
        #
        # `schema_name` is passed rather than hardcoded because there is more than one
        # task: 'level' for the ambiguity pass, 'ranking' for the nightly rank,
        # 'question_match' for prefill. A stale name is cosmetic on the wire but
        # misleading in a traced request.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        },
    }


def content_of(raw: object) -> Optional[str]:
    """The assistant's text from a chat-completion payload, or None if absent.

    Total by construction: every unexpected shape is None, which the callers read as
    "no answer" rather than as an error. Nothing here raises.
    """
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return str(content) if content else None


def model_ids(raw: object) -> list[str]:
    """Served model tags from a `/v1/models` payload."""
    if not isinstance(raw, dict):
        return []
    return [
        str(m["id"]) for m in raw.get("data", []) if isinstance(m, dict) and m.get("id")
    ]
