from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import ActorType, AuditAggregateType, AuditEvent
from ..infrastructure.models import AuditEventModel


def write_audit_event(
    session: Session,
    *,
    aggregate_type: AuditAggregateType,
    aggregate_id: str,
    event_type: str,
    actor_id: str | None,
    actor_type: ActorType,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEventModel:
    previous = session.scalar(
        select(AuditEventModel)
        .where(
            AuditEventModel.aggregate_type == aggregate_type.value,
            AuditEventModel.aggregate_id == aggregate_id,
        )
        .order_by(AuditEventModel.occurred_at.desc())
        .limit(1)
    )
    event = AuditEvent(
        aggregate_type=aggregate_type,
        aggregate_id=UUID(aggregate_id),
        event_type=event_type,
        actor_type=actor_type,
        actor_id=UUID(actor_id) if actor_id else None,
        reason=reason,
        metadata=metadata or {},
        previous_event_hash=previous.event_hash if previous else None,
    )
    model = AuditEventModel(
        id=str(event.id),
        aggregate_type=event.aggregate_type.value,
        aggregate_id=str(event.aggregate_id),
        event_type=event.event_type,
        actor_id=str(event.actor_id) if event.actor_id else None,
        actor_type=event.actor_type.value,
        reason=event.reason,
        metadata_json=event.metadata,
        occurred_at=event.occurred_at,
        previous_event_hash=event.previous_event_hash,
        event_hash=event.event_hash,
    )
    session.add(model)
    return model
