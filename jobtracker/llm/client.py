"""The only module in this package that touches the network.

Mirrors `fetch.py`'s split: providers are pure, this owns sockets, timeouts, retries
and tracing. It also owns the one behavioural rule that makes the whole pass safe to
enable — **every failure returns None**, and None means the posting stays UNCERTAIN,
which is exactly where it already was. A model that is down, slow, misconfigured, or
returning nonsense costs you the resolution you did not have before; it can never
produce a wrong verdict.

That is why nothing here raises for an unreachable server. Matching correctness must
not depend on an inference server being up.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import requests
from opentelemetry import trace

from .base import Provider

log = logging.getLogger("jobtracker.llm")
tracer = trace.get_tracer("jobtracker.llm")

# Job descriptions run long and the tail is boilerplate — benefits, EEO statements,
# "about us". Level signals ("recent graduate", "0-2 years") live near the top, in the
# requirements section. Truncating keeps the prompt inside any local context window
# without losing what we are reading for.
MAX_DESCRIPTION_CHARS = 6000

LEVEL_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": ["entry", "not_entry", "unclear"]},
        "evidence": {"type": "string"},
    },
    "required": ["level", "evidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You read a software job description and report ONLY the experience level it requires.

Answer with one of:
  entry      — explicitly open to new/recent graduates, or asks for roughly 0-2 years,
               or is labelled new grad / university grad / early career / campus.
  not_entry  — requires more experience than a new graduate has: a minimum number of
               years above 2, or seniority (senior, staff, principal, lead, manager).
  unclear    — the description does not state a level either way.

Rules:
- Judge ONLY experience level. Do not judge whether the role is backend, interesting,
  or a good fit. Something else decides that.
- "unclear" is a correct and useful answer. Prefer it over guessing. A wrong "entry"
  wastes an application; a wrong "not_entry" hides a real job.
- An internship is not entry-level for this purpose; answer not_entry.
- Quote the exact phrase you used as evidence. If there is none, use "unclear" and
  leave evidence empty.
"""


ORDINAL = ["none", "weak", "moderate", "strong"]
RISK = ["low", "medium", "high"]

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "backend_fit": {"type": "string", "enum": ORDINAL},
        "growth": {"type": "string", "enum": ORDINAL},
        "entry_risk": {"type": "string", "enum": RISK},
        "why": {"type": "string"},
    },
    "required": ["backend_fit", "growth", "entry_risk", "why"],
    "additionalProperties": False,
}

# Note what this asks for and what it does not. Three labelled ordinals and a sentence
# — never a score, never a rank, never a comparison. The model judges one posting in
# isolation and has no idea another exists; Python turns these into a number and an
# order, from weights you can read and diff (DESIGN.md §3.2).
#
# Ordinals rather than 0-100 because absolute numeric scores from an LLM cluster in a
# narrow band and shift whenever the prompt or model changes, which would silently
# re-rank the whole queue. A four-point labelled scale is stable across both.
RANK_SYSTEM_PROMPT = """\
You judge how well ONE job posting fits ONE candidate's stated goals.

You are given the candidate's profile and a single job description. Answer four
questions about THIS posting only. You are not ranking, comparing, or choosing —
another system does that with your answers.

  backend_fit — how well the ACTUAL WORK described matches the candidate's target
                roles. Judge the day-to-day engineering described in the body, not
                the job title and not the company's reputation.
                  strong   — the core work is squarely what they are looking for
                  moderate — adjacent or partially on-target
                  weak     — mostly off-target with some overlap
                  none     — a different discipline entirely

  growth      — evidence in the description of learning and mentorship: named senior
                engineers or mentors, structured onboarding, a new-grad or early-career
                program, explicit teaching, breadth of systems they would touch.
                  strong   — the posting concretely describes these
                  moderate — implied but not stated
                  weak     — little sign either way
                  none     — the posting reads as "deliver alone, no support"

  entry_risk  — the risk that this is NOT actually open to a new graduate, despite
                its title. Look for required years of experience, "proven track
                record", ownership of a whole system, or on-call leadership.
                  low      — clearly open to new grads
                  medium   — ambiguous
                  high     — reads as needing real prior experience

  why         — ONE sentence, under 200 characters, citing something specific from
                the description. Not a summary of the role; the reason for your
                judgment. This is shown to the candidate, so make it useful.

Judge only what the description says. If it is vague, say so through the labels —
"moderate" and "medium" are honest answers and are preferred over guessing.
"""


@dataclass(frozen=True)
class RankJudgment:
    backend_fit: str
    growth: str
    entry_risk: str
    why: str = ""


@dataclass(frozen=True)
class LevelVerdict:
    level: str  # 'entry' | 'not_entry' | 'unclear'
    evidence: str = ""

    @property
    def usable(self) -> bool:
        return self.level in ("entry", "not_entry")


class LlmClient:
    """A configured local inference endpoint. Construct once per run."""

    def __init__(
        self,
        provider: Provider,
        base_url: str,
        model: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._model = model
        self._session = requests.Session()

    # -- lifecycle -----------------------------------------------------------------
    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "LlmClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- model discovery -----------------------------------------------------------
    def _served_models(self) -> Optional[list[str]]:
        """Ask the server what it is serving. None means it could not be reached."""
        url = self.provider.models_url(self.base_url)
        if not url:
            return None
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return self.provider.parse_models(resp.json())
        except Exception as exc:  # noqa: BLE001 — unreachable is a normal state here
            log.debug("model discovery failed at %s: %s", url, exc)
            return None

    def resolve_model(self) -> Optional[str]:
        """The model name to send, discovered from the server if not configured.

        vLLM rejects a request naming a model it is not serving, so guessing is worse
        than asking. Cached: one call per run, not per posting.
        """
        if self._model:
            return self._model
        names = self._served_models()
        if not names:
            return None
        self._model = names[0]
        if len(names) > 1:
            log.info("server offers %d models; using %r", len(names), self._model)
        return self._model

    def probe(self) -> bool:
        """Confirm the server is actually reachable before processing the queue.

        This deliberately contacts the server even when a model name was configured.
        Short-circuiting on a configured name would let `probe` report "ready" against
        a box that is switched off, and the failure would then surface as one silent
        miss per posting for the rest of the run — precisely what this exists to stop.
        """
        names = self._served_models()
        if names is None:
            log.warning(
                "llm unreachable at %s — the uncertain queue will be left as-is",
                self.base_url,
            )
            return False
        if not names:
            log.warning("%s is up but serving no models", self.base_url)
            return False

        if self._model and self._model not in names:
            # Not fatal — the server is the authority and may accept aliases — but it
            # is the most likely explanation if every call then fails.
            log.warning(
                "configured model %r is not in the server's list (%s)",
                self._model, ", ".join(names[:3]),
            )
        elif not self._model:
            self._model = names[0]

        log.info(
            "llm ready: %s at %s (model=%s)", self.provider.name, self.base_url, self._model
        )
        return True

    # -- classification ------------------------------------------------------------
    def classify_level(self, title: str, description: str) -> Optional[LevelVerdict]:
        """Read a description for its experience level. None on any failure."""
        if not description or not description.strip():
            return None
        model = self.resolve_model()
        if not model:
            return None

        body = self.provider.build_request(
            model=model,
            system=SYSTEM_PROMPT,
            user=f"Job title: {title}\n\nJob description:\n{description[:MAX_DESCRIPTION_CHARS]}",
            schema=LEVEL_SCHEMA,
            schema_name="level",
        )

        with tracer.start_as_current_span("llm.classify") as span:
            # Low cardinality only: the provider and model are bounded, titles are not.
            span.set_attribute("llm.provider", self.provider.name)
            span.set_attribute("llm.model", model)
            try:
                resp = self._session.post(
                    self.provider.chat_url(self.base_url), json=body, timeout=self.timeout
                )
                resp.raise_for_status()
                text = self.provider.parse_response(resp.json())
            except Exception as exc:  # noqa: BLE001
                log.debug("llm call failed for %r: %s", title[:40], exc)
                span.set_attribute("llm.outcome", "error")
                return None

            verdict = _parse_verdict(text)
            span.set_attribute("llm.outcome", verdict.level if verdict else "unparseable")
            return verdict


    # -- ranking -------------------------------------------------------------------
    def judge_posting(
        self, title: str, description: str, profile_prose: str
    ) -> Optional["RankJudgment"]:
        """Judge one posting against the candidate profile. None on any failure.

        Same contract as `classify_level`: None means "no new information", and the
        posting simply stays unranked and out of the top 3 rather than being scored
        wrongly. A model that is down, slow, or nonsensical costs you the ranking you
        did not have before; it can never invent an order.
        """
        if not description or not description.strip():
            return None
        model = self.resolve_model()
        if not model:
            return None

        body = self.provider.build_request(
            model=model,
            system=RANK_SYSTEM_PROMPT,
            user=(
                f"CANDIDATE PROFILE\n{profile_prose}\n\n"
                f"---\n\nJob title: {title}\n\n"
                f"Job description:\n{description[:MAX_DESCRIPTION_CHARS]}"
            ),
            schema=RANK_SCHEMA,
            schema_name="ranking",
        )

        with tracer.start_as_current_span("llm.judge") as span:
            span.set_attribute("llm.provider", self.provider.name)
            span.set_attribute("llm.model", model)
            try:
                resp = self._session.post(
                    self.provider.chat_url(self.base_url), json=body, timeout=self.timeout
                )
                resp.raise_for_status()
                text = self.provider.parse_response(resp.json())
            except Exception as exc:  # noqa: BLE001
                log.debug("llm judge failed for %r: %s", title[:40], exc)
                span.set_attribute("llm.outcome", "error")
                return None

            judgment = _parse_judgment(text)
            span.set_attribute("llm.outcome", "ok" if judgment else "unparseable")
            return judgment


def _parse_judgment(text: Optional[str]) -> Optional["RankJudgment"]:
    """Validate the model's JSON. Anything off-enum is treated as no answer.

    Same reasoning as `_parse_verdict`: constrained decoding should make this total,
    but a server that silently ignores `response_format` would otherwise let prose
    through as a judgment — the exact regression that made `resolve` a no-op for a
    month. Rejecting here means such a server produces no ranking rather than a
    fabricated one.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fit, growth = data.get("backend_fit"), data.get("growth")
    risk = data.get("entry_risk")
    if fit not in ORDINAL or growth not in ORDINAL or risk not in RISK:
        return None
    return RankJudgment(
        backend_fit=fit,
        growth=growth,
        entry_risk=risk,
        why=str(data.get("why") or "")[:300],
    )


def _parse_verdict(text: Optional[str]) -> Optional[LevelVerdict]:
    """Validate the model's JSON. Anything unexpected is treated as no answer.

    Constrained decoding should make this total, but a provider that silently ignores
    `guided_json` would otherwise let free-form prose through as a verdict.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    level = data.get("level")
    if level not in ("entry", "not_entry", "unclear"):
        return None
    return LevelVerdict(level=level, evidence=str(data.get("evidence") or "")[:300])
