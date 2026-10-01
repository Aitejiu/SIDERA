from dataclasses import dataclass
from hashlib import sha256

from .contracts import (
    AssembledInput,
    ContextItem,
    JsonValue,
    ProfileRevision,
    RunInput,
    RunStart,
    SourceContent,
)
from .errors import AgentError
from .ports import ContextSourceReader

type ContextRequest = RunStart | RunInput


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    input: AssembledInput
    context_receipts: tuple[dict[str, JsonValue], ...]


class ContextAssembler:
    def assemble(
        self,
        profile: ProfileRevision,
        request: ContextRequest,
        source_reader: ContextSourceReader,
    ) -> ContextAssembly:
        if isinstance(request, RunStart):
            if request.profile_ref != profile.ref:
                raise AgentError(
                    "profile_reference_mismatch", "Profile reference is unavailable."
                )
            request_instructions = request.instructions
        elif request.workspace_id == profile.ref.workspace_id:
            request_instructions = ()
        else:
            raise AgentError("permission_denied", "Access denied.")

        resolved: list[ContextItem] = []
        receipts: list[dict[str, JsonValue]] = []
        for item in sorted(
            request.context, key=lambda value: (value.ordinal, value.item_id)
        ):
            try:
                if item.source_ref is None:
                    if item.text is None:
                        raise ValueError("inline context text is missing")
                    content = SourceContent(
                        text=item.text,
                        actual_version=item.version,
                        content_hash=item.content_hash,
                    )
                else:
                    content = source_reader.read(request.grant_id, item.source_ref)
                    if not isinstance(content, SourceContent):
                        raise TypeError("context source returned invalid content")
            except Exception:  # noqa: BLE001
                if item.required:
                    raise AgentError(
                        "context_required_missing",
                        "Required context is unavailable.",
                    ) from None
                receipts.append(_omission_receipt(item, "source_unavailable"))
                continue

            if content.actual_version != item.version:
                if item.required:
                    raise AgentError(
                        "context_version_mismatch",
                        "Context source version does not match the requested version.",
                    ) from None
                receipts.append(_omission_receipt(item, "context_version_mismatch"))
                continue

            digest = _content_hash(content.text)
            if content.content_hash != digest or item.content_hash != digest:
                if item.required:
                    raise AgentError(
                        "context_hash_mismatch",
                        "Context source content could not be verified.",
                    ) from None
                receipts.append(_omission_receipt(item, "context_hash_mismatch"))
                continue

            resolved.append(
                ContextItem(
                    item_id=item.item_id,
                    origin=item.origin,
                    required=item.required,
                    ordinal=item.ordinal,
                    version=content.actual_version,
                    content_hash=digest,
                    text=content.text,
                    source_ref=None,
                )
            )
            receipts.append(
                {
                    "item_id": item.item_id,
                    "origin": item.origin,
                    "requested_version": item.version,
                    "actual_version": content.actual_version,
                    "content_hash": digest,
                    "status": "resolved",
                    "reason_code": None,
                }
            )
        assembled = AssembledInput(
            instructions=(profile.draft.instructions, *request_instructions),
            text=request.text,
            context=tuple(resolved),
        )
        return ContextAssembly(assembled, tuple(receipts))


def _content_hash(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _omission_receipt(item: ContextItem, reason_code: str) -> dict[str, JsonValue]:
    return {
        "item_id": item.item_id,
        "origin": item.origin,
        "requested_version": item.version,
        "actual_version": None,
        "content_hash": None,
        "status": "omitted",
        "reason_code": reason_code,
    }
