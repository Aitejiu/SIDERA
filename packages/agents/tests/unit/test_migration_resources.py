from pathlib import Path

from alembic.config import Config


def _write_migration_layout(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[alembic]\nscript_location = %(here)s/migrations\n",
        encoding="utf-8",
    )
    (config_path.parent / "migrations" / "versions").mkdir(parents=True)


def test_alembic_config_resolves_source_and_installed_package_layouts(tmp_path):
    from sidera_agents.postgres.store import _resolve_alembic_config_path

    source_root = tmp_path / "source"
    source_package = source_root / "src" / "sidera_agents"
    source_config = source_root / "alembic.ini"
    _write_migration_layout(source_config)

    source_path = _resolve_alembic_config_path(source_package, source_root)
    source_config_obj = Config(str(source_path))
    assert source_path == source_config
    assert Path(source_config_obj.get_main_option("script_location")) == (
        source_root / "migrations"
    )

    installed_source_root = tmp_path / "venv" / "lib" / "python"
    site_packages = installed_source_root / "site-packages"
    installed_package = site_packages / "sidera_agents"
    installed_config = installed_package / "alembic.ini"
    _write_migration_layout(installed_config)
    _write_migration_layout(installed_source_root / "alembic.ini")

    installed_path = _resolve_alembic_config_path(
        installed_package, installed_source_root
    )
    installed_config_obj = Config(str(installed_path))
    assert installed_path == installed_config
    assert Path(installed_config_obj.get_main_option("script_location")) == (
        installed_package / "migrations"
    )
