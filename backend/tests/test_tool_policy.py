from __future__ import annotations

from dataclasses import dataclass

from app.services.tool_policy import classify_tool, requires_approval


@dataclass
class _Annotations:
    read_only_hint: bool | None = None
    destructive_hint: bool = False


@dataclass
class _Tool:
    name: str
    annotations: _Annotations | None = None


def test_server_annotation_wins_over_name():
    # Named like a read, annotated as a write. Trust the server.
    tool = _Tool("get_everything", _Annotations(read_only_hint=False))
    assert classify_tool(tool) is False


def test_destructive_hint_always_wins():
    tool = _Tool("list_things", _Annotations(read_only_hint=True, destructive_hint=True))
    assert classify_tool(tool) is False


def test_name_pattern_used_when_unannotated():
    assert classify_tool(_Tool("search_udm_events")) is True
    assert classify_tool(_Tool("list_cases")) is True


def test_unknown_tool_fails_closed():
    """The default for anything we can't classify must be 'needs approval'."""
    assert classify_tool(_Tool("update_case_status")) is False
    assert classify_tool(_Tool("frobnicate")) is False
    assert requires_approval(classify_tool(_Tool("frobnicate"))) is True
