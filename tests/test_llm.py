"""Local LLM providers, the level→verdict mapping, and the degradation guarantees.

No network anywhere in this file. Providers are pure by design, so they are tested
against recorded payloads; the client's failure paths are tested against a port that
is closed on purpose.

The tests that matter most are the ones asserting what happens when things go wrong:
a model that is down, slow, or babbling must never move a posting to a wrong verdict.
"""

import json

import pytest

from jobtracker import config
from jobtracker.criteria import load_criteria
from jobtracker.llm import LlmClient, get_provider, provider_names
from jobtracker.llm.client import LEVEL_SCHEMA, LevelVerdict, _parse_verdict
from jobtracker.models import Decision
from jobtracker.resolve import looks_engineering, verdict_from_level


@pytest.fixture
def criteria():
    return load_criteria(config.CRITERIA_YAML)


@pytest.fixture
def vllm():
    return get_provider("vllm")


# -- registry ----------------------------------------------------------------------
def test_registry_mirrors_the_sources_pattern(vllm):
    assert vllm is not None and vllm.name == "vllm"
    assert "vllm" in provider_names()
    assert get_provider("definitely-not-a-provider") is None


# -- vLLM request construction -----------------------------------------------------
def test_request_constrains_output_and_is_deterministic(vllm):
    body = vllm.build_request("m", "sys", "user", LEVEL_SCHEMA)
    assert body["model"] == "m"
    # Constrained decoding, not hope. Must be the OpenAI-standard `response_format`:
    # vLLM dropped `guided_json` and silently ignores it, answering in prose, which
    # makes every posting unparseable and the whole pass a no-op. See vllm.py.
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == LEVEL_SCHEMA
    assert "guided_json" not in body
    assert body["temperature"] == 0                 # same posting -> same verdict
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_urls(vllm):
    assert vllm.chat_url("http://box:8000/") == "http://box:8000/v1/chat/completions"
    assert vllm.models_url("http://box:8000") == "http://box:8000/v1/models"


def test_parse_models_from_recorded_payload(vllm):
    raw = {"object": "list", "data": [{"id": "some/model", "object": "model"}]}
    assert vllm.parse_models(raw) == ["some/model"]
    assert vllm.parse_models({"data": []}) == []
    assert vllm.parse_models("nonsense") == []


def test_parse_response_from_recorded_payload(vllm):
    raw = {"choices": [{"message": {"role": "assistant", "content": '{"level":"entry"}'}}]}
    assert vllm.parse_response(raw) == '{"level":"entry"}'


@pytest.mark.parametrize("raw", [
    {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}, "nope", None,
])
def test_parse_response_survives_malformed_payloads(vllm, raw):
    assert vllm.parse_response(raw) is None


# -- verdict parsing ---------------------------------------------------------------
def test_parse_verdict_accepts_a_well_formed_answer():
    v = _parse_verdict(json.dumps({"level": "entry", "evidence": "recent graduates"}))
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
    """
    assert _parse_verdict(text) is None


def test_unclear_is_parsed_but_not_usable():
    v = _parse_verdict('{"level":"unclear","evidence":""}')
    assert v.level == "unclear"
    assert not v.usable          # -> posting stays UNCERTAIN


# -- degradation: the whole safety story -------------------------------------------
def test_unreachable_server_returns_none_not_an_exception(vllm):
    """Port 1 is reserved and closed. Matching must not depend on a model being up."""
    client = LlmClient(vllm, "http://127.0.0.1:1", model="m", timeout=0.25)
    assert client.classify_level("Software Engineer", "Some description") is None
    assert client.probe() is False
    client.close()


def test_empty_description_is_not_sent(vllm):
    """No description means nothing to read — do not burn a call to find that out."""
    client = LlmClient(vllm, "http://127.0.0.1:1", model="m", timeout=0.25)
    assert client.classify_level("Software Engineer", "") is None
    assert client.classify_level("Software Engineer", "   ") is None
    client.close()


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
def test_rank_request_names_its_schema_and_carries_the_profile(vllm):
    """Two tasks now share build_request, so the schema label must say which is which."""
    from jobtracker.llm.client import RANK_SCHEMA

    body = vllm.build_request("m", "sys", "user", RANK_SCHEMA, schema_name="ranking")
    assert body["response_format"]["json_schema"]["name"] == "ranking"
    assert body["response_format"]["json_schema"]["schema"] == RANK_SCHEMA
    assert "guided_json" not in body
    assert body["temperature"] == 0  # same posting + same profile -> same judgment


def test_parse_judgment_from_a_recorded_payload():
    from jobtracker.llm.client import _parse_judgment

    raw = json.dumps({
        "backend_fit": "strong",
        "growth": "moderate",
        "entry_risk": "low",
        "why": "Owns the Kafka ingestion pipeline; explicitly open to new graduates.",
    })
    j = _parse_judgment(raw)
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
    from jobtracker.llm.client import _parse_judgment

    assert _parse_judgment(bad) is None


def test_judge_against_an_unreachable_server_returns_none(vllm):
    client = LlmClient(vllm, "http://127.0.0.1:1", model="m", timeout=0.25)
    assert client.judge_posting("Software Engineer", "A description", "profile") is None
    client.close()


def test_judge_does_not_call_out_for_an_empty_description(vllm):
    client = LlmClient(vllm, "http://127.0.0.1:1", model="m", timeout=0.25)
    assert client.judge_posting("Software Engineer", "", "profile") is None
    assert client.judge_posting("Software Engineer", "   ", "profile") is None
    client.close()


def test_the_rank_prompt_forbids_ordering():
    """Scope boundary: the model judges one posting and never sees another.

    Widening this to "pick the best" would put a nondeterministic component back in
    the ordering itself, which is what DESIGN.md was written to undo.
    """
    from jobtracker.llm.client import RANK_SCHEMA, RANK_SYSTEM_PROMPT

    assert "not ranking, comparing, or choosing" in RANK_SYSTEM_PROMPT
    # No score, no rank, no comparison anywhere in what it may return.
    assert set(RANK_SCHEMA["properties"]) == {"backend_fit", "growth", "entry_risk", "why"}
    assert RANK_SCHEMA["additionalProperties"] is False
