from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.domain.planning import TaskGoalSubmission
from sidera_control_plane.persistence.models import TaskGoalSubmissionModel


class TaskGoalRepository:
    def __init__(self, session: Session):
        self._session = session

    def add(self, submission: TaskGoalSubmission):
        self._session.add(
            TaskGoalSubmissionModel(
                workspace_id=submission.workspace_id,
                root_task_id=submission.root_task_id,
                planning_request_id=submission.planning_request_id,
                root_task_version=submission.root_task_version,
                principal_id=submission.principal_id,
                grant_id=submission.grant_id,
            )
        )

    def get(self, workspace_id, planning_request_id, *, for_update=False):
        statement = select(TaskGoalSubmissionModel).where(
            TaskGoalSubmissionModel.workspace_id == workspace_id,
            TaskGoalSubmissionModel.planning_request_id == planning_request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return (
            None
            if row is None
            else TaskGoalSubmission(
                row.workspace_id,
                row.root_task_id,
                row.planning_request_id,
                row.root_task_version,
                row.principal_id,
                row.grant_id,
            )
        )
