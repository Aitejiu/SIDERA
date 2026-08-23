from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
ToolOutcome = Literal[
    "ok",
    "needs_user",
    "conflict",
    "unavailable",
    "result_unknown",
    "failed",
]


class ToolEnvelope(BaseModel):
    """Stable, bounded result envelope returned by every bridge tool."""

    model_config = ConfigDict(extra="forbid")

    outcome: ToolOutcome
    summary: str
    resource_ref: str | None = None
    web_url: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value
