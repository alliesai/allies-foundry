from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.db import DatabaseError


class FakeAsyncCursor:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def execute(self, *args):
        self.calls.append(args)


class FakeAsyncDatabase:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def clear_health_cache():
    with (
        patch("config.health._health_cache_expires_at", 0.0),
        patch("config.health._health_cache_result", None),
        patch("config.health._health_probe_in_flight", False),
    ):
        yield


@pytest.mark.django_db
def test_healthz_reports_ready_when_database_is_available(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_healthz_reports_unavailable_when_database_check_fails(client):
    with patch("config.health.connection.cursor", side_effect=DatabaseError):
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.django_db
def test_healthz_reuses_a_recent_probe_result(client):
    cursor = MagicMock()
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor

    with (
        patch("config.health.connection.cursor", return_value=cursor_context),
        patch("config.health.monotonic", side_effect=[0.0, 0.0, 0.0]),
    ):
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200

    select_calls = [
        item
        for item in cursor.execute.call_args_list
        if item.args and item.args[0] == "SELECT 1"
    ]
    assert len(select_calls) == 1


@pytest.mark.django_db
def test_healthz_bounds_postgres_probe_with_statement_timeout(client):
    with (
        patch("config.health.connection.vendor", "postgresql"),
        patch("config.health._run_postgres_probe") as probe,
    ):
        response = client.get("/healthz")

    assert response.status_code == 200
    probe.assert_called_once_with()


@pytest.mark.django_db
def test_healthz_returns_unavailable_when_postgres_probe_times_out(client):
    with (
        patch("config.health.connection.vendor", "postgresql"),
        patch("config.health._run_postgres_probe", side_effect=TimeoutError),
    ):
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_run_postgres_probe_uses_bounded_async_connection():
    cursor = FakeAsyncCursor()
    database = FakeAsyncDatabase(cursor)

    with (
        patch(
            "config.health.connection.get_connection_params",
            return_value={"cursor_factory": object(), "dbname": "foundry"},
        ),
        patch(
            "config.health.AsyncConnection.connect",
            new=AsyncMock(return_value=database),
        ) as connect,
    ):
        from config.health import _run_postgres_probe

        _run_postgres_probe()

    connect.assert_awaited_once_with(dbname="foundry")
    assert cursor.calls == [
        (
            "SELECT set_config('statement_timeout', %s, true)",
            ["5000"],
        ),
        ("SELECT 1",),
    ]
    assert database.closed
