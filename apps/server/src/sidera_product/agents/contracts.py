import hashlib
import json
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sidera_agents.contracts import ProfileRef, ResourceRef

type ConfigurationAction = Literal[
    "profile.create", "profile.revise", "resource.create", "resource.revise"
]
type ConfigurationState = Literal[
    "claimed", "dispatching", "succeeded", "reconciled", "rejected", "unknown"
]
type ConfigurationRef = ProfileRef | ResourceRef


def canonical_hash(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ConfigurationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, hide_input_in_errors=True
    )


class ConfigurationRequest(ConfigurationModel):
    workspace_id: UUID
    principal_id: str = Field(min_length=1, max_length=200, strict=True)
    operation_id: str = Field(min_length=1, max_length=128, strict=True)
    action: ConfigurationAction
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    draft_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    target_ref: ConfigurationRef
    expected_ref: ConfigurationRef | None
    state: ConfigurationState
    version: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    reason_code: str | None
    result_ref: ConfigurationRef | None

    @model_validator(mode="after")
    def _exact_refs(self) -> Self:
        profile = self.action.startswith("profile.")
        if (
            not self.principal_id.strip()
            or self.target_ref.workspace_id != self.workspace_id
            or isinstance(self.target_ref, ProfileRef) is not profile
            or (self.result_ref is not None and self.result_ref != self.target_ref)
        ):
            raise ValueError("invalid configuration request scope")
        if self.action.endswith(".create"):
            if self.expected_ref is not None or self.target_ref.revision != 1:
                raise ValueError("invalid create target")
        else:
            expected = self.expected_ref
            if (
                expected is None
                or type(expected) is not type(self.target_ref)
                or expected.workspace_id != self.workspace_id
            ):
                raise ValueError("invalid revision parent")
            values = expected.model_dump()
            values["revision"] = expected.revision + 1
            if type(expected).model_validate(values) != self.target_ref:
                raise ValueError("revision target does not follow its exact parent")
        if self.state in {"succeeded", "reconciled"} and self.result_ref is None:
            raise ValueError("confirmed configuration requires its result reference")
        return self


class ConfigurationResult(ConfigurationModel):
    operation_id: str = Field(
        alias="operationId", min_length=1, max_length=128, strict=True
    )
    state: ConfigurationState
    target_ref: ConfigurationRef
    result_ref: ConfigurationRef | None
    reason_code: str | None

    @classmethod
    def from_request(cls, record: ConfigurationRequest) -> Self:
        return cls(
            operationId=record.operation_id,
            state=record.state,
            target_ref=record.target_ref,
            result_ref=record.result_ref,
            reason_code=record.reason_code,
        )
