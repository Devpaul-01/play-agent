# tests/test_groq_diagnoser.py
"""Tests for GroqDiagnoser using a fake transport -- no real network calls,
no real API key required. These exercise the prompt-building, tool-call
response parsing, and error-handling logic against hand-built response
objects shaped like what the real Groq SDK returns.

CAVEAT: these tests prove the parsing/control-flow logic is correct given
responses shaped as expected. They do NOT prove the real Groq API
actually returns responses in this shape, or that tool_choice forcing
behaves as documented -- that needs a live smoke test against a real
API key, which is a separate, explicitly-run script, not part of this
suite (keeps CI free and fast, and keeps API costs at zero for routine
runs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from play_agent.orchestrator.groq_diagnoser import DiagnoserError, GroqDiagnoser
from play_agent.orchestrator.states import Candidate
from play_agent.tools.contracts import FailureEvidence


def make_evidence() -> FailureEvidence:
    return FailureEvidence(
        test_file_path="checkout.spec.ts",
        test_title="completes checkout",
        error_message="Timeout waiting for selector '.old-class'",
        stack_trace="at checkout.spec.ts:12",
        screenshot_path=None,
        trace_path=None,
    )


def make_tool_call_response(function_name: str, arguments: dict, prompt_tokens=100, completion_tokens=50):
    """Builds a fake object matching the shape of groq's ChatCompletion
    response far enough for our parsing code to work against it."""

    tool_call = SimpleNamespace(
        function=SimpleNamespace(name=function_name, arguments=json.dumps(arguments))
    )
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


@dataclass
class FakeGroqClient:
    """Stands in for groq.Groq() -- scripted responses per call, in order."""

    responses: list
    _call_count: int = field(default=0)
    captured_calls: list = field(default_factory=list)

    def __post_init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.captured_calls.append(kwargs)
        response = self.responses[self._call_count]
        self._call_count += 1
        return response


# -- diagnose() -------------------------------------------------------------


def test_diagnose_returns_ranked_candidates():
    response = make_tool_call_response(
        "report_diagnosis",
        {
            "repairable": True,
            "candidates": [
                {"locator_call_id": "L5C10", "reasoning": "class renamed in a recent commit"},
                {"locator_call_id": "L5C10", "reasoning": "fallback: try role-based lookup"},
            ],
        },
    )
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    candidates = diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=False)

    assert len(candidates) == 2
    assert candidates[0].rank == 1
    assert candidates[0].locator_call_id == "L5C10"
    assert candidates[1].rank == 2


def test_diagnose_not_repairable_returns_empty_tuple():
    response = make_tool_call_response(
        "report_diagnosis", {"repairable": False, "candidates": []}
    )
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    candidates = diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=False)

    assert candidates == ()


def test_diagnose_forces_tool_choice():
    # Confirms we actually request forced tool-calling, not leaving it to
    # the model's discretion -- this is the whole reason tool-calling was
    # chosen over freeform JSON-mode prompting.
    response = make_tool_call_response(
        "report_diagnosis", {"repairable": False, "candidates": []}
    )
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=False)

    call_kwargs = client.captured_calls[0]
    assert call_kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "report_diagnosis"},
    }


def test_diagnose_re_diagnosis_prompt_includes_retry_note():
    # The prompt text itself is the behavior under test here -- confirms
    # is_re_diagnosis actually changes what's sent, not just accepted and
    # ignored.
    response = make_tool_call_response(
        "report_diagnosis", {"repairable": False, "candidates": []}
    )
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=True)

    sent_prompt = client.captured_calls[0]["messages"][0]["content"]
    assert "already proposed candidates" in sent_prompt


def test_diagnose_no_tool_calls_raises_diagnoser_error():
    # The model refused or returned plain text instead of calling the
    # tool -- must surface as a clear error, not silently return empty
    # candidates (which would be indistinguishable from a legitimate
    # 'not repairable' diagnosis).
    message = SimpleNamespace(tool_calls=None)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    response = SimpleNamespace(choices=[choice], usage=usage)

    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    with pytest.raises(DiagnoserError, match="did not call"):
        diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=False)


def test_diagnose_malformed_json_raises_diagnoser_error():
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="report_diagnosis", arguments="{not valid json")
    )
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    response = SimpleNamespace(choices=[choice], usage=usage)

    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    with pytest.raises(DiagnoserError, match="not valid JSON"):
        diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=False)


def test_diagnose_wrong_tool_called_raises_diagnoser_error():
    response = make_tool_call_response("some_other_function", {})
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    with pytest.raises(DiagnoserError, match="expected tool call to"):
        diagnoser.diagnose(make_evidence(), test_source="...", is_re_diagnosis=False)


# -- propose_repair() --------------------------------------------------------


def test_propose_repair_returns_expression():
    response = make_tool_call_response(
        "report_repair_expression", {"new_locator_expression": 'page.getByTestId("new-id")'}
    )
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    candidate = Candidate(rank=1, locator_call_id="L5C10", reasoning="stale class")
    expr = diagnoser.propose_repair(make_evidence(), candidate)

    assert expr == 'page.getByTestId("new-id")'


def test_propose_repair_missing_expression_raises():
    response = make_tool_call_response("report_repair_expression", {})
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    candidate = Candidate(rank=1, locator_call_id="L5C10", reasoning="stale class")

    with pytest.raises(DiagnoserError, match="did not return a usable"):
        diagnoser.propose_repair(make_evidence(), candidate)


# -- usage/cost tracking ------------------------------------------------------


def test_diagnose_with_usage_populates_token_counts():
    response = make_tool_call_response(
        "report_diagnosis",
        {"repairable": False, "candidates": []},
        prompt_tokens=250,
        completion_tokens=40,
    )
    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    result = diagnoser._diagnose_with_usage(make_evidence(), test_source="...", is_re_diagnosis=False)

    assert result.usage.prompt_tokens == 250
    assert result.usage.completion_tokens == 40
    assert result.usage.estimated_cost_usd > 0


def test_diagnose_with_usage_handles_missing_usage_gracefully():
    # Some responses (or fakes/edge cases) might not include usage data --
    # must not crash, should default to zero rather than raise.
    tool_call = SimpleNamespace(
        function=SimpleNamespace(
            name="report_diagnosis",
            arguments=json.dumps({"repairable": False, "candidates": []}),
        )
    )
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    response = SimpleNamespace(choices=[choice], usage=None)

    client = FakeGroqClient(responses=[response])
    diagnoser = GroqDiagnoser(api_key="unused", model="test-model", client=client)

    result = diagnoser._diagnose_with_usage(make_evidence(), test_source="...", is_re_diagnosis=False)

    assert result.usage.prompt_tokens == 0
    assert result.usage.estimated_cost_usd == 0.0