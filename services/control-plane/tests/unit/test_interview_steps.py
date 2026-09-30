from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.interview_demo_support import manifest_for


@pytest.mark.parametrize(
    "text",
    (
        "手机号允许为空，有值时才校验格式",
        "手机号可选",
    ),
)
def test_phone_policy_accepts_explicit_optional_rules(text: str) -> None:
    from sidera_control_plane.demo.worker_steps import interpret_phone_policy

    assert interpret_phone_policy(text) == "optional"


@pytest.mark.parametrize(
    "text",
    (
        "手机号不能为空",
        "手机号必须填写",
    ),
)
def test_phone_policy_accepts_explicit_required_rules(text: str) -> None:
    from sidera_control_plane.demo.worker_steps import interpret_phone_policy

    assert interpret_phone_policy(text) == "required"


@pytest.mark.parametrize(
    "text",
    (
        "顺便把生产库迁移一下",
        "手机号允许为空，但同时不能为空",
        "",
    ),
)
def test_phone_policy_leaves_unsupported_or_conflicting_input_unhandled(
    text: str,
) -> None:
    from sidera_control_plane.demo.worker_steps import interpret_phone_policy

    assert interpret_phone_policy(text) == "unsupported"


def test_advance_worker_rejects_a_session_not_owned_by_the_run(tmp_path) -> None:
    from sidera_control_plane.demo.worker_steps import advance_worker

    manifest = manifest_for(tmp_path)

    with pytest.raises(ValueError, match="session"):
        advance_worker(manifest, uuid4())
