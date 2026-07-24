"""vLLM, via its OpenAI-compatible server.

  chat    POST {base}/v1/chat/completions
  models  GET  {base}/v1/models

Two things make vLLM a good first provider here, and both are worth preserving if a
second one is added:

* **No SDK.** The wire format is plain JSON over HTTP, so `requests` — already a
  dependency for the ATS fetches — is the entire client. Nothing new to install means
  the "LLM pass is optional" claim is structurally true rather than aspirational.

* **Constrained decoding.** `guided_json` makes the server emit text that conforms to
  a JSON Schema by restricting sampling, so malformed output is not a failure mode we
  have to parse around. A hosted API would give us a validated response only if the
  vendor implemented it; here it is a property of the local decoder.

The model name is discovered from /v1/models rather than configured. vLLM serves one
model and rejects a request naming a different one, so asking the server what it has
is both more robust and matches how this is meant to be used: point it at an address.
"""

from __future__ import annotations

from typing import Optional

from .base import Provider, register


class VLLM(Provider):
    name = "vllm"

    def chat_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/v1/chat/completions"

    def models_url(self, base_url: str) -> Optional[str]:
        return f"{base_url.rstrip('/')}/v1/models"

    def parse_models(self, raw: object) -> list[str]:
        if not isinstance(raw, dict):
            return []
        return [
            str(m["id"])
            for m in raw.get("data", [])
            if isinstance(m, dict) and m.get("id")
        ]

    def build_request(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int = 512,
    ) -> dict:
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
            # vLLM's constrained decoding. Unknown keys are ignored by servers that
            # do not implement it, which degrades to "parse the JSON and hope" rather
            # than to an error — and the caller validates the parse either way.
            "guided_json": schema,
            "guided_decoding_backend": "xgrammar",
        }

    def parse_response(self, raw: object) -> Optional[str]:
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


register(VLLM())
