"""The instructions every completion carries.

The prompt shapes behaviour; it does not enforce it. What is asserted here is
that it reaches the model at all, that an operator can replace it, and that a
broken override degrades to the built-in rather than sending nothing — a
completion with no system message is an agent with no rules, which is the one
outcome worse than a stale prompt.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import llm

settings = get_settings()


@pytest.fixture(autouse=True)
def _fresh_prompt():
    """The prompt is cached for the process; tests must not inherit each
    other's."""
    llm.system_prompt.cache_clear()
    yield
    llm.system_prompt.cache_clear()


async def test_the_prompt_leads_every_completion():
    wire = await llm.build_messages([{"role": "user", "content": "hi"}], 40)

    assert wire[0]["role"] == "system"
    assert wire[0]["content"] == llm.DEFAULT_SYSTEM_PROMPT
    assert wire[1]["content"] == "hi"


async def test_it_survives_history_trimming():
    """Trimming drops from the oldest end, and the prompt is prepended after —
    an agent that loses its rules on long investigations is the wrong way
    round."""
    history = [{"role": "user", "content": "x" * 10_000} for _ in range(50)]
    wire = await llm.build_messages(history, 5, max_chars=1000)

    assert wire[0]["role"] == "system"
    assert wire[0]["content"] == llm.DEFAULT_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "rule",
    [
        "Never invent hostnames",
        "If a tool returns nothing, say so",
        "ask the analyst before querying",
        "Never invent a parameter value",
        "no filesystem",
        "environment variables",
        "Never ask an analyst to paste a secret",
        "needs the analyst's explicit approval",
        "do not retry it",
    ],
)
def test_the_rules_that_were_asked_for_are_actually_in_it(rule: str):
    """Each of these is a specific instruction someone asked for. A prompt is
    easy to edit and easy to gut by accident."""
    assert rule in llm.DEFAULT_SYSTEM_PROMPT


def test_an_operator_can_replace_it(tmp_path, monkeypatch):
    custom = tmp_path / "house-rules.txt"
    custom.write_text("You are the night shift's assistant. Escalate to #soc-oncall.")
    monkeypatch.setattr(settings, "system_prompt_file", str(custom))

    assert llm.system_prompt().startswith("You are the night shift's assistant")


def test_a_missing_file_falls_back_rather_than_sending_nothing(monkeypatch, caplog):
    """A typo in a path must not silently strip the agent of its instructions."""
    import logging

    monkeypatch.setattr(settings, "system_prompt_file", "/no/such/prompt.txt")

    with caplog.at_level(logging.ERROR):
        prompt = llm.system_prompt()

    assert prompt == llm.DEFAULT_SYSTEM_PROMPT
    assert any("could not be read" in r.message for r in caplog.records)


def test_an_empty_file_falls_back_too(tmp_path, monkeypatch):
    """Truncating the file to zero is a likelier accident than deleting it."""
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\n")
    monkeypatch.setattr(settings, "system_prompt_file", str(empty))

    assert llm.system_prompt() == llm.DEFAULT_SYSTEM_PROMPT
