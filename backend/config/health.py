import asyncio
from threading import Lock
from time import monotonic

from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse
from psycopg import AsyncConnection
from psycopg import Error as PsycopgError

HEALTHCHECK_TIMEOUT_MS = 5_000
HEALTHCHECK_TIMEOUT_SECONDS = HEALTHCHECK_TIMEOUT_MS / 1_000
HEALTHCHECK_CACHE_SECONDS = 1.0

_health_cache_lock = Lock()
_health_cache_expires_at = 0.0
_health_cache_result: tuple[dict[str, str], int] | None = None
_health_probe_in_flight = False


async def _async_postgres_probe(params: dict[str, object]) -> None:
    database = await AsyncConnection.connect(**params)
    try:
        async with database.cursor() as cursor:
            await cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                [str(HEALTHCHECK_TIMEOUT_MS)],
            )
            await cursor.execute("SELECT 1")
    finally:
        await database.close()


def _run_postgres_probe(db_connection=connection) -> None:
    params = dict(db_connection.get_connection_params())
    # Django's PostgreSQL backend supplies its synchronous cursor class. The
    # short-lived async probe uses psycopg's native async cursor instead.
    params.pop("cursor_factory", None)
    probe = asyncio.wait_for(
        _async_postgres_probe(params),
        timeout=HEALTHCHECK_TIMEOUT_SECONDS,
    )
    # Psycopg's async connection requires a selector loop on Windows; using it
    # everywhere also keeps this bounded probe's event-loop behavior uniform.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(probe)


def _run_sqlite_probe() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def healthz(request):
    global _health_cache_expires_at, _health_cache_result, _health_probe_in_flight

    with _health_cache_lock:
        if _health_cache_result is not None and monotonic() < _health_cache_expires_at:
            payload, status = _health_cache_result
            return JsonResponse(payload, status=status)
        if _health_probe_in_flight:
            # Serve the last result while a bounded probe refreshes it. This
            # avoids both false 503s during a slow-but-healthy probe and
            # blocking an unbounded number of gunicorn workers.
            if _health_cache_result is not None:
                payload, status = _health_cache_result
            else:
                payload, status = {"status": "unavailable"}, 503
            return JsonResponse(payload, status=status)
        _health_probe_in_flight = True

    payload, status = {"status": "unavailable"}, 503
    try:
        if connection.vendor == "postgresql":
            _run_postgres_probe()
        else:
            _run_sqlite_probe()
    except (DatabaseError, PsycopgError, OSError, TimeoutError):
        pass
    else:
        payload, status = {"status": "ok"}, 200
    finally:
        with _health_cache_lock:
            _health_cache_result = payload, status
            _health_cache_expires_at = monotonic() + HEALTHCHECK_CACHE_SECONDS
            _health_probe_in_flight = False

    return JsonResponse(payload, status=status)
