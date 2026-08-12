import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import config.settings as settings
print(settings.DEBUG)
print(settings.DATABASES["default"]["ENGINE"])
print(settings.STATIC_ROOT)
"""


def run_settings_probe(**overrides):
    environment = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "DJANGO_ALLOWED_HOSTS",
        "DJANGO_DEBUG",
        "DJANGO_SECRET_KEY",
        "RAILWAY_PUBLIC_DOMAIN",
    ):
        environment.pop(name, None)
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_development_mode_keeps_sqlite_fallback():
    result = run_settings_probe(DJANGO_DEBUG="true")

    assert result.returncode == 0, result.stderr
    assert "True" in result.stdout
    assert "django.db.backends.sqlite3" in result.stdout


def test_production_mode_requires_secret_key():
    result = run_settings_probe(DJANGO_DEBUG="false")

    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY is required" in result.stderr


def test_production_mode_requires_database_url():
    result = run_settings_probe(
        DJANGO_DEBUG="false",
        DJANGO_SECRET_KEY="synthetic-test-secret",
        DJANGO_ALLOWED_HOSTS="localhost",
    )

    assert result.returncode != 0
    assert "DATABASE_URL is required" in result.stderr


def test_production_mode_requires_allowed_host():
    result = run_settings_probe(
        DJANGO_DEBUG="false",
        DJANGO_SECRET_KEY="synthetic-test-secret",
        DATABASE_URL="sqlite:///test-production.sqlite3",
    )

    assert result.returncode != 0
    assert "DJANGO_ALLOWED_HOSTS or RAILWAY_PUBLIC_DOMAIN is required" in result.stderr


@pytest.mark.parametrize("database_url", ["sqlite:///test-production.sqlite3"])
def test_production_mode_accepts_explicit_database(database_url):
    result = run_settings_probe(
        DJANGO_DEBUG="false",
        DJANGO_SECRET_KEY="synthetic-test-secret",
        DJANGO_ALLOWED_HOSTS="localhost",
        DATABASE_URL=database_url,
    )

    assert result.returncode == 0, result.stderr
    assert "False" in result.stdout
    assert "django.db.backends.sqlite3" in result.stdout
