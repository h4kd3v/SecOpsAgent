"""Unit tests for the pieces most likely to break silently in production:
streamed tool-call reassembly, argument parsing, and context trimming."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.agent_loop import _parse_args
from app.services.llm import _ToolCallAccumulator, build_messages


@dataclass
class _Fn:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _Delta:
    index: int
    id: str | None = None
    function: _Fn = None  # type: ignore[assignment]


def test_accumulator_reassembles_fragmented_tool_calls():
    acc = _ToolCallAccumulator()
    # Exactly how a proxy fragments a call: id once, name split, args in pieces.
    acc.add([_Delta(0, "call_1", _Fn(name="secops_", arguments=""))])
    acc.add([_Delta(0, None, _Fn(name="search_udm", arguments='{"qu'))])
    acc.add([_Delta(0, None, _Fn(arguments='ery": "ip=1.'))])
    acc.add([_Delta(0, None, _Fn(arguments='2.3.4"}'))])

    calls = acc.result()
    assert len(calls) == 1
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"]["name"] == "secops_search_udm"
    assert calls[0]["function"]["arguments"] == '{"query": "ip=1.2.3.4"}'


def test_accumulator_keeps_parallel_calls_separate():
    acc = _ToolCallAccumulator()
    acc.add([_Delta(0, "a", _Fn(name="get_alert", arguments="{}"))])
    acc.add([_Delta(1, "b", _Fn(name="get_case", arguments='{"id":'))])
    acc.add([_Delta(1, None, _Fn(arguments=' "7"}'))])

    calls = acc.result()
    assert [c["id"] for c in calls] == ["a", "b"]
    assert calls[1]["function"]["arguments"] == '{"id": "7"}'


def test_parse_args_survives_malformed_json():
    args, error = _parse_args('{"query": "unterminated')
    assert error is not None
    assert args["_raw"].startswith('{"query"')


def test_parse_args_rejects_non_objects():
    _, error = _parse_args("[1, 2, 3]")
    assert error == "expected a JSON object"


def test_parse_args_accepts_empty():
    assert _parse_args("") == ({}, None)


@pytest.mark.asyncio
async def test_trimming_never_orphans_a_tool_message():
    """A `tool` message whose assistant parent was trimmed away makes the
    proxy 400 the whole request."""
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
        {"role": "tool", "tool_call_id": "x", "content": "result"},
        {"role": "assistant", "content": "answer"},
    ]
    messages = await build_messages(history, max_messages=2)

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] != "tool"


@pytest.mark.asyncio
async def test_character_budget_sheds_oldest_history_first():
    """Message count is a useless budget when one SecOps result is 500 KB."""
    history = [
        {"role": "user", "content": "a" * 100_000},
        {"role": "assistant", "content": "b" * 100_000},
        {"role": "user", "content": "the recent question"},
    ]
    messages = await build_messages(history, max_messages=40, max_chars=50_000)

    contents = [m["content"] for m in messages]
    assert "the recent question" in contents
    assert not any(c and c.startswith("aaa") for c in contents)


@pytest.mark.asyncio
async def test_a_single_oversized_result_is_kept_rather_than_dropped():
    """The freshest tool result is the one the model is reasoning about right
    now. Better to send it and let the proxy complain than to silently hand
    the model an empty turn."""
    huge = "x" * 500_000
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": huge},
    ]
    messages = await build_messages(history, max_messages=40, max_chars=1_000)

    assert messages[-1]["content"] == huge
    assert len(messages) == 2  # system prompt + the one surviving message


@pytest.mark.asyncio
async def test_zero_budget_disables_character_trimming():
    history = [{"role": "user", "content": "z" * 1_000_000}]
    messages = await build_messages(history, max_messages=40, max_chars=0)

    assert len(messages[-1]["content"]) == 1_000_000


class _ApiError(Exception):
    """Shaped like an openai.APIStatusError: status_code plus a JSON body."""

    def __init__(self, status_code, body, message=""):
        super().__init__(message or str(body))
        self.status_code = status_code
        self.body = body


def test_no_credit_is_explained_not_dumped():
    """The raw 429 is a wall of nested JSON that says nothing about what to do."""
    from app.services.llm import describe_error

    message = describe_error(
        _ApiError(
            429,
            {"error": {"code": "insufficient_quota", "message": "You exceeded your quota"}},
            "Error code: 429",
        )
    )

    assert "no remaining credit" in message
    assert "billing" in message
    assert "not a fault in the app" in message
    assert "{'error'" not in message  # no raw JSON


def test_bad_key_points_at_the_right_setting():
    from app.services.llm import describe_error

    message = describe_error(_ApiError(401, {"error": {"code": "invalid_api_key"}}))
    assert "LLM_API_KEY" in message
    assert "LLM_PROXY_URL" in message


def test_unknown_model_names_the_diagnostic_command():
    from app.services.llm import describe_error

    message = describe_error(_ApiError(404, {"error": {"code": "model_not_found"}}))
    assert "LLM_MODEL_NAME" in message
    assert "app.diagnose" in message


def test_context_overflow_suggests_the_levers_that_matter():
    from app.services.llm import describe_error

    message = describe_error(_ApiError(400, {"error": {"code": "context_length_exceeded"}}))
    assert "TOOL_ALLOWLIST" in message


def test_unrecognised_errors_still_surface_the_detail():
    from app.services.llm import describe_error

    message = describe_error(ValueError("something odd happened"))
    assert "ValueError" in message
    assert "something odd happened" in message
