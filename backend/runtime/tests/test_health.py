from unittest.mock import MagicMock, call, patch

import pytest
from django.db import DatabaseError


@pytest.fixture(autouse=True)
def clear_health_cache():
    with (
        patch("config.health._health_cache_expires_at", 0.0),
        patch("config.health._health_cache_result", None),
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

    with patch("config.health.connection.cursor", return_value=cursor_context):
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
    cursor = MagicMock()
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor

    with (
        patch("config.health.connection.vendor", "postgresql"),
        patch("config.health.connection.cursor", return_value=cursor_context),
    ):
        response = client.get("/healthz")

    assert response.status_code == 200
    health_calls = [
        call_item
        for call_item in cursor.execute.call_args_list
        if call_item.args
        and call_item.args[0]
        in {"SELECT set_config('statement_timeout', %s, true)", "SELECT 1"}
    ]
    assert health_calls == [
        call(
            "SELECT set_config('statement_timeout', %s, true)",
            ["5000"],
        ),
        call("SELECT 1"),
    ]
