def session_observation_matches(
    *,
    expected_version: int | None,
    expected_last_sequence: int | None,
    current_version: int | None,
    current_last_sequence: int | None,
) -> bool:
    if (
        type(expected_version) is not int
        or expected_version < 1
        or type(current_version) is not int
        or current_version < 1
    ):
        return False
    for version, sequence in (
        (expected_version, expected_last_sequence),
        (current_version, current_last_sequence),
    ):
        if sequence is not None and (
            type(sequence) is not int or sequence < 0 or sequence >= version
        ):
            return False
    if expected_last_sequence is None or current_last_sequence is None:
        return expected_version == current_version
    return (
        current_version >= expected_version
        and current_last_sequence >= expected_last_sequence
        and current_version - expected_version
        == current_last_sequence - expected_last_sequence
    )
