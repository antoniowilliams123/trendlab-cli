"""Capability claims: the harness makes the agent try before it says it cannot (C15).

The live failure this pins down: asked for a weather forecast with ``web_search`` and
``web_fetch`` on the tool list, the agent answered "I have no weather API access" without
calling either. A limitation is a claim about the harness, so the harness checks it against
the tool list before it can end a run.
"""

from pathlib import Path

from trendlab.agent.capability import detect_unattempted_refusal, unattempted_refusals
from trendlab.agent.evaluator import CompletionEvaluator, EvidenceSummary
from trendlab.agent.tasks import Plan
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent

REFUSAL = "I cannot reach the web and have no weather API access, so I can't check the forecast."
CATALOG = ["read_file", "web_search", "web_fetch", "shell"]


# -- the detector -----------------------------------------------------------------------------


def test_refusal_phrases_are_detected():
    assert detect_unattempted_refusal([], "I don't have access to a weather API.")
    assert detect_unattempted_refusal([], "My tools don't include a browser.")
    assert detect_unattempted_refusal([], "There is no way for me to check that.")


def test_a_plain_answer_is_not_a_refusal():
    assert not detect_unattempted_refusal([], "Here is the summary of src/app.py.")
    assert not detect_unattempted_refusal([], "It is 72F and sunny in Atlanta.")
    assert not detect_unattempted_refusal("", "")  # nothing said yet


def test_a_policy_refusal_is_not_a_capability_claim():
    # safety refusals must not be nudged into "try a tool first"
    assert not detect_unattempted_refusal([], "I can't help with deleting your home directory.")
    assert not detect_unattempted_refusal([], "I won't reveal the API key, and I cannot do that.")


def test_a_tried_lookup_tool_settles_the_claim():
    assert not detect_unattempted_refusal(["web_search"], REFUSAL)
    assert unattempted_refusals(REFUSAL, tools=CATALOG, attempted=["web_search"]) == []


def test_refusal_with_no_attempt_is_flagged_with_the_tools_it_skipped():
    [claim] = unattempted_refusals(REFUSAL, tools=CATALOG, attempted=[])
    assert "never tried" in claim
    assert "web_search" in claim and "web_fetch" in claim


def test_no_lookup_tool_on_offer_means_the_limitation_is_real():
    assert unattempted_refusals(REFUSAL, tools=["write_file"], attempted=[]) == []
    assert unattempted_refusals(REFUSAL, tools=[], attempted=[]) == []


# -- the harness acts on it -------------------------------------------------------------------


def _nudge(text, attempted):
    ev = EvidenceSummary([], [], Plan(), tools_available=CATALOG, tools_tried=attempted)
    return CompletionEvaluator().evaluate(ev, text, validation_available=False)


def test_refusal_without_an_attempt_is_nudged():
    verdict = _nudge(REFUSAL, [])
    assert not verdict.accept
    assert any("never tried" in r for r in verdict.reasons)


def test_evidence_that_shows_the_lookup_tool_was_used_is_accepted():
    assert _nudge(REFUSAL, ["web_search"]).accept


async def test_run_records_the_unchecked_refusal(project: Path, manager_factory, events, recorder):
    provider = ScriptedProvider([ModelResponse(text=REFUSAL)] * 4)
    agent, _ = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE, validation={}
    )
    result = await agent.run("what is the weather in Atlanta")
    assert result.capability_unchecked, "the run recorded no unchecked refusal"
    assert "web_search" in result.capability_unchecked[0]
    assert result.unattempted_refusal is True
    assert result.to_json()["unattempted_refusal"] is True
    assert (
        recorder.of_type(EventType.UNATTEMPTED_REFUSAL)[0].data["claims"]
        == result.capability_unchecked
    )
