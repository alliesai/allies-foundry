from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from runtime.contracts import (
    build_event_envelope,
    event_envelope_bytes,
)
from runtime.exceptions import RuntimeConflictError, RuntimeValidationError
from runtime.models import (
    EventDeliveryState,
    ExecutionEvent,
    ExecutionEventDelivery,
)

DELIVERY_LEASE_SECONDS = 60
MAX_DELIVERY_ATTEMPTS = 8
MAX_DELIVERY_BATCH = 20
MAX_RESPONSE_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class EventDeliveryClaim:
    delivery_id: UUID
    envelope_bytes: bytes
    attempt: int


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    claimed: int = 0
    delivered: int = 0
    deferred: int = 0
    exhausted: int = 0


def enqueue_event_delivery(event: ExecutionEvent) -> ExecutionEventDelivery | None:
    """Create one wire outbox row for a contract-visible FND-007 event."""

    execution = event.attempt.execution
    envelope = build_event_envelope(execution, event.attempt, event)
    if envelope is None:
        return None
    encoded = event_envelope_bytes(envelope)
    existing = ExecutionEventDelivery.objects.filter(event_id=event.id).first()
    if existing is not None:
        if existing.fingerprint != envelope.fingerprint:
            raise RuntimeConflictError(
                "event delivery identity conflicts with stored state"
            )
        return existing
    return ExecutionEventDelivery.objects.create(
        event=event,
        envelope_bytes=encoded,
        byte_length=len(encoded),
        fingerprint=envelope.fingerprint,
        state=EventDeliveryState.PENDING,
        delivery_attempts=0,
        next_attempt_at=event.created_at or timezone.now(),
    )


def claim_event_deliveries(
    limit: int = MAX_DELIVERY_BATCH,
    *,
    now: datetime | None = None,
) -> tuple[EventDeliveryClaim, ...]:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_DELIVERY_BATCH:
        raise RuntimeValidationError("delivery limit must be between 1 and 20")
    observed_at = now or timezone.now()

    @transaction.atomic
    def claim_once() -> tuple[EventDeliveryClaim, ...]:
        rows = list(
            ExecutionEventDelivery.objects.select_for_update()
            .filter(
                state__in=(EventDeliveryState.PENDING, EventDeliveryState.DELIVERING),
                next_attempt_at__lte=observed_at,
                delivery_attempts__lt=MAX_DELIVERY_ATTEMPTS,
            )
            .filter(models_q_delivering_expired(observed_at))
            .order_by("next_attempt_at", "created_at", "id")[:limit]
        )
        claims: list[EventDeliveryClaim] = []
        for row in rows:
            row.state = EventDeliveryState.DELIVERING
            row.delivery_attempts += 1
            row.lease_expires_at = observed_at + timedelta(
                seconds=DELIVERY_LEASE_SECONDS
            )
            row.save(
                update_fields=[
                    "state",
                    "delivery_attempts",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            claims.append(
                EventDeliveryClaim(
                    row.id, bytes(row.envelope_bytes), row.delivery_attempts
                )
            )
        return tuple(claims)

    return claim_once()


def mark_event_delivery(
    delivery_id: UUID,
    *,
    success: bool,
    safe_error_code: str = "",
    terminal: bool = False,
    now: datetime | None = None,
) -> ExecutionEventDelivery:
    observed_at = now or timezone.now()

    @transaction.atomic
    def mark_once() -> ExecutionEventDelivery:
        row = ExecutionEventDelivery.objects.select_for_update().get(pk=delivery_id)
        if success:
            row.state = EventDeliveryState.DELIVERED
            row.delivered_at = observed_at
            row.lease_expires_at = None
            row.safe_error_code = ""
        else:
            if safe_error_code and not _safe_error_code(safe_error_code):
                raise RuntimeValidationError("delivery error code is invalid")
            row.safe_error_code = safe_error_code or "delivery_failed"
            row.lease_expires_at = None
            if terminal or row.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
                row.state = EventDeliveryState.EXHAUSTED
            else:
                row.state = EventDeliveryState.PENDING
                delay = min(300, 2 ** max(row.delivery_attempts - 1, 0))
                row.next_attempt_at = observed_at + timedelta(seconds=delay)
        row.save(
            update_fields=[
                "state",
                "delivered_at",
                "lease_expires_at",
                "safe_error_code",
                "next_attempt_at",
                "updated_at",
            ]
        )
        return row

    return mark_once()


def publish_pending_event_deliveries(limit: int = MAX_DELIVERY_BATCH) -> DeliveryReport:
    """Deliver a bounded batch; the outbox remains the retry authority."""

    claims = claim_event_deliveries(limit)
    delivered = deferred = exhausted = 0
    for claim in claims:
        status, code = _post_to_cloud(claim.envelope_bytes)
        if status == 202:
            mark_event_delivery(claim.delivery_id, success=True)
            delivered += 1
        elif status in (401, 403, 404, 422):
            mark_event_delivery(
                claim.delivery_id,
                success=False,
                safe_error_code=code or "delivery_rejected",
                terminal=True,
            )
            row = ExecutionEventDelivery.objects.get(pk=claim.delivery_id)
            exhausted += int(row.state == EventDeliveryState.EXHAUSTED)
            deferred += int(row.state == EventDeliveryState.PENDING)
        else:
            mark_event_delivery(
                claim.delivery_id,
                success=False,
                safe_error_code=code or "delivery_unavailable",
            )
            row = ExecutionEventDelivery.objects.get(pk=claim.delivery_id)
            exhausted += int(row.state == EventDeliveryState.EXHAUSTED)
            deferred += int(row.state == EventDeliveryState.PENDING)
    return DeliveryReport(len(claims), delivered, deferred, exhausted)


def _post_to_cloud(envelope_bytes: bytes) -> tuple[int, str]:
    if not getattr(settings, "ALLIES_CLOUD_EVENT_DELIVERY_ENABLED", False):
        return 503, "delivery_disabled"
    base_url = getattr(settings, "ALLIES_CLOUD_URL", None)
    token = getattr(settings, "ALLIES_CLOUD_EVENT_SERVICE_TOKEN", None)
    if not base_url or not token:
        return 503, "delivery_not_configured"
    request = Request(
        f"{base_url.rstrip('/')}/api/v1/internal/foundry/events",
        data=envelope_bytes,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            _discard_response(response)
            return int(response.status), ""
    except HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES)
        finally:
            exc.close()
        return int(exc.code), _safe_response_code(body)
    except (TimeoutError, URLError, OSError):
        return 503, "delivery_unavailable"


def _discard_response(response: Any) -> None:
    response.read(MAX_RESPONSE_BYTES)


def _safe_response_code(body: bytes) -> str:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return ""
    code = value.get("code") if isinstance(value, dict) else None
    return code if isinstance(code, str) and _safe_error_code(code) else ""


def _safe_error_code(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value[0].islower()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def models_q_delivering_expired(observed_at: datetime):
    """Return the due-state predicate without importing Django's Q at module load."""

    from django.db.models import Q

    return Q(state=EventDeliveryState.PENDING) | Q(
        state=EventDeliveryState.DELIVERING,
        lease_expires_at__lte=observed_at,
    )


__all__ = [
    "DELIVERY_LEASE_SECONDS",
    "MAX_DELIVERY_ATTEMPTS",
    "DeliveryReport",
    "EventDeliveryClaim",
    "claim_event_deliveries",
    "enqueue_event_delivery",
    "mark_event_delivery",
    "publish_pending_event_deliveries",
]
