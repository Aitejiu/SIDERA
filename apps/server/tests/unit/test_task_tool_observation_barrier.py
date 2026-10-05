from __future__ import annotations

import pytest

from tests.integration import test_task_tool_progress as journey


@pytest.mark.parametrize(
    "cursors,sequence,after_cursor,expected",
    (
        ((0,), 0, None, False),
        ((1,), 2, None, False),
        ((2,), 2, None, True),
        ((3,), 2, None, False),
        ((), 2, None, False),
        ((2, 2), 2, None, False),
        ((2,), 2, 2, False),
        ((3,), 3, 2, True),
        ((2,), 3, 1, False),
    ),
    ids=(
        "zero",
        "behind",
        "current",
        "ahead",
        "missing",
        "duplicate",
        "not-advanced",
        "advanced-current",
        "advanced-but-behind",
    ),
)
def test_observation_barrier_requires_one_current_positive_native_cursor(
    cursors, sequence, after_cursor, expected
):
    facts = {
        "link": [{"event_cursor": cursor} for cursor in cursors],
        "native": {"sequence": sequence},
    }
    assert journey.observation_is_current(facts, after_cursor=after_cursor) is expected
    assert facts == {
        "link": [{"event_cursor": cursor} for cursor in cursors],
        "native": {"sequence": sequence},
    }
