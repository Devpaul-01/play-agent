"""Real Groq-backed Diagnoser (architecture Sec12), implementing the
Diagnoser Protocol from driver.py.

Uses Groq's tool-calling interface, not freeform JSON-mode prompting, to
get structurally parseable responses: diagnose_with_usage() and
propose_repair() each force the model to call a single defined tool whose
arguments are the actual structured output, rather than asking it to emit
JSON as prose and hoping it's well-formed. The orchestrator still
independently validates everything that comes back (Sec12:
"provider-reported success is never treated as proof of correctness") --
tool-calling narrows the space of malformed responses, it doesn't
eliminate the need to check them.

This module owns API calls and prompt construction only. It does not
decide retry policy (that's transitions.py) or know about the sandbox --
it is a pure "evidence in, candidates or an expression out" boundary,
same as the other tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from groq import Groq
from groq.types.chat import ChatCompletionToolParam

from play_agent.orchestrator.states import Candidate
from play_agent.tools.contracts import FailureEvidence

_PRICING_USD_PER_MILLION_TOKENS = {
    "prompt": 0.59,
    "completion": 0.79,
}


class DiagnoserError(Exception):
    """Raised when the model's response cannot be turned into a usable
    result even after retry -- an infrastructure/protocol failure, not a
    legitimate 'not repairable' diagnosis (which is an empty candidates
    tuple, not an exception)."""


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float


@dataclass(frozen=True)
class DiagnosisResult:
    """Wraps the Candidate tuple with usage data, so the Driver can
    persist cost/token figures onto the Attempt row (Sec13's
    token_usage_prompt/token_usage_completion/estimated_cost_usd columns)."""

    candidates: tuple[Candidate, ...]
    usage: TokenUsage


_DIAGNOSE_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "report_diagnosis",
        "description": (
            "Report diagnosed candidate locator repairs for a failing Playwright "
            "test, ranked best-first, or report that the failure is not a "
            "repairable stale-locator issue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repairable": {
                    "type": "boolean",
                    "description": (
                        "False if this failure is not a stale-locator issue "
                        "(e.g. a real application bug, a timeout unrelated to "
                        "element lookup, a network error) -- in that case, "
                        "candidates must be empty."
                    ),
                },
                "candidates": {
                    "type": "array",
                    "description": "Ranked best-first; empty if repairable is false.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "locator_call_id": {
                                "type": "string",
                                "description": (
                                    "The locator_call_id of the specific call in "
                                    "the test source that should be replaced, "
                                    "copied exactly from the evidence provided."
                                ),
                            },
                            "reasoning": {
                                "type": "string",
                                "description": (
                                    "Why this locator is believed stale, and why "
                                    "this is a plausible fix -- one or two sentences."
                                ),
                            },
                        },
                        "required": ["locator_call_id", "reasoning"],
                    },
                },
            },
            "required": ["repairable", "candidates"],
        },
    },
}

_PROPOSE_REPAIR_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "report_repair_expression",
        "description": "Report the replacement locator expression for a candidate.",
        "parameters": {
            "type": "object",
            "properties": {
                "new_locator_expression": {
                    "type": "string",
                    "description": (
                        "A single Playwright locator-builder expression, e.g. "
                        'page.getByTestId("submit-button") or '
                        'page.getByRole("button").filter({ hasText: "Submit" }). '
                        "Must use only allowlisted locator methods (getByTestId, "
                        "getByRole, getByText, getByLabel, getByPlaceholder, "
                        "getByAltText, getByTitle, locator, first, last, nth, "
                        "filter) with literal arguments only -- no variables, no "
                        "template interpolation, no other expressions. This will "
                        "be rejected by structural validation otherwise."
                    ),
                },
            },
            "required": ["new_locator_expression"],
        },
    },
}


class GroqDiagnoser:
    """Diagnoser Protocol implementation backed by Groq's chat completions
    API. See driver.py's Diagnoser Protocol for the interface contract.
    """

    def __init__(self, api_key: str, model: str, client: Groq | None = None) -> None:
        self._model = model
        self._client = client or Groq(api_key=api_key)

    def diagnose_with_usage(
        self,
        evidence: FailureEvidence,
        test_source: str,
        is_re_diagnosis: bool,
    ) -> DiagnosisResult:
        prompt = _build_diagnosis_prompt(evidence, test_source, is_re_diagnosis)

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            tools=[_DIAGNOSE_TOOL],
            tool_choice={"type": "function", "function": {"name": "report_diagnosis"}},
        )

        usage = _extract_usage(response)
        args = _extract_tool_call_arguments(response, "report_diagnosis")

        if not args.get("repairable", False):
            return DiagnosisResult(candidates=(), usage=usage)

        raw_candidates = args.get("candidates", [])
        candidates = tuple(
            Candidate(
                rank=i + 1,
                locator_call_id=c["locator_call_id"],
                reasoning=c["reasoning"],
            )
            for i, c in enumerate(raw_candidates)
        )
        return DiagnosisResult(candidates=candidates, usage=usage)

    def diagnose(
        self,
        evidence: FailureEvidence,
        test_source: str,
        is_re_diagnosis: bool,
    ) -> tuple[Candidate, ...]:
        """Convenience wrapper for callers that only want candidates (e.g.
        the smoke-test script) -- the Driver uses diagnose_with_usage
        directly so it can persist cost/token data."""

        return self.diagnose_with_usage(evidence, test_source, is_re_diagnosis).candidates

    def propose_repair(self, evidence: FailureEvidence, candidate: Candidate) -> str:
        prompt = _build_repair_prompt(evidence, candidate)

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            tools=[_PROPOSE_REPAIR_TOOL],
            tool_choice={"type": "function", "function": {"name": "report_repair_expression"}},
        )

        args = _extract_tool_call_arguments(response, "report_repair_expression")
        expression = args.get("new_locator_expression")
        if not expression or not isinstance(expression, str):
            raise DiagnoserError(
                f"model did not return a usable new_locator_expression: {args!r}"
            )
        return expression


def _extract_tool_call_arguments(response, expected_function_name: str) -> dict:
    try:
        message = response.choices[0].message
        tool_calls = message.tool_calls
        if not tool_calls:
            raise DiagnoserError(
                f"model did not call {expected_function_name} -- no tool_calls in response "
                f"(this can happen if the model refuses or returns plain text instead)"
            )
        call = tool_calls[0]
        if call.function.name != expected_function_name:
            raise DiagnoserError(
                f"expected tool call to {expected_function_name}, got {call.function.name}"
            )
        return json.loads(call.function.arguments)
    except (AttributeError, IndexError, KeyError) as e:
        raise DiagnoserError(f"malformed response shape from Groq: {e}") from e
    except json.JSONDecodeError as e:
        raise DiagnoserError(f"model's tool call arguments were not valid JSON: {e}") from e


def _extract_usage(response) -> TokenUsage:
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

    cost = (
        prompt_tokens / 1_000_000 * _PRICING_USD_PER_MILLION_TOKENS["prompt"]
        + completion_tokens / 1_000_000 * _PRICING_USD_PER_MILLION_TOKENS["completion"]
    )

    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=round(cost, 6),
    )


def _build_diagnosis_prompt(
    evidence: FailureEvidence, test_source: str, is_re_diagnosis: bool
) -> str:
    retry_note = (
        "\nNote: an earlier diagnosis pass for this same failure already "
        "proposed candidates and none of them fixed the test after real "
        "re-verification. Consider different locator strategies than an "
        "obvious first guess might suggest."
        if is_re_diagnosis
        else ""
    )

    return f"""A Playwright test has failed. Diagnose whether this is a stale \
element locator (the UI still has the element, but the way the test finds \
it broke) that can be repaired by changing only the locator expression.

Test file: {evidence.test_file_path}
Test: {evidence.test_title}

Error:
{evidence.error_message}

Stack trace:
{evidence.stack_trace}

Test source (locator calls are what you may propose replacing):
{test_source}
{retry_note}

Only propose a fix if you believe this is genuinely a stale-locator issue. \
If the failure looks like a real application bug, an unrelated timeout, or \
something else that changing a locator cannot fix, report repairable=false."""


def _build_repair_prompt(evidence: FailureEvidence, candidate: Candidate) -> str:
    return f"""You previously diagnosed a candidate locator repair for a \
failing Playwright test. Propose the exact replacement locator expression.

Test: {evidence.test_title}
Error: {evidence.error_message}

Candidate locator_call_id: {candidate.locator_call_id}
Diagnosis reasoning: {candidate.reasoning}

Report only the new locator expression -- a single Playwright locator-\
builder call or chain, using only allowlisted methods with literal \
arguments."""