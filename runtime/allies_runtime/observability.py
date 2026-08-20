"""Runtime-local WideEventV1 formatter and fail-open stdout dispatcher."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from .config import WideEventSettings

MAX_STRING_LENGTH = 256
MAX_EVENT_DEPTH = 3
ALLOWED_EVENT_NAMES = frozenset(
    {
        "http.request",
        "runtime.operation.started",
        "runtime.operation.succeeded",
        "runtime.operation.failed",
        "runtime.operation.retried",
        "provider.operation.started",
        "provider.operation.succeeded",
        "provider.operation.failed",
        "provider.operation.retried",
        "worker.started",
        "worker.idle",
        "worker.failed",
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_EMAIL_RE = re.compile(r"\b[^\s@/]+@[^\s@/]+\.[^\s@/]+\b")
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|token[=:]\s*|password[=:]\s*|secret[=:]\s*|api[_-]?key[=:]\s*)[^\s,;]+"
)
_FIELDS = frozenset(
    {
        "schema_version",
        "event",
        "occurred_at",
        "service",
        "process",
        "environment",
        "revision",
        "request_id",
        "correlation_id",
        "method",
        "route",
        "status_code",
        "duration_ms",
        "outcome",
        "error_type",
        "error_code",
        "reason_code",
        "message",
        "operation",
        "provider",
        "workspace_id",
        "execution_id",
        "attempt_id",
        "lease_id",
        "provider_resource_id",
        "resource_id",
        "profile_id",
        "session_id",
        "run_id",
        "retry_count",
        "sampled",
    }
)


class EventSink(Protocol):
    def offer(self, envelope: bytes) -> Any: ...


def _text(value: str, limit: int = MAX_STRING_LENGTH) -> str:
    value = _SECRET_RE.sub("[REDACTED]", value)
    value = _EMAIL_RE.sub("[REDACTED]", value)
    value = re.sub(r"(?i)https?://[^\s]+", "[REDACTED_URL]", value)
    return value.replace("\r", " ").replace("\n", " ")[:limit]


def _identifier(value: object) -> str | None:
    return (
        value.strip()[:128]
        if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value.strip())
        else None
    )


def _identifier_digest(value: object) -> str | None:
    safe = _identifier(value)
    key = os.getenv("ALLIES_OBSERVABILITY_DIGEST_KEY") or os.getenv("DJANGO_SECRET_KEY")
    if safe is None or not key:
        return None
    return "id_" + hmac.new(key.encode("utf-8"), safe.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _value(name: str, value: object) -> object | None:
    if name in {
        "workspace_id",
        "execution_id",
        "attempt_id",
        "lease_id",
        "provider_resource_id",
        "resource_id",
        "profile_id",
        "session_id",
        "run_id",
    }:
        return _identifier_digest(value)
    if name in {"request_id", "correlation_id"}:
        return _identifier(value)
    if name == "error_type":
        return value[:96] if isinstance(value, str) and _CLASS_RE.fullmatch(value) else None
    if name in {"error_code", "reason_code", "operation", "provider"}:
        return value[:64] if isinstance(value, str) and _CODE_RE.fullmatch(value) else None
    if name == "route":
        if not isinstance(value, str):
            return None
        return value.split("?", 1)[0][:MAX_STRING_LENGTH]
    if name == "status_code":
        return value if type(value) is int and 100 <= value <= 599 else None
    if name == "duration_ms":
        return round(max(0.0, min(float(value), 86_400_000.0)), 3) if isinstance(value, (int, float)) else None
    if name == "retry_count":
        return value if type(value) is int and 0 <= value <= 1_000_000 else None
    if name == "sampled":
        return value if isinstance(value, bool) else None
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and abs(value) <= 1_000_000_000:
        return value
    return None


def _settings() -> WideEventSettings:
    try:
        return WideEventSettings.from_env(os.environ)
    except Exception:  # noqa: BLE001 - a bad env must not break failure paths
        return WideEventSettings()


def build_event(kind: str, **fields: object) -> dict[str, object]:
    if kind not in ALLOWED_EVENT_NAMES:
        raise ValueError("event name is not allowlisted")
    event: dict[str, object] = {
        "schema_version": 1,
        "event": kind,
        "occurred_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "service": "foundry",
        "process": "runtime",
        "environment": os.getenv("ALLIES_ENVIRONMENT", "development"),
        "revision": os.getenv("ALLIES_REVISION", "unknown"),
        "sampled": True,
    }
    for name, value in fields.items():
        if name in _FIELDS:
            safe = _value(name, value)
            if safe is not None:
                event[name] = safe
    return event


def serialize_event(event: Mapping[str, object], *, max_bytes: int | None = None) -> bytes:
    safe = {
        name: safe_value
        for name, value in event.items()
        if name in _FIELDS
        for safe_value in [_value(name, value)]
        if safe_value is not None
    }
    safe.setdefault("schema_version", 1)
    safe.setdefault("occurred_at", datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    limit = max_bytes or _settings().max_bytes
    raw = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) <= limit:
        return raw
    for name in ("message", "revision", "provider_resource_id", "resource_id", "run_id", "session_id", "attempt_id", "lease_id"):
        safe.pop(name, None)
        raw = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) <= limit:
            return raw
    raise ValueError("wide event exceeds configured byte bound")


class EventCounters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {"events_emitted": 0, "events_sampled_out": 0, "events_dropped": 0}

    def increment(self, name: str) -> None:
        with self._lock:
            if name in self._counts:
                self._counts[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


_COUNTERS = EventCounters()
_SINK: EventSink | None = None


def configure_runtime_observability(*, sink: EventSink | None = None) -> None:
    global _SINK
    _SINK = sink


def event_counters() -> dict[str, int]:
    return _COUNTERS.snapshot()


def _always_emit(event: Mapping[str, object], config: WideEventSettings) -> bool:
    outcome = event.get("outcome")
    duration = event.get("duration_ms")
    return (
        outcome in {"error", "failed", "retry", "cancelled"}
        or str(event.get("event", "")).endswith((".failed", ".retried"))
        or (isinstance(duration, (int, float)) and duration >= config.slow_ms)
    )


def emit_runtime_event(event: Mapping[str, object]) -> None:
    """Emit a runtime event without allowing logging failures to escape."""

    try:
        config = _settings()
        if not config.enabled:
            _COUNTERS.increment("events_dropped")
            return
        if not (_always_emit(event, config) or random.random() < config.success_sample_rate):
            _COUNTERS.increment("events_sampled_out")
            return
        mutable = dict(event)
        mutable["sampled"] = True
        envelope = serialize_event(mutable, max_bytes=config.max_bytes)
        stream = getattr(sys.stdout, "buffer", sys.stdout)
        try:
            stream.write(envelope + b"\n")
        except TypeError:
            stream.write((envelope + b"\n").decode("utf-8"))
        stream.flush()
        _COUNTERS.increment("events_emitted")
        if config.sink_enabled and _SINK is not None:
            try:
                result = _SINK.offer(bytes(envelope))
                if getattr(result, "accepted", True) is False:
                    _COUNTERS.increment("events_dropped")
            except Exception:  # noqa: BLE001 - optional sink is fail-open
                _COUNTERS.increment("events_dropped")
    except Exception:  # noqa: BLE001 - never change runtime behavior
        _COUNTERS.increment("events_dropped")


__all__ = [
    "ALLOWED_EVENT_NAMES",
    "EventCounters",
    "EventSink",
    "build_event",
    "configure_runtime_observability",
    "emit_runtime_event",
    "event_counters",
    "serialize_event",
]
