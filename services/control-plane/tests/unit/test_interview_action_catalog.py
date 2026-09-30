import json
from pathlib import Path

from sidera_control_plane.demo.checks import action_catalog_ids, validate_action_catalog


def test_action_catalog_has_unique_ids_and_all_feature_groups() -> None:
    path = Path(__file__).parents[4] / "apps" / "web" / "tests" / "interview" / "action-catalog.json"

    ids = action_catalog_ids(path)

    assert ids
    assert len(ids) == len(set(ids))
    assert validate_action_catalog(path) == ()
    assert {value.split("-", 1)[0] for value in ids} == {
        f"F{number:02d}" for number in range(1, 19)
    }


def test_action_catalog_validation_rejects_duplicate_and_unknown_ids(tmp_path: Path) -> None:
    path = tmp_path / "action-catalog.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {"id": "F01-01"},
                    {"id": "F01-01"},
                    {"id": "F99-01"},
                ],
            }
        ),
        encoding="utf-8",
    )

    errors = validate_action_catalog(path)

    assert "action_id_duplicate:F01-01" in errors
    assert "action_id_invalid:F99-01" in errors
    assert "action_group_missing:F18" in errors
