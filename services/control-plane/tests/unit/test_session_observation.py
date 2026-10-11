import pytest


@pytest.mark.parametrize(
    "old_v,old_s,new_v,new_s,want",
    [
        (5, 3, 9, 7, True),
        (5, 3, 5, 3, True),
        (5, 3, 6, 3, False),
        (5, 3, 5, 4, False),
        (5, 3, 4, 2, False),
        (5, None, 6, 4, False),
        (5, None, 5, None, True),
        (True, 0, 2, 1, False),
        (5, -1, 5, -1, False),
        (5, 5, 6, 6, False),
        (5, 3, 6, None, False),
        (None, None, None, None, False),
        (0, 0, 1, 0, False),
        (5.0, 3, 5, 3, False),
        (5, 3, True, 0, False),
        (5, 3, 5.0, 3, False),
        (5, True, 5, 1, False),
        (5, 3.0, 5, 3, False),
        (5, 3, 5, -1, False),
        (5, 3, 5, 5, False),
        (5, 3, 5, 3.0, False),
        (5, 3, 5, True, False),
        (5, None, 5, 3, True),
        (5, 3, 5, None, True),
        (1, 0, 3, 2, True),
    ],
)
def test_observation_progress(old_v, old_s, new_v, new_s, want):
    from sidera_control_plane.domain.session_observation import (
        session_observation_matches,
    )

    assert (
        session_observation_matches(
            expected_version=old_v,
            expected_last_sequence=old_s,
            current_version=new_v,
            current_last_sequence=new_s,
        )
        is want
    )
