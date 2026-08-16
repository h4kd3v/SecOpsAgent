from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionOut(BaseModel):
    id: uuid.UUID
    label: str
    created_at: datetime


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class InvocationOut(BaseModel):
    id: uuid.UUID
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    is_write: bool
    latency_ms: int | None
    error: str | None
    # Deliberately NOT the whole result. A single UDM page is ~400 KB, and a
    # thread with five searches made every reload — and every turn, which ends
    # in a reload — a multi-megabyte download of data the analyst had already
    # seen. `result_chars` tells the UI whether there is more to fetch.
    result_preview: str | None = None
    result_chars: int = 0


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    status: str
    # The model's working, when the gateway streams it. Separate from content
    # so the UI can present it as working rather than as the answer.
    reasoning: str | None = None
    # Why this turn failed, if it did. Kept out of `content` so the UI can
    # style it as a failure rather than as something the model said.
    error: str | None = None
    seq: int
    created_at: datetime
    # {prompt_tokens, completion_tokens, total_tokens, estimated?}
    token_usage: dict[str, Any] | None = None
    model: str | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]
    invocations: list[InvocationOut]
    # Running total across the thread, for cost tracking across ~20 analysts.
    total_tokens: int = 0


class AppConfigOut(BaseModel):
    """UI-relevant server settings, fetched once on boot."""

    model_display_name: str
    require_approval_for_write: bool
    demo_mode: bool


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)


class ApprovalDecision(BaseModel):
    invocation_id: uuid.UUID
    decision: Literal["approve", "deny"]


class ApprovalRequest(BaseModel):
    decisions: list[ApprovalDecision] = Field(min_length=1)


class ToolOut(BaseModel):
    name: str
    description: str
    read_only: bool


class ToolCatalogOut(BaseModel):
    tools: list[ToolOut]
    fetched_at: datetime | None
    age_seconds: int | None
    # "cache" served from Postgres, "live" just fetched, "stale" past TTL but
    # the MCP server could not be reached
    source: str
    stale: bool
    error: str | None
    ttl_hours: float
