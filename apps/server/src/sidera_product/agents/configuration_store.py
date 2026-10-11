from datetime import datetime
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError
from sidera_agents.contracts import ProfileRef, ResourceRef
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sidera_product.postgres.models import ConfigurationRequestRow
from sidera_product.postgres.store import ProductStoreError

from .contracts import ConfigurationRef, ConfigurationRequest, ConfigurationState


def _json(ref: ConfigurationRef | None) -> dict[str, JsonValue] | None:
    return (
        None
        if ref is None
        else TypeAdapter(dict[str, JsonValue]).validate_python(
            ref.model_dump(mode="json")
        )
    )


def _request(row: ConfigurationRequestRow) -> ConfigurationRequest:
    ref_type = ProfileRef if row.action.startswith("profile.") else ResourceRef
    return ConfigurationRequest.model_validate(
        {
            "workspace_id": row.workspace_id,
            "principal_id": row.principal_id,
            "operation_id": row.operation_id,
            "action": row.action,
            "request_fingerprint": row.request_fingerprint,
            "draft_fingerprint": row.draft_fingerprint,
            "target_ref": ref_type.model_validate(row.target_ref),
            "expected_ref": None
            if row.expected_ref is None
            else ref_type.model_validate(row.expected_ref),
            "state": row.state,
            "version": row.version,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "reason_code": row.reason_code,
            "result_ref": None
            if row.result_ref is None
            else ref_type.model_validate(row.result_ref),
        }
    )


def _owned(workspace_id: UUID, principal_id: str, operation_id: str):
    return select(ConfigurationRequestRow).where(
        ConfigurationRequestRow.workspace_id == workspace_id,
        ConfigurationRequestRow.principal_id == principal_id,
        ConfigurationRequestRow.operation_id == operation_id,
    )


class PostgresConfigurationStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def claim(self, request: ConfigurationRequest) -> tuple[ConfigurationRequest, bool]:
        if request.state != "dispatching" or request.version != 1:
            raise ProductStoreError("configuration_request_invalid")
        values = request.model_dump(mode="python")
        for field in ("target_ref", "expected_ref", "result_ref"):
            values[field] = _json(getattr(request, field))
        statement = (
            insert(ConfigurationRequestRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "principal_id", "operation_id"]
            )
            .returning(ConfigurationRequestRow)
        )
        try:
            with Session(self.engine) as session, session.begin():
                inserted = session.execute(statement).scalar_one_or_none()
                if inserted is not None:
                    return _request(inserted), True
                row = session.execute(
                    _owned(
                        request.workspace_id, request.principal_id, request.operation_id
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise ProductStoreError("product_store_unavailable")
                saved = _request(row)
                if (
                    saved.request_fingerprint != request.request_fingerprint
                    or saved.draft_fingerprint != request.draft_fingerprint
                    or saved.action != request.action
                    or saved.expected_ref != request.expected_ref
                    or saved.target_ref != request.target_ref
                ):
                    raise ProductStoreError("operation_conflict")
                return saved, False
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def get(
        self, *, workspace_id: UUID, principal_id: str, operation_id: str
    ) -> ConfigurationRequest | None:
        try:
            with Session(self.engine) as session:
                row = session.execute(
                    _owned(workspace_id, principal_id, operation_id)
                ).scalar_one_or_none()
                return None if row is None else _request(row)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def settle(
        self,
        expected: ConfigurationRequest,
        *,
        state: ConfigurationState,
        reason_code: str | None,
        result_ref: ConfigurationRef | None,
        now: datetime,
        observation: bool = False,
    ) -> ConfigurationRequest:
        try:
            with Session(self.engine) as session, session.begin():
                row = session.execute(
                    _owned(
                        expected.workspace_id,
                        expected.principal_id,
                        expected.operation_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if row is None:
                    raise ProductStoreError("configuration_request_not_found")
                saved = _request(row)
                if (
                    saved.request_fingerprint != expected.request_fingerprint
                    or saved.target_ref != expected.target_ref
                    or saved.expected_ref != expected.expected_ref
                ):
                    raise ProductStoreError("operation_conflict")
                if observation and saved.version != expected.version:
                    return saved
                # The winner's positive reply can follow an uncertain duplicate read.
                if (
                    saved.state == "succeeded"
                    or saved.state == "rejected"
                    or (saved.state == "reconciled" and state != "succeeded")
                ):
                    return saved
                updated = saved.model_copy(
                    update={
                        "state": state,
                        "reason_code": reason_code,
                        "result_ref": result_ref,
                        "updated_at": now,
                        "version": saved.version + 1,
                    }
                )
                updated = ConfigurationRequest.model_validate(
                    updated.model_dump(mode="python")
                )
                row.state, row.reason_code = updated.state, updated.reason_code
                row.result_ref, row.updated_at, row.version = (
                    _json(updated.result_ref),
                    updated.updated_at,
                    updated.version,
                )
                session.flush()
                return _request(row)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None
