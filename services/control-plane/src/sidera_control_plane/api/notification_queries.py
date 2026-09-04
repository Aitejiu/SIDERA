from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sidera_control_plane.api.dependencies import (
    get_notification_query_service,
    get_settings,
    require_loopback_adapter_workflow,
)
from sidera_control_plane.api.schemas import (
    NotificationAttemptPageResponse,
    NotificationAttemptResponse,
    NotificationSummaryResponse,
    NotificationThreadDetailResponse,
    NotificationThreadPageResponse,
    NotificationThreadResponse,
)
from sidera_control_plane.application.notification_queries import (
    NotificationQueryService,
)
from sidera_control_plane.config import Settings

router = APIRouter(prefix="/api/v1/workspaces")


@router.get(
    "/{workspace_id}/notification-summary",
    response_model=NotificationSummaryResponse,
    operation_id="get_notification_summary",
)
def get_notification_summary(
    workspace_id: UUID,
    service: Annotated[
        NotificationQueryService, Depends(get_notification_query_service)
    ],
) -> NotificationSummaryResponse:
    return NotificationSummaryResponse.from_view(service.get_summary(workspace_id))


@router.get(
    "/{workspace_id}/notification-threads",
    response_model=NotificationThreadPageResponse,
    operation_id="list_notification_threads",
)
def list_notification_threads(
    workspace_id: UUID,
    service: Annotated[
        NotificationQueryService, Depends(get_notification_query_service)
    ],
    category: Annotated[Literal["action", "progress", "archived"], Query()] = "action",
    root_task_id: Annotated[UUID | None, Query()] = None,
    subtask_id: Annotated[UUID | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NotificationThreadPageResponse:
    page = service.list_threads(
        workspace_id,
        category=category,
        root_task_id=root_task_id,
        subtask_id=subtask_id,
        unread_only=unread_only,
        cursor=cursor,
        limit=limit,
    )
    return NotificationThreadPageResponse(
        items=tuple(
            NotificationThreadResponse.from_view(item)
            for item in page.items
        ),
        next_cursor=page.next_cursor,
        last_change_cursor=page.last_change_cursor,
    )


@router.get(
    "/{workspace_id}/notification-threads/{thread_id}",
    response_model=NotificationThreadDetailResponse,
    operation_id="get_notification_thread",
)
def get_notification_thread(
    workspace_id: UUID,
    thread_id: UUID,
    service: Annotated[
        NotificationQueryService, Depends(get_notification_query_service)
    ],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NotificationThreadDetailResponse:
    return NotificationThreadDetailResponse.from_view(
        service.get_thread_detail(
            workspace_id, thread_id, cursor=cursor, limit=limit
        )
    )


@router.get(
    "/{workspace_id}/notification-deliveries/{delivery_id}/attempts",
    response_model=NotificationAttemptPageResponse,
    operation_id="list_notification_delivery_attempts",
)
def list_notification_delivery_attempts(
    workspace_id: UUID,
    delivery_id: UUID,
    service: Annotated[
        NotificationQueryService, Depends(get_notification_query_service)
    ],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NotificationAttemptPageResponse:
    page = service.list_attempts(
        workspace_id, delivery_id, cursor=cursor, limit=limit
    )
    return NotificationAttemptPageResponse(
        items=tuple(NotificationAttemptResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{workspace_id}/notification-routes/{route_id}/threads",
    response_model=NotificationThreadPageResponse,
    operation_id="list_notification_route_threads",
)
def list_notification_route_threads(
    workspace_id: UUID,
    route_id: UUID,
    service: Annotated[
        NotificationQueryService, Depends(get_notification_query_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NotificationThreadPageResponse:
    require_loopback_adapter_workflow(settings)
    page = service.list_route_threads(
        workspace_id, route_id, cursor=cursor, limit=limit
    )
    return NotificationThreadPageResponse(
        items=tuple(NotificationThreadResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
        last_change_cursor=page.last_change_cursor,
    )
