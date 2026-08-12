from threading import Lock
from time import monotonic

from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse

HEALTHCHECK_TIMEOUT_MS = 5_000
HEALTHCHECK_CACHE_SECONDS = 1.0

_health_cache_lock = Lock()
_health_cache_expires_at = 0.0
_health_cache_result: tuple[dict[str, str], int] | None = None


def healthz(request):
    global _health_cache_expires_at, _health_cache_result

    # Serialize probes and briefly reuse their result so an unauthenticated
    # health endpoint cannot turn every incoming request into a DB round-trip.
    with _health_cache_lock:
        if _health_cache_result is not None and monotonic() < _health_cache_expires_at:
            payload, status = _health_cache_result
            return JsonResponse(payload, status=status)

        try:
            with transaction.atomic(), connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        [str(HEALTHCHECK_TIMEOUT_MS)],
                    )
                cursor.execute("SELECT 1")
        except DatabaseError:
            payload, status = {"status": "unavailable"}, 503
        else:
            payload, status = {"status": "ok"}, 200

        _health_cache_result = payload, status
        _health_cache_expires_at = monotonic() + HEALTHCHECK_CACHE_SECONDS
        return JsonResponse(payload, status=status)
