from __future__ import annotations

from pathlib import Path
from typing import Literal

from .contracts import DemoManifest
from .environment import runtime_root_for
from .manifest import load_manifest
from .state import DemoStateStore

_GOVERNANCE_EVALUATION_GATE = "governance"

GovernanceAction = Literal[
    "PauseCollaborationRelease",
    "ApproveCollaborationReleaseProposal",
    "RejectCollaborationReleaseProposal",
    "ResumeAutomaticCollaboration",
]


def manual_release_actions() -> tuple[GovernanceAction, ...]:
    """Expose the existing governance command path used by the demo scenario."""
    return (
        "PauseCollaborationRelease",
        "ApproveCollaborationReleaseProposal",
        "RejectCollaborationReleaseProposal",
        "ResumeAutomaticCollaboration",
    )


def is_manual_release_pending(governance: object) -> bool:
    """Read the projection vocabulary without introducing a second state machine."""
    if not isinstance(governance, dict):
        return False
    return governance.get("governance_mode") == "paused" and (
        governance.get("release_state") in {"pending", "held", "proposal_pending"}
        or governance.get("active_proposal") is not None
    )


def demo_governance_evaluation_armed(manifest: DemoManifest) -> bool:
    """Allow the governance interview to expose its pause action deterministically."""

    if manifest.scenario != "governance":
        return True
    return DemoStateStore(
        runtime_root_for(manifest) / "state", manifest.run_id
    ).read_gate(_GOVERNANCE_EVALUATION_GATE)


def arm_demo_governance_evaluation(manifest_path: Path) -> None:
    """Open the demo-only evaluation gate after a committed pause command."""

    manifest = load_manifest(manifest_path)
    if manifest.scenario != "governance":
        return
    DemoStateStore(
        runtime_root_for(manifest) / "state", manifest.run_id
    ).set_gate(_GOVERNANCE_EVALUATION_GATE, True)
