import os
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_product.cli import migrate_runtime
from tests.support.controlled_knowledge import OwnedKnowledgeServer
from tests.support.product_lab import build_lab, write_evidence
from tests.support.product_targets import acquire_guards, select_targets

_retained_incomplete = []


@pytest.fixture(scope="session")
def selected_product_targets(tmp_path_factory):
    targets = select_targets(os.environ)
    if targets is None:
        pytest.skip("NOT_RUN: four explicit product target selections are absent")
    directory = os.environ.get("SIDERA_PRODUCT_TEST_EVIDENCE_DIRECTORY")
    evidence = None if directory is None else Path(directory)
    guards = acquire_guards(targets)
    safe = True
    write_evidence(
        evidence, "targets-guarded-before-mutation", [guard.initial for guard in guards]
    )
    migration_lab = build_lab(
        targets, tmp_path_factory.mktemp("product-migration"), evidence
    )
    try:
        migrate_runtime(migration_lab.runtime)
        assert all(migration_lab.runtime.inspect_readiness().values())
    finally:
        safe = migration_lab.safe_close()
        if not safe:
            _retained_incomplete.extend((migration_lab, guards))
            pytest.fail("Product migration owner cleanup incomplete; guards retained")
    write_evidence(evidence, "targets-migrated", [guard.inspect() for guard in guards])
    try:
        yield targets, guards, evidence
    finally:
        if _retained_incomplete:
            pytest.fail(
                "Product cleanup incomplete; resources are retained, no idle handoff"
            )
        final = [guard.inspect() for guard in guards]
        write_evidence(evidence, "targets-idle-before-release", final)
        assert all(all(value == 0 for value in item["idle"].values()) for item in final)
        released = {
            guard.target.component: guard.release() for guard in reversed(guards)
        }
        write_evidence(evidence, "guards-released", released)
        assert all(released.values())


@pytest.fixture
def product_lab(selected_product_targets, tmp_path):
    targets, _, evidence = selected_product_targets
    lab = build_lab(targets, tmp_path, evidence)
    lab.runtime.settings.workspace_root.mkdir(mode=0o700)
    try:
        yield lab
    finally:
        if not lab.safe_close():
            _retained_incomplete.append(lab)
            pytest.fail(
                "Product native owner cleanup incomplete; stores/guards retained"
            )


@pytest.fixture
def planning_product_lab(selected_product_targets, tmp_path):
    targets, _, evidence = selected_product_targets
    knowledge = OwnedKnowledgeServer(uuid4())
    knowledge.start()
    lab = None
    try:
        lab = build_lab(
            targets,
            tmp_path,
            evidence,
            workspace_id=knowledge.workspace,
            knowledge_url=knowledge.url,
            knowledge_token=knowledge.token,
        )
        lab.runtime.settings.workspace_root.mkdir(mode=0o700)
        yield lab, knowledge
    finally:
        if lab is not None and not lab.safe_close():
            _retained_incomplete.extend((lab, knowledge))
            pytest.fail("Original product cleanup pending; external fixture retained")
        complete = knowledge.close()
        write_evidence(evidence, "external-knowledge-closed", knowledge.facts())
        if not complete:
            _retained_incomplete.append(knowledge)
            pytest.fail(
                "Owned external HTTP thread cleanup incomplete; guards retained"
            )
