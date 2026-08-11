from unittest.mock import patch

import pytest
from django.db import DatabaseError


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
