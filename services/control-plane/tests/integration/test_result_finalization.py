from __future__ import annotations

from uuid import uuid4


def _start_verification_writer(result_scenario, candidate):
    request_id = result_scenario.verification_service.schedule(
        workspace_id=result_scenario.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )
    result_scenario.tick()
    request = result_scenario.request(request_id)
    assert request.first_execution_id is not None
    result_scenario._planning._start_source(
        request.first_execution_id,
        key="summary",
    )
    return request.first_execution_id


def test_default_acceptance_completes_without_applying_branch(result_scenario) -> None:
    result_scenario.seed_plan(apply=False)
    candidate = result_scenario.candidate()
    before = result_scenario.target_commit()

    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))
    result_scenario.drive()

    assert result_scenario.root().status == "succeeded"
    assert result_scenario.target_commit() == before
    assert len(result_scenario.events("TaskCompleted")) == 1


def test_result_worker_finalizes_default_acceptance_without_apply(result_scenario) -> None:
    result_scenario.seed_plan(apply=False)
    candidate = result_scenario.candidate()
    before = result_scenario.target_commit()

    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))
    outcome = result_scenario.result_worker.run_once()

    assert outcome.state == "completed"
    assert result_scenario.root().status == "succeeded"
    assert result_scenario.target_commit() == before
    finalization = next(
        item for item in result_scenario.result_operations()
        if item.operation_kind == "finalization"
    )
    assert finalization.status.value == "succeeded"


def test_active_writer_keeps_accepted_root_pending(result_scenario) -> None:
    result_scenario.seed_plan(apply=False)
    candidate = result_scenario.candidate()
    _start_verification_writer(result_scenario, candidate)
    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))

    first = result_scenario.drive()
    second = result_scenario.drive()

    assert first.state == "pending"
    assert second.state == "pending"
    assert first.blocking_execution_ids == second.blocking_execution_ids
    assert len(first.blocking_execution_ids) == 1
    assert result_scenario.root().status != "succeeded"
    assert len(result_scenario.events("ExecutionStopRequested")) == 1
    assert result_scenario.events("TaskCompleted") == ()
    with result_scenario.uow() as uow:
        execution = uow.executions.get(
            first.blocking_execution_ids[0], result_scenario.workspace_id
        )
    assert execution is not None
    assert execution.stop_requested is True


def test_unstarted_request_is_cancelled_before_default_completion(result_scenario) -> None:
    result_scenario.seed_plan(apply=False)
    candidate = result_scenario.candidate()
    request_id = result_scenario.verification_service.schedule(
        workspace_id=result_scenario.workspace_id,
        candidate_id=candidate.id,
        command_id=uuid4(),
    )
    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))

    outcome = result_scenario.drive()

    assert outcome.state == "completed"
    assert result_scenario.request(request_id).status.value == "cancelled"
    assert len(result_scenario.events("ExecutionRequestCanceled")) == 1
    assert result_scenario.root().status == "succeeded"


def test_unknown_writer_state_blocks_accepted_root(result_scenario) -> None:
    result_scenario.seed_plan(apply=False)
    candidate = result_scenario.candidate()
    verification_execution_id = _start_verification_writer(
        result_scenario, candidate
    )
    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))

    with result_scenario.uow() as uow:
        execution = uow.executions.get(
            verification_execution_id, result_scenario.workspace_id
        )
        assert execution is not None
        assert execution.active_lease_id is not None
        lease = uow.leases.get(execution.active_lease_id, result_scenario.workspace_id)
        assert lease is not None
        previous_version = lease.version
        lease.revoke(now=lease.issued_at)
        uow.leases.save(lease, previous_version)

    outcome = result_scenario.drive()

    assert outcome.state == "blocked"
    assert outcome.failure_code == "writer_state_unknown"
    assert outcome.blocking_execution_ids == (execution.id,)
    assert result_scenario.root().status != "succeeded"
    assert result_scenario.events("TaskCompleted") == ()
