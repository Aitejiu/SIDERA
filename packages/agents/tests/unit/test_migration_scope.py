import subprocess
import sys
import textwrap


def test_configure_uses_metadata_scoped_filters_in_isolated_process():
    script = textwrap.dedent(
        """
        from contextlib import nullcontext
        from pathlib import Path
        from runpy import run_path
        from types import SimpleNamespace

        from alembic import context
        from sqlalchemy import Column, Integer, MetaData, Table
        from sqlalchemy.schema import Index

        executed = []
        class Connection:
            def in_transaction(self):
                return True
            def execute(self, statement):
                executed.append(statement)

        options = {}
        context.config = SimpleNamespace(attributes={"connection": Connection()})
        context.configure = lambda **kwargs: options.update(kwargs)
        context.begin_transaction = nullcontext
        context.run_migrations = lambda: None
        run_path(str(Path("migrations/env.py").resolve()))
        assert executed

        include_name = options["include_name"]
        assert include_name("agents", "schema", {}) is True
        for schema in (None, "public", "control_plane", "product", "knowledge"):
            assert include_name(schema, "schema", {}) is False
        assert include_name(
            "profile_heads",
            "table",
            {
                "schema_name": "agents",
                "schema_qualified_table_name": "agents.profile_heads",
            },
        ) is True
        assert include_name(
            "user_notes",
            "table",
            {
                "schema_name": "agents",
                "schema_qualified_table_name": "agents.user_notes",
            },
        ) is False
        assert include_name(
            "profile_heads",
            "table",
            {
                "schema_name": "public",
                "schema_qualified_table_name": "public.profile_heads",
            },
        ) is False

        from sidera_agents.postgres.models import Base
        include_object = options["include_object"]
        owned = Base.metadata.tables["agents.profile_heads"]
        assert include_object(owned, "profile_heads", "table", True, None) is True
        assert include_object(owned, "profile_heads", "table", False, None) is True
        index = Index("ix_scope_profile", owned.c.profile_id)
        assert include_object(index, index.name, "index", True, None) is True

        unowned = Table(
            "user_notes", MetaData(), Column("id", Integer, primary_key=True),
            schema="agents",
        )
        assert include_object(unowned, "user_notes", "table", True, None) is False
        foreign = Table(
            "profile_heads", MetaData(), Column("id", Integer, primary_key=True),
            schema="product",
        )
        assert include_object(foreign, "profile_heads", "table", True, None) is False

        new_owned = Table(
            "new_package_owned_table", Base.metadata,
            Column("id", Integer, primary_key=True), schema="agents",
        )
        try:
            assert include_name(
                new_owned.name,
                "table",
                {
                    "schema_name": "agents",
                    "schema_qualified_table_name": new_owned.key,
                },
            ) is True
            assert include_object(new_owned, new_owned.name, "table", False, None) is True
        finally:
            Base.metadata.remove(new_owned)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
