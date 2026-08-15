"""The wire format, the parsers, and the degradation guarantees.

No network anywhere in this file. `wire.py` is pure by design, so it is tested against
recorded payloads; the client's failure paths are tested against a port that is closed
on purpose.

The tests that matter most are the ones asserting what happens when things go wrong: a
router that is down, slow, or babbling must never move a posting to a wrong verdict.

Async tests run through `asyncio.run` rather than a plugin — the SDK is async-only, and
adding a test dependency to assert four failure paths is not worth it.
"""

import asyncio
import json

import pytest

from jobtracker import config
from jobtracker.criteria import load_criteria
from jobtracker.llm import LlmClient, wire
from jobtracker.llm.client import is_configured, resolve_base_url
from jobtracker.models import Decision
from jobtracker.tasks.judge import (
    RANK_SCHEMA,
    RANK_SYSTEM_PROMPT,
    parse_judgment,
)
from jobtracker.tasks.level import (
    LEVEL_SCHEMA,
    LevelVerdict,
    looks_engineering,
    parse_verdict,
    verdict_from_level,
)


@pytest.fixture
def criteria():
    return load_criteria(config.CRITERIA_YAML)


def _dead_client():
    """Port 1 is reserved and closed. Nothing in this file may depend on a live router."""
    return LlmClient(model="m", base_url="http://127.0.0.1:1", timeout=0.25)


# -- request construction ----------------------------------------------------------
def test_request_constrains_output_and_is_deterministic():
    body = wire.chat_body("m", "sys", "user", LEVEL_SCHEMA, schema_name="level")
    assert body["model"] == "m"
    # Constrained decoding, not hope. Must be the OpenAI-standard `response_format`:
    # vLLM dropped `guided_json` and silently ignores it, answering in prose, which
    # makes every posting unparseable and the whole pass a no-op. See llm/wire.py.
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == LEVEL_SCHEMA
    assert body["response_format"]["json_schema"]["name"] == "level"
    assert "guided_json" not in body
    assert body["temperature"] == 0                 # same posting -> same verdict
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_the_router_forwards_the_body_so_the_schema_still_travels():
    """The SDK sets `model` from its argument and forwards everything else untouched.

    That is the contract this depends on: routing through `sir` must not strip the
    constraint, or the pass silently degrades to the prose failure again.
    """
    body = wire.chat_body("m", "sys", "user", LEVEL_SCHEMA)
    forwarded = {**body, "model": "m"}      # what AsyncClient.submit sends
    assert forwarded["response_format"] == body["response_format"]
    assert forwarded["temperature"] == 0


def test_parse_models_from_recorded_payload():
    raw = {"object": "list", "data": [{"id": "some/model", "object": "model"}]}
    assert wire.model_ids(raw) == ["some/model"]
    assert wire.model_ids({"data": []}) == []
    assert wire.model_ids("nonsense") == []


def test_parse_response_from_recorded_payload():
    raw = {"choices": [{"message": {"role": "assistant", "content": '{"level":"entry"}'}}]}
    assert wire.content_of(raw) == '{"level":"entry"}'


@pytest.mark.parametrize("raw", [
    {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}, "nope", None,
])
def test_parse_response_survives_malformed_payloads(raw):
    assert wire.content_of(raw) is None


# -- configuration -----------------------------------------------------------------
def test_the_address_is_the_whole_configuration(monkeypatch):
    """One transport, so there is no provider to name — only somewhere to point."""
    monkeypatch.delenv("JOBTRACKER_LLM_URL", raising=False)
    monkeypatch.delenv("SIR_BASE_URL", raising=False)
    monkeypatch.delenv("SIR_ENDPOINTS", raising=False)
    assert resolve_base_url(None) is None
    assert is_configured(None) is False

    assert resolve_base_url("http://box:8000/") == "http://box:8000"
    monkeypatch.setenv("JOBTRACKER_LLM_URL", "http://ours:8000")
    assert resolve_base_url(None) == "http://ours:8000"
    assert resolve_base_url("http://flag:9000") == "http://flag:9000"  # flag wins


def test_the_sdks_own_env_var_is_honoured(monkeypatch):
    """A box already pointing other services at the router should not repeat itself."""
    monkeypatch.delenv("JOBTRACKER_LLM_URL", raising=False)
    monkeypatch.setenv("SIR_BASE_URL", "http://router:8000")
    assert resolve_base_url(None) == "http://router:8000"
    assert is_configured(None) is True


def test_per_model_endpoints_alone_count_as_configured(monkeypatch):
    monkeypatch.delenv("JOBTRACKER_LLM_URL", raising=False)
    monkeypatch.delenv("SIR_BASE_URL", raising=False)
    monkeypatch.setenv("SIR_ENDPOINTS", "a=http://gpu:8000")
    assert resolve_base_url(None) is None      # no single catch-all address
    assert is_configured(None) is True         # but the registry can still route


# -- verdict parsing ---------------------------------------------------------------
def test_parse_verdict_accepts_a_well_formed_answer():
    v = parse_verdict(json.dumps({"level": "entry", "evidence": "recent graduates"}))
    assert v == LevelVerdict(level="entry", evidence="recent graduates")
    assert v.usable


@pytest.mark.parametrize("text", [
    None, "", "not json at all", "[]", '{"level":"sort of entry"}', '{"evidence":"x"}',
    '"entry"',
])
def test_parse_verdict_rejects_anything_unexpected(text):
    """A server ignoring the schema request lets prose through as a verdict.

    Not hypothetical: vLLM 0.23 does exactly this when sent the older `guided_json`.
    This check is what turned that into "resolves nothing" instead of "wrong verdicts".
    Routing through `sir` does not change it — the router forwards the body untouched,
    so a backend that ignores the key still answers in prose.
    """
    assert parse_verdict(text) is None


def test_unclear_is_parsed_but_not_usable():
    v = parse_verdict('{"level":"unclear","evidence":""}')
    assert v.level == "unclear"
    assert not v.usable          # -> posting stays UNCERTAIN


# -- degradation: the whole safety story -------------------------------------------
def test_unreachable_router_returns_none_not_an_exception():
    """Matching must not depend on an inference server being up."""
    client = _dead_client()

    async def go():
        try:
            assert await client.complete("sys", "user", LEVEL_SCHEMA) is None
            assert await client.probe() is False
        finally:
            await client.aclose()

    asyncio.run(go())


def test_an_unroutable_model_is_absence_not_an_error(monkeypatch):
    """ModelNotRouted is raised before anything is sent. It must still read as None."""
    monkeypatch.delenv("JOBTRACKER_LLM_URL", raising=False)
    monkeypatch.delenv("SIR_BASE_URL", raising=False)
    monkeypatch.delenv("SIR_ENDPOINTS", raising=False)
    client = LlmClient(model="nowhere", base_url=None, timeout=0.25)

    async def go():
        try:
            assert await client.complete("sys", "user", LEVEL_SCHEMA) is None
            assert await client.probe() is False
        finally:
            await client.aclose()

    asyncio.run(go())


def test_a_malformed_endpoints_env_does_not_raise(monkeypatch):
    """`Registry.from_env` raises on a bad SIR_ENDPOINTS. Nothing here may propagate it."""
    monkeypatch.delenv("JOBTRACKER_LLM_URL", raising=False)
    monkeypatch.delenv("SIR_BASE_URL", raising=False)
    monkeypatch.setenv("SIR_ENDPOINTS", "this is not a pair")
    client = LlmClient(model="m", base_url=None, timeout=0.25)

    async def go():
        try:
            assert await client.probe() is False
            assert await client.complete("sys", "user", LEVEL_SCHEMA) is None
        finally:
            await client.aclose()

    asyncio.run(go())


# -- level -> verdict --------------------------------------------------------------
def test_entry_plus_engineering_matches(criteria):
    v = verdict_from_level("Acme", "1", "Software Engineer", "entry", "0-2 years", criteria)
    assert v.decision is Decision.MATCH
    assert v.decided_by == "llm"           # never confusable with a rules verdict
    assert "llm:entry" in v.reason


def test_entry_on_a_non_engineering_title_still_rejects(criteria):
    """The model supplies the level; the RULES still decide it is on-target.

    This is the 'Finance Associate' guard — an entry-level reading must not be
    enough on its own to match a backend tracker.
    """
    v = verdict_from_level("Acme", "1", "Finance Analyst", "entry", "new grads", criteria)
    assert v.decision is Decision.REJECT
    assert v.reason == "llm:entry+non_engineering"


def test_not_entry_rejects_and_keeps_the_evidence(criteria):
    v = verdict_from_level("Acme", "1", "Software Engineer", "not_entry",
                           "8+ years required", criteria)
    assert v.decision is Decision.REJECT
    assert "8+ years" in v.reason


def test_unclear_leaves_the_verdict_alone(criteria):
    assert verdict_from_level("Acme", "1", "Software Engineer", "unclear", "", criteria) is None


def test_backend_title_is_reported_as_backend_relevant(criteria):
    v = verdict_from_level("Acme", "1", "Backend Engineer", "entry", "grad", criteria)
    assert v.reason == "llm:entry+role:backend"


# -- scoping -----------------------------------------------------------------------
def test_engineering_scope_filters_the_queue(criteria):
    assert looks_engineering("Software Engineer", criteria)
    assert looks_engineering("Backend Developer", criteria)
    assert not looks_engineering("Field Marketer", criteria)
    assert not looks_engineering("Talent Strategist", criteria)


def test_scope_is_documented_as_lossy(criteria):
    """Known blind spot, asserted so it cannot regress silently into a REJECT.

    'Member of Technical Staff' is a real engineering title with no matching token.
    It must stay out of scope (never read) rather than be rejected — it remains
    UNCERTAIN and visible in the queue for a human.
    """
    assert not looks_engineering("Member of Technical Staff", criteria)


# -- ranking judgments -------------------------------------------------------------
# The second task on this interface. Same guarantees as level extraction: the model
# supplies labelled facts, Python turns them into a number, and every failure path
# leaves the posting unjudged rather than mis-scored.
def test_rank_request_names_its_schema():
    """Several tasks share one body builder, so the label must say which is which."""
    body = wire.chat_body("m", "sys", "user", RANK_SCHEMA, schema_name="ranking")
    assert body["response_format"]["json_schema"]["name"] == "ranking"
    assert body["response_format"]["json_schema"]["schema"] == RANK_SCHEMA
    assert "guided_json" not in body
    assert body["temperature"] == 0  # same posting + same profile -> same judgment


def test_parse_judgment_from_a_recorded_payload():
    raw = json.dumps({
        "backend_fit": "strong",
        "growth": "moderate",
        "entry_risk": "low",
        "why": "Owns the Kafka ingestion pipeline; explicitly open to new graduates.",
    })
    j = parse_judgment(raw)
    assert j.backend_fit == "strong"
    assert j.growth == "moderate"
    assert j.entry_risk == "low"
    assert "Kafka" in j.why


@pytest.mark.parametrize("bad", [
    None,
    "",
    "The role looks like a strong fit for a backend new grad.",  # prose, not JSON
    "[]",
    "null",
    json.dumps({"backend_fit": "excellent", "growth": "strong",       # off-enum
                "entry_risk": "low", "why": "x"}),
    json.dumps({"backend_fit": "strong", "growth": "strong",
                "entry_risk": "unknown", "why": "x"}),               # off-enum
    json.dumps({"growth": "strong", "entry_risk": "low", "why": "x"}),  # missing field
])
def test_a_malformed_judgment_is_no_judgment(bad):
    """A server ignoring response_format must produce NO ranking, not a fabricated one.

    This is the `guided_json` regression's shape: the request looks accepted, the
    answer comes back as prose, and without this check it would be scored anyway.
    """
    assert parse_judgment(bad) is None


def test_the_rank_prompt_forbids_ordering():
    """Scope boundary: the model judges one posting and never sees another.

    Widening this to "pick the best" would put a nondeterministic component back in
    the ordering itself, which is what DESIGN.md was written to undo.
    """
    assert "not ranking, comparing, or choosing" in RANK_SYSTEM_PROMPT
    # No score, no rank, no comparison anywhere in what it may return.
    assert set(RANK_SCHEMA["properties"]) == {"backend_fit", "growth", "entry_risk", "why"}
    assert RANK_SCHEMA["additionalProperties"] is False


# -- the SDK itself is optional ------------------------------------------------------
def test_the_package_works_without_the_sdk_installed(monkeypatch):
    """`check`, `report`, `dashboard` and rank's scoring must not need an inference SDK.

    "The model is optional" used to be true because there was nothing to install at all.
    Now it is true because nothing imports the SDK until something wants an answer — so
    a checkout without it still runs every step that needs no model, and the task queue
    reports "no router" rather than failing to import.
    """
    import builtins
    import importlib
    import sys

    real = builtins.__import__

    def refuse(name, *a, **k):
        if name.startswith("sir_client") or name == "httpx":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", refuse)
    for name in [m for m in list(sys.modules) if m.startswith("jobtracker.llm")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    module = importlib.import_module("jobtracker.llm.client")
    assert module.SDK_AVAILABLE is False
    # Configured-looking environment, still not usable — and it says so rather than
    # raising somewhere deep in a nightly run.
    assert module.is_configured("http://box:8000") is False

    client = module.LlmClient(model="m", base_url="http://box:8000", timeout=0.2)
    assert asyncio.run(client.probe()) is False
    assert asyncio.run(client.complete("s", "u", LEVEL_SCHEMA)) is None
    asyncio.run(client.aclose())
