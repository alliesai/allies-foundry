from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse

HEALTHCHECK_TIMEOUT_MS = 5_000


def healthz(request):
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [str(HEALTHCHECK_TIMEOUT_MS)],
                )
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)

    return JsonResponse({"status": "ok"})
