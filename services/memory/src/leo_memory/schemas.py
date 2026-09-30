"""Request/response models and the summarizer's output schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TurnMessage(BaseModel):
    id: str | None = None
    role: str
    content: Any = ""  # str, or OpenAI-style list of parts


class TurnEvent(BaseModel):
    chat_id: str | None = None
    user_id: str = Field(min_length=1)
    title: str | None = None
    messages: list[TurnMessage] = Field(default_factory=list)
    memory_enabled: bool = True  # the filter's per-user valve
    project_id: str = ""  # Open WebUI folder id; "" = global scope


class FactIn(BaseModel):
    user_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=300)
    project_id: str = ""


class ForgetIn(BaseModel):
    user_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=500)
    project_id: str = ""


class SummaryV1(BaseModel):
    """What the summarizer must return. Limits are also enforced in code."""

    summary: str
    key_points: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    weak_spots: list[str] = Field(default_factory=list)
    mastered: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class RecallItem(BaseModel):
    type: str  # summary | fact | profile
    id: str
    score: float | None = None
    text: str


class RecallResponse(BaseModel):
    block: str
    items: list[RecallItem]
