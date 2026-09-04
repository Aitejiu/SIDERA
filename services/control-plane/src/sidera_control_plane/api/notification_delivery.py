from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from sidera_control_plane.api.dependencies import (
    get_notification_delivery_service,
    get_settings,
    require_loopback_adapter_workflow,
)
from sidera_control_plane.api.schemas import (
    NotificationDeliveryAckRequest,
    NotificationDeliveryClaimRequest,
    NotificationDeliveryClaimResponse,
    NotificationDeliveryEnvelope,
    NotificationDeliveryNackRequest,
    NotificationDeliverySettlementResponse,
)
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.config import Settings

router = APIRouter(prefix="/api/v1/workspaces", tags=["notification-delivery"])

_WORKFLOW_DESCRIPTION = (
    "trusted loopback Adapter workflow; claim fencing is not authentication."
)


@router.post(
    "/{workspace_id}/notification-deliveries/claims",
    response_model=NotificationDeliveryClaimResponse,
    operation_id="claim_notification_deliveries",
    description=_WORKFLOW_DESCRIPTION,
)
def claim_notification_deliveries(
    workspace_id: UUID,
    request: NotificationDeliveryClaimRequest,
    service: Annotated[
        NotificationDeliveryService, Depends(get_notification_delivery_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NotificationDeliveryClaimResponse:
    require_loopback_adapter_workflow(settings)
    envelopes = service.claim(
        workspace_id,
        request.module_instance_id,
        supported_contract_versions=request.supported_contract_versions,
        batch_size=request.batch_size,
        lease_seconds=request.lease_seconds,
    )
    return NotificationDeliveryClaimResponse(
        deliveries=tuple(NotificationDeliveryEnvelope.from_view(item) for item in envelopes)
    )


@router.post(
    "/{workspace_id}/notification-deliveries/{delivery_id}/ack",
    response_model=NotificationDeliverySettlementResponse,
    operation_id="ack_notification_delivery",
    description=_WORKFLOW_DESCRIPTION,
)
def ack_notification_delivery(
    workspace_id: UUID,
    delivery_id: UUID,
    request: NotificationDeliveryAckRequest,
    service: Annotated[
        NotificationDeliveryService, Depends(get_notification_delivery_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NotificationDeliverySettlementResponse:
    require_loopback_adapter_workflow(settings)
    result = service.ack(
        workspace_id,
        delivery_id,
        claim_token=request.claim_token,
        fencing_token=request.fencing_token,
        completed_at=request.completed_at,
        provider_message_ref=request.provider_message_ref,
    )
    return NotificationDeliverySettlementResponse.from_result(result)

@router.post(
    "/{workspace_id}/notification-deliveries/{delivery_id}/nack",
    response_model=NotificationDeliverySettlementResponse,
    operation_id="nack_notification_delivery",
    description=_WORKFLOW_DESCRIPTION,
)
def nack_notification_delivery(
    workspace_id: UUID,
    delivery_id: UUID,
    request: NotificationDeliveryNackRequest,
    service: Annotated[
        NotificationDeliveryService, Depends(get_notification_delivery_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NotificationDeliverySettlementResponse:
    require_loopback_adapter_workflow(settings)
    result = service.nack(
        workspace_id,
        delivery_id,
        claim_token=request.claim_token,
        fencing_token=request.fencing_token,
        outcome=request.outcome,
        error_code=request.stable_error_code,
        retry_after_seconds=request.retry_after_seconds,
        provider_message_ref=request.provider_message_ref,
    )
    return NotificationDeliverySettlementResponse.from_result(result)
