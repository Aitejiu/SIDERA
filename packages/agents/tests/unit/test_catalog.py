from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sidera_agents.contracts import CredentialRef, ResourceRef, SecretBinding
from sidera_agents.errors import AgentError


@pytest.mark.parametrize("entity", ["profile", "resource"])
def test_explicit_create_identity_is_preserved_and_never_overwritten(
    catalog_lab, entity
):
    identity = uuid4()
    catalog = catalog_lab.catalog
    create = getattr(catalog, "create_" + entity)
    draft = catalog_lab.draft if entity == "profile" else catalog_lab.skill_draft
    kwargs = {entity + "_id": identity, "grant_id": catalog_lab.grant_id}

    saved = create(catalog_lab.workspace_id, draft, **kwargs)
    assert getattr(saved.ref, entity + "_id") == identity
    assert saved.ref.revision == 1
    with pytest.raises(AgentError) as error:
        create(
            catalog_lab.workspace_id,
            draft.model_copy(update={"name": "other"}),
            **kwargs,
        )
    assert error.value.code == "operation_conflict"
    assert (
        getattr(catalog, "get_" + entity)(saved.ref, grant_id=catalog_lab.grant_id)
        == saved
    )


@pytest.mark.parametrize("entity", ["profile", "resource"])
@pytest.mark.parametrize("identity", ["not-a-uuid", 1, True])
def test_explicit_create_identity_is_validated_before_store(
    catalog_lab, entity, identity, monkeypatch
):
    writes = []
    monkeypatch.setattr(
        catalog_lab.catalog._store,
        "create_" + entity,
        lambda *args: writes.append(args),
    )
    draft = catalog_lab.draft if entity == "profile" else catalog_lab.skill_draft

    with pytest.raises(AgentError) as error:
        getattr(catalog_lab.catalog, "create_" + entity)(
            catalog_lab.workspace_id,
            draft,
            grant_id=catalog_lab.grant_id,
            **{entity + "_id": identity},
        )
    assert error.value.code == "configuration_invalid"
    assert writes == []


@pytest.mark.parametrize("entity", ["profile", "resource"])
def test_explicit_create_denial_happens_before_store(catalog_lab, entity, monkeypatch):
    writes = []
    monkeypatch.setattr(
        catalog_lab.catalog._store,
        "create_" + entity,
        lambda *args: writes.append(args),
    )
    draft = catalog_lab.draft if entity == "profile" else catalog_lab.skill_draft

    with pytest.raises(AgentError) as error:
        getattr(catalog_lab.catalog, "create_" + entity)(
            catalog_lab.workspace_id,
            draft,
            grant_id=uuid4(),
            **{entity + "_id": uuid4()},
        )
    assert error.value.code == "permission_denied"
    assert writes == []


@pytest.mark.parametrize("entity", ["profile", "resource"])
def test_omitted_create_identity_still_generates_independent_ids(catalog_lab, entity):
    create = getattr(catalog_lab.catalog, "create_" + entity)
    draft = catalog_lab.draft if entity == "profile" else catalog_lab.skill_draft
    first = create(catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id)
    second = create(catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id)
    assert first.ref != second.ref
    assert first.ref.revision == second.ref.revision == 1


@pytest.mark.parametrize("entity", ["profile", "resource"])
def test_status_authorization_uses_public_typed_target(catalog_lab, entity):
    from sidera_agents import api, contracts

    target_type = getattr(contracts, "CatalogStatusTarget", None)
    assert target_type is not None
    assert getattr(api, "CatalogStatusTarget", None) is target_type
    draft = catalog_lab.draft if entity == "profile" else catalog_lab.skill_draft
    saved = getattr(catalog_lab.catalog, "create_" + entity)(
        catalog_lab.workspace_id,
        draft,
        grant_id=catalog_lab.grant_id,
    )
    identity = getattr(saved.ref, entity + "_id")
    getattr(catalog_lab.catalog, "set_" + entity + "_enabled")(
        catalog_lab.workspace_id,
        identity,
        False,
        1,
        grant_id=catalog_lab.grant_id,
    )
    assert catalog_lab.authorizer.calls[-1] == (
        catalog_lab.grant_id,
        entity + ".disable",
        target_type(
            workspace_id=catalog_lab.workspace_id, entity=entity, entity_id=identity
        ),
    )


def test_profile_pins_resource_revision(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    resource = catalog.create_resource(
        catalog_lab.workspace_id,
        catalog_lab.skill_draft,
        grant_id=catalog_lab.grant_id,
    )
    draft = catalog_lab.draft.model_copy(update={"resource_refs": (resource.ref,)})
    profile = catalog.create_profile(
        catalog_lab.workspace_id,
        draft,
        grant_id=catalog_lab.grant_id,
    )

    catalog.revise_resource(
        resource.ref,
        resource.draft.model_copy(update={"name": "new"}),
        grant_id=catalog_lab.grant_id,
    )

    saved = catalog.get_profile(profile.ref, grant_id=catalog_lab.grant_id)
    assert saved.draft.resource_refs == (resource.ref,)
    revised_resource = catalog.get_resource(
        ResourceRef(
            workspace_id=resource.ref.workspace_id,
            resource_id=resource.ref.resource_id,
            revision=2,
        ),
        grant_id=catalog_lab.grant_id,
    )
    new_profile = catalog.create_profile(
        catalog_lab.workspace_id,
        catalog_lab.draft.model_copy(update={"resource_refs": (revised_resource.ref,)}),
        grant_id=catalog_lab.grant_id,
    )
    assert new_profile.config_hash != saved.config_hash


def test_profile_revisions_are_append_only_and_hash_deterministically(
    catalog_lab,
) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_profile(
        catalog_lab.workspace_id, catalog_lab.draft, grant_id=catalog_lab.grant_id
    )
    second = catalog.revise_profile(
        first.ref, first.draft, grant_id=catalog_lab.grant_id
    )
    third = catalog.create_profile(
        catalog_lab.workspace_id, catalog_lab.draft, grant_id=catalog_lab.grant_id
    )

    assert second.ref.revision == 2
    assert second.config_hash == first.config_hash
    assert third.config_hash == first.config_hash
    assert catalog.get_profile(first.ref, grant_id=catalog_lab.grant_id) == first


def test_stale_profile_revision_cannot_overwrite_the_head(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_profile(
        catalog_lab.workspace_id, catalog_lab.draft, grant_id=catalog_lab.grant_id
    )
    changed = first.draft.model_copy(
        update={"instructions": "Use the review checklist."}
    )
    catalog.revise_profile(first.ref, changed, grant_id=catalog_lab.grant_id)

    with pytest.raises(AgentError, match="stale") as error:
        catalog.revise_profile(first.ref, first.draft, grant_id=catalog_lab.grant_id)

    assert error.value.code == "revision_conflict"


def test_competing_profile_writers_from_one_head_have_one_winner(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_profile(
        catalog_lab.workspace_id, catalog_lab.draft, grant_id=catalog_lab.grant_id
    )
    barrier = Barrier(3)

    def revise(instructions: str) -> str:
        barrier.wait()
        draft = first.draft.model_copy(update={"instructions": instructions})
        try:
            catalog.revise_profile(first.ref, draft, grant_id=catalog_lab.grant_id)
        except AgentError as error:
            return error.code
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_writer = pool.submit(revise, "Writer one")
        second_writer = pool.submit(revise, "Writer two")
        barrier.wait()
        outcomes = (first_writer.result(), second_writer.result())

    assert sorted(outcomes) == ["revision_conflict", "saved"]


def test_memory_catalog_lists_current_resource_heads(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    changed = catalog.revise_resource(
        first.ref,
        first.draft.model_copy(update={"name": "updated"}),
        grant_id=catalog_lab.grant_id,
    )

    listed = catalog.list_resources(
        catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
    )

    assert tuple(item.ref for item in listed) == (changed.ref,)


def test_resource_revisions_are_append_only_with_stable_hashes(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    same_content = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    updated = catalog.revise_resource(
        first.ref,
        first.draft.model_copy(update={"name": "new review"}),
        grant_id=catalog_lab.grant_id,
    )

    assert same_content.content_hash == first.content_hash
    assert updated.ref.revision == 2
    assert updated.content_hash != first.content_hash
    assert (
        catalog.get_resource(first.ref, grant_id=catalog_lab.grant_id).draft.name
        == "review"
    )


def test_stale_resource_revision_cannot_overwrite_the_head(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    changed = first.draft.model_copy(update={"name": "updated review"})
    catalog.revise_resource(first.ref, changed, grant_id=catalog_lab.grant_id)

    with pytest.raises(AgentError, match="stale") as error:
        catalog.revise_resource(first.ref, first.draft, grant_id=catalog_lab.grant_id)

    assert error.value.code == "revision_conflict"


def test_competing_resource_writers_from_one_head_have_one_winner(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    first = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    barrier = Barrier(3)

    def revise(name: str) -> str:
        barrier.wait()
        draft = first.draft.model_copy(update={"name": name})
        try:
            catalog.revise_resource(first.ref, draft, grant_id=catalog_lab.grant_id)
        except AgentError as error:
            return error.code
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_writer = pool.submit(revise, "Resource one")
        second_writer = pool.submit(revise, "Resource two")
        barrier.wait()
        outcomes = (first_writer.result(), second_writer.result())

    assert sorted(outcomes) == ["revision_conflict", "saved"]


def test_returned_profile_snapshots_cannot_mutate_saved_configuration(
    catalog_lab,
) -> None:
    nested = {"nested": {"items": ["original"]}}
    draft = catalog_lab.draft.model_copy(update={"native_settings": nested})
    profile = catalog_lab.catalog.create_profile(
        catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
    )
    returned = catalog_lab.catalog.get_profile(
        profile.ref, grant_id=catalog_lab.grant_id
    )
    returned.draft.native_settings["nested"]["items"].append("mutation")

    saved = catalog_lab.catalog.get_profile(profile.ref, grant_id=catalog_lab.grant_id)
    assert saved.draft.native_settings == {"nested": {"items": ["original"]}}


def test_catalog_lists_current_heads_in_stable_order(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    older = catalog.create_profile(
        catalog_lab.workspace_id, catalog_lab.draft, grant_id=catalog_lab.grant_id
    )
    newer_id_draft = catalog_lab.draft.model_copy(update={"name": "another"})
    newer = catalog.create_profile(
        catalog_lab.workspace_id, newer_id_draft, grant_id=catalog_lab.grant_id
    )
    revised = catalog.revise_profile(
        older.ref,
        older.draft.model_copy(update={"instructions": "Revised."}),
        grant_id=catalog_lab.grant_id,
    )

    listed = catalog.list_profiles(
        catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
    )
    assert {item.ref.profile_id: item.ref.revision for item in listed} == {
        older.ref.profile_id: revised.ref.revision,
        newer.ref.profile_id: 1,
    }
    assert tuple(str(item.ref.profile_id) for item in listed) == tuple(
        sorted(str(item.ref.profile_id) for item in listed)
    )


def test_profile_and_resource_enabled_states_use_version_cas(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    profile = catalog.create_profile(
        catalog_lab.workspace_id, catalog_lab.draft, grant_id=catalog_lab.grant_id
    )
    resource = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    with pytest.raises(AgentError) as denied:
        catalog.set_profile_enabled(
            catalog_lab.workspace_id,
            profile.ref.profile_id,
            False,
            1,
            grant_id=uuid4(),
        )
    assert denied.value.code == "permission_denied"

    profile_status = catalog.set_profile_enabled(
        catalog_lab.workspace_id,
        profile.ref.profile_id,
        False,
        1,
        grant_id=catalog_lab.grant_id,
    )
    resource_status = catalog.set_resource_enabled(
        catalog_lab.workspace_id,
        resource.ref.resource_id,
        False,
        1,
        grant_id=catalog_lab.grant_id,
    )

    assert (profile_status.enabled, profile_status.version) == (False, 2)
    assert (resource_status.enabled, resource_status.version) == (False, 2)
    profile_action = catalog_lab.authorizer.calls[-2]
    resource_action = catalog_lab.authorizer.calls[-1]
    assert profile_action[1] == "profile.disable"
    assert profile_action[2].entity == "profile"
    assert profile_action[2].entity_id == profile.ref.profile_id
    assert resource_action[1] == "resource.disable"
    assert resource_action[2].entity == "resource"
    assert resource_action[2].entity_id == resource.ref.resource_id
    with pytest.raises(AgentError) as profile_error:
        catalog.set_profile_enabled(
            catalog_lab.workspace_id,
            profile.ref.profile_id,
            True,
            1,
            grant_id=catalog_lab.grant_id,
        )
    with pytest.raises(AgentError) as resource_error:
        catalog.set_resource_enabled(
            catalog_lab.workspace_id,
            resource.ref.resource_id,
            True,
            1,
            grant_id=catalog_lab.grant_id,
        )
    assert profile_error.value.code == resource_error.value.code == "revision_conflict"
    assert catalog.get_profile(profile.ref, grant_id=catalog_lab.grant_id) == profile
    assert catalog.get_resource(resource.ref, grant_id=catalog_lab.grant_id) == resource


def test_disabled_resource_cannot_be_attached_to_a_new_profile(catalog_lab) -> None:
    catalog = catalog_lab.catalog
    resource = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    saved_profile = catalog.create_profile(
        catalog_lab.workspace_id,
        catalog_lab.draft.model_copy(update={"resource_refs": (resource.ref,)}),
        grant_id=catalog_lab.grant_id,
    )
    catalog.set_resource_enabled(
        catalog_lab.workspace_id,
        resource.ref.resource_id,
        False,
        1,
        grant_id=catalog_lab.grant_id,
    )

    with pytest.raises(AgentError) as error:
        catalog.create_profile(
            catalog_lab.workspace_id,
            catalog_lab.draft.model_copy(update={"resource_refs": (resource.ref,)}),
            grant_id=catalog_lab.grant_id,
        )

    assert error.value.code == "configuration_invalid"
    assert (
        catalog.get_profile(saved_profile.ref, grant_id=catalog_lab.grant_id)
        == saved_profile
    )


def test_profile_rejects_nonexistent_cross_workspace_and_duplicate_resource_refs(
    catalog_lab,
) -> None:
    catalog = catalog_lab.catalog
    missing = ResourceRef(
        workspace_id=catalog_lab.workspace_id, resource_id=uuid4(), revision=1
    )
    foreign = ResourceRef(workspace_id=uuid4(), resource_id=uuid4(), revision=1)

    with pytest.raises(AgentError) as missing_error:
        catalog.create_profile(
            catalog_lab.workspace_id,
            catalog_lab.draft.model_copy(update={"resource_refs": (missing,)}),
            grant_id=catalog_lab.grant_id,
        )
    with pytest.raises(AgentError) as foreign_error:
        catalog.create_profile(
            catalog_lab.workspace_id,
            catalog_lab.draft.model_copy(update={"resource_refs": (foreign,)}),
            grant_id=catalog_lab.grant_id,
        )

    resource = catalog.create_resource(
        catalog_lab.workspace_id, catalog_lab.skill_draft, grant_id=catalog_lab.grant_id
    )
    with pytest.raises(AgentError) as duplicate_error:
        catalog.create_profile(
            catalog_lab.workspace_id,
            catalog_lab.draft.model_copy(
                update={"resource_refs": (resource.ref, resource.ref)}
            ),
            grant_id=catalog_lab.grant_id,
        )

    assert missing_error.value.code == "not_found"
    assert foreign_error.value.code == "configuration_invalid"
    assert duplicate_error.value.code == "configuration_invalid"


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", {"PATH": "/tmp"}),
        ("environment", {"OPENCODE_CONFIG_DIR": "/tmp/opencode"}),
        ("environment", {"OPENAI_API_KEY": "plain-secret-value"}),
        ("native_settings", {"api_key": "plain-secret-value"}),
        ("native_settings", {"endpoint": "Bearer plain-secret-value"}),
    ],
)
def test_profile_rejects_reserved_or_secret_configuration(
    catalog_lab, field, value
) -> None:
    draft = catalog_lab.draft.model_copy(update={field: value})

    with pytest.raises(AgentError) as error:
        catalog_lab.catalog.create_profile(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )

    assert error.value.code == "configuration_invalid"


def test_resource_rejects_secret_configuration_and_empty_name(catalog_lab) -> None:
    secret_settings = catalog_lab.skill_draft.model_copy(
        update={"settings": {"access_token": "plain-secret-value"}}
    )
    empty_name = catalog_lab.skill_draft.model_copy(update={"name": "  "})

    with pytest.raises(AgentError) as secret_error:
        catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id, secret_settings, grant_id=catalog_lab.grant_id
        )
    with pytest.raises(AgentError) as name_error:
        catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id, empty_name, grant_id=catalog_lab.grant_id
        )

    assert secret_error.value.code == name_error.value.code == "configuration_invalid"


def test_secret_references_are_workspace_scoped_and_have_unique_destinations(
    catalog_lab,
) -> None:
    credential = CredentialRef(
        workspace_id=catalog_lab.workspace_id, credential_id=uuid4()
    )
    duplicate = SecretBinding(credential_ref=credential, destination="API_TOKEN")
    draft = catalog_lab.draft.model_copy(update={"secret_refs": (duplicate, duplicate)})

    with pytest.raises(AgentError) as duplicate_error:
        catalog_lab.catalog.create_profile(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )

    foreign = SecretBinding(
        credential_ref=CredentialRef(workspace_id=uuid4(), credential_id=uuid4()),
        destination="API_TOKEN",
    )
    foreign_draft = catalog_lab.draft.model_copy(update={"secret_refs": (foreign,)})
    with pytest.raises(AgentError) as foreign_error:
        catalog_lab.catalog.create_profile(
            catalog_lab.workspace_id, foreign_draft, grant_id=catalog_lab.grant_id
        )

    assert (
        duplicate_error.value.code
        == foreign_error.value.code
        == "configuration_invalid"
    )


def test_authorization_is_checked_before_catalog_lookup(catalog_lab) -> None:
    nonexistent = ResourceRef(
        workspace_id=catalog_lab.workspace_id, resource_id=uuid4(), revision=1
    )

    with pytest.raises(AgentError) as error:
        catalog_lab.catalog.get_resource(nonexistent, grant_id=uuid4())

    assert error.value.code == "permission_denied"
    grant_id, action, target = catalog_lab.authorizer.calls[-1]
    assert grant_id != catalog_lab.grant_id
    assert action == "resource.read"
    assert target == nonexistent


@pytest.mark.parametrize("kind", ["profile", "resource"])
def test_public_status_reads_current_identity_without_changing_pinned_history(
    catalog_lab, kind
) -> None:
    catalog = catalog_lab.catalog
    draft = catalog_lab.draft if kind == "profile" else catalog_lab.skill_draft
    create = getattr(catalog, f"create_{kind}")
    get_revision = getattr(catalog, f"get_{kind}")
    get_status = getattr(catalog, f"get_{kind}_status")
    revise = getattr(catalog, f"revise_{kind}")
    set_enabled = getattr(catalog, f"set_{kind}_enabled")
    first = create(catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id)
    identity = getattr(first.ref, f"{kind}_id")

    initial = get_status(first.ref, grant_id=catalog_lab.grant_id)
    assert (initial.enabled, initial.version) == (True, 1)
    disabled = set_enabled(
        catalog_lab.workspace_id, identity, False, 1, grant_id=catalog_lab.grant_id
    )
    assert get_status(first.ref, grant_id=catalog_lab.grant_id) == disabled
    second = revise(
        first.ref,
        draft.model_copy(update={"name": "revised"}),
        grant_id=catalog_lab.grant_id,
    )
    assert second.ref.revision == 2

    for ref in (first.ref, second.ref, first.ref):
        status = get_status(ref, grant_id=catalog_lab.grant_id)
        assert getattr(status, f"{kind}_id") == identity
        assert (status.enabled, status.version) == (False, 2)
        assert catalog_lab.authorizer.calls[-1] == (
            catalog_lab.grant_id,
            f"{kind}.read",
            ref,
        )
    assert get_revision(first.ref, grant_id=catalog_lab.grant_id) == first
    assert get_revision(second.ref, grant_id=catalog_lab.grant_id) == second


@pytest.mark.parametrize("kind", ["profile", "resource"])
@pytest.mark.parametrize("target_case", ["unauthorized", "foreign", "missing"])
def test_public_status_authorizes_exact_ref_before_missing_lookup(
    catalog_lab, kind, target_case
) -> None:
    from sidera_agents.contracts import ProfileRef

    ref_type = ProfileRef if kind == "profile" else ResourceRef
    ref = ref_type(
        workspace_id=(
            uuid4() if target_case == "foreign" else catalog_lab.workspace_id
        ),
        **{f"{kind}_id": uuid4()},
        revision=7,
    )
    if target_case == "unauthorized":
        draft = catalog_lab.draft if kind == "profile" else catalog_lab.skill_draft
        ref = getattr(catalog_lab.catalog, f"create_{kind}")(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        ).ref
    grant_id = uuid4() if target_case != "foreign" else catalog_lab.grant_id
    get_status = getattr(catalog_lab.catalog, f"get_{kind}_status")
    with pytest.raises(AgentError) as denied:
        get_status(ref, grant_id=grant_id)

    assert denied.value.code == "permission_denied"
    assert catalog_lab.authorizer.calls[-1] == (grant_id, f"{kind}.read", ref)


@pytest.mark.parametrize("kind", ["profile", "resource"])
@pytest.mark.parametrize("revision", [0, 2])
def test_public_status_requires_the_supplied_revision_to_exist(
    catalog_lab, kind, revision
) -> None:
    catalog = catalog_lab.catalog
    draft = catalog_lab.draft if kind == "profile" else catalog_lab.skill_draft
    first = getattr(catalog, f"create_{kind}")(
        catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
    )
    get_status = getattr(catalog, f"get_{kind}_status")
    missing_revision = first.ref.model_copy(update={"revision": revision})
    with pytest.raises(AgentError) as missing:
        get_status(missing_revision, grant_id=catalog_lab.grant_id)
    assert missing.value.code == "not_found"
    assert catalog_lab.authorizer.calls[-1] == (
        catalog_lab.grant_id,
        f"{kind}.read",
        missing_revision,
    )
    unchanged = get_status(first.ref, grant_id=catalog_lab.grant_id)
    assert (unchanged.enabled, unchanged.version) == (True, 1)


def test_profile_revision_revalidates_malformed_nested_json(catalog_lab) -> None:
    malformed = catalog_lab.skill_draft.model_copy(
        update={"settings": {"body": object()}}
    )

    with pytest.raises(ValidationError):
        catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id, malformed, grant_id=catalog_lab.grant_id
        )


def test_postgres_models_and_migration_are_scoped_to_agents_schema() -> None:
    package_dir = Path(__file__).resolve().parents[2]
    script = """
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sidera_agents.postgres.models import Base

base_expected = {
    "profile_heads",
    "profile_revisions",
    "resource_heads",
    "resource_revisions",
}
runtime_expected = {
    "runtime_installations",
    "runtime_references",
}
run_expected = {"run_operations", "runs", "run_reservations"}
input_event_expected = {"run_inputs", "run_events"}
expected = base_expected | runtime_expected | run_expected | input_event_expected
tables = Base.metadata.tables
assert {table.name for table in tables.values()} == expected
assert all(table.schema == "agents" for table in tables.values())
assert not any(
    foreign_key.column.table.schema != "agents"
    for table in tables.values()
    for foreign_key in table.foreign_keys
)
assert all(
    not tables[f"agents.{name}"].foreign_keys
    for name in run_expected | input_event_expected
)
assert set(tables["agents.run_inputs"].c.keys()) == {
    "workspace_id", "run_id", "operation_id", "fingerprint", "response_id",
    "queue_sequence", "status", "reason_code", "evidence_sequence", "text", "context",
}
assert set(tables["agents.run_events"].c.keys()) == {
    "run_id", "sequence", "occurred_at", "kind", "operation_id", "response_id",
    "status", "reason_code", "native_cursor", "data",
}
assert tables["agents.runs"].c.original_grant_id.nullable is True

path = Path("migrations/versions/0001_agent_catalog.py")
spec = spec_from_file_location("migration_0001_agent_catalog", path)
module = module_from_spec(spec)
spec.loader.exec_module(module)
buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql",
    opts={"as_sql": True, "output_buffer": buffer, "version_table_schema": "agents"},
)
assert context._version.schema == "agents"
with Operations.context(context):
    module.upgrade()
upgrade_sql = buffer.getvalue().lower()
assert "create schema if not exists agents" in upgrade_sql
assert all(f"create table agents.{name}" in upgrade_sql for name in base_expected)
assert not any(f"create table agents.{name}" in upgrade_sql for name in expected - base_expected)

buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
)
with Operations.context(context):
    module.downgrade()
downgrade_sql = buffer.getvalue().lower()
assert all(f"drop table agents.{name}" in downgrade_sql for name in base_expected)

path = Path("migrations/versions/0002_runtime_catalog.py")
spec = spec_from_file_location("migration_0002_runtime_catalog", path)
module = module_from_spec(spec)
spec.loader.exec_module(module)
buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql",
    opts={"as_sql": True, "output_buffer": buffer, "version_table_schema": "agents"},
)
with Operations.context(context):
    module.upgrade()
upgrade_sql = buffer.getvalue().lower()
assert all(
    f"create table agents.{name}" in upgrade_sql
    for name in runtime_expected
)
assert "auth_state" in upgrade_sql

buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
)
with Operations.context(context):
    module.downgrade()
downgrade_sql = buffer.getvalue().lower()
assert all(
    f"drop table agents.{name}" in downgrade_sql
    for name in runtime_expected
)

path = Path("migrations/versions/0003_agent_runs_operations.py")
spec = spec_from_file_location("migration_0003_agent_runs_operations", path)
module = module_from_spec(spec)
spec.loader.exec_module(module)
buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql",
    opts={"as_sql": True, "output_buffer": buffer, "version_table_schema": "agents"},
)
with Operations.context(context):
    module.upgrade()
upgrade_sql = buffer.getvalue().lower()
assert all(f"create table agents.{name}" in upgrade_sql for name in run_expected)
assert upgrade_sql.count("create table ") == len(run_expected)
assert "foreign key" not in upgrade_sql

buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
)
with Operations.context(context):
    module.downgrade()
downgrade_sql = buffer.getvalue().lower()
assert all(f"drop table agents.{name}" in downgrade_sql for name in run_expected)
assert downgrade_sql.count("drop table ") == len(run_expected)

path = Path("migrations/versions/0004_inputs_events.py")
spec = spec_from_file_location("migration_0004_inputs_events", path)
module = module_from_spec(spec)
spec.loader.exec_module(module)
buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql",
    opts={"as_sql": True, "output_buffer": buffer, "version_table_schema": "agents"},
)
with Operations.context(context):
    module.upgrade()
upgrade_sql = buffer.getvalue().lower()
assert all(f"create table agents.{name}" in upgrade_sql for name in input_event_expected)
assert upgrade_sql.count("create table ") == len(input_event_expected)
assert "foreign key" not in upgrade_sql
assert " ".join(upgrade_sql.split()) == (
    "alter table agents.runs add column original_grant_id uuid; "
    "create table agents.run_inputs ("
    " workspace_id uuid not null, run_id uuid not null, operation_id uuid not null,"
    " fingerprint varchar(71) not null, response_id uuid not null,"
    " queue_sequence integer not null, status varchar(24) not null,"
    " reason_code varchar(120), evidence_sequence integer not null, text text,"
    " context json, primary key (workspace_id, run_id, operation_id) ); "
    "alter table agents.run_inputs add constraint uq_run_inputs_run_queue_sequence"
    " unique (run_id, queue_sequence); "
    "create table agents.run_events ("
    " run_id uuid not null, sequence integer not null,"
    " occurred_at timestamp with time zone not null, kind varchar(80) not null,"
    " operation_id uuid, response_id uuid, status varchar(24) not null,"
    " reason_code varchar(120), native_cursor integer, data json not null,"
    " primary key (run_id, sequence) );"
)

buffer = StringIO()
context = MigrationContext.configure(
    dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
)
with Operations.context(context):
    module.downgrade()
downgrade_sql = buffer.getvalue().lower()
assert all(f"drop table agents.{name}" in downgrade_sql for name in input_event_expected)
assert downgrade_sql.count("drop table ") == len(input_event_expected)
assert " ".join(downgrade_sql.split()) == (
    "drop table agents.run_events; drop table agents.run_inputs; "
    "alter table agents.runs drop column original_grant_id;"
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=package_dir,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
