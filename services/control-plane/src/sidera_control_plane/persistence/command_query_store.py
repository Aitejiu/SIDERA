import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from sidera_control_plane.application.command_queries import CommandTerminalView
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import CommandModel

_CONFIRMATION_PAYLOAD_KEYS = frozenset(
    {"proposal_revision", "proposal_content_hash", "profile_id", "profile_version"}
)


class SqlAlchemyCommandQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_terminal(
        self, workspace_id: UUID, command_id: UUID
    ) -> CommandTerminalView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(CommandModel).where(
                    CommandModel.workspace_id == workspace_id,
                    CommandModel.id == command_id,
                )
            )
        if row is None:
            return None
        if row.status == "applied":
            status = "applied"
        elif row.status == "rejected":
            status = "rejected"
        else:
            raise RuntimeError("stored command status is invalid")
        return CommandTerminalView(
            command_id=row.id,
            status=status,
            result=dict(row.result or {}),
            error_code=row.rejection_reason if status == "rejected" else None,
            command_type=row.command_type,
            target_type=row.target_type,
            target_id=row.target_id,
            expected_version=row.expected_version,
            payload_sha256=_payload_sha256(row.payload),
            payload=_public_payload(row.command_type, row.payload),
        )


def _payload_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_payload(
    command_type: str, payload: object
) -> dict[str, object] | None:
    """Expose only the non-secret confirmation payload projection."""

    if command_type != "ConfirmPlanningProposal" or not isinstance(payload, dict):
        return None
    if set(payload) != _CONFIRMATION_PAYLOAD_KEYS:
        return None
    revision = payload.get("proposal_revision")
    proposal_hash = payload.get("proposal_content_hash")
    profile_id = payload.get("profile_id")
    profile_version = payload.get("profile_version")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(proposal_hash, str)
        or len(proposal_hash) != 64
        or any(character not in "0123456789abcdef" for character in proposal_hash)
        or not isinstance(profile_id, str)
        or not isinstance(profile_version, int)
        or isinstance(profile_version, bool)
        or profile_version < 1
    ):
        return None
    try:
        UUID(profile_id)
    except ValueError:
        return None
    return {
        "proposal_revision": revision,
        "proposal_content_hash": proposal_hash,
        "profile_id": profile_id,
        "profile_version": profile_version,
    }
