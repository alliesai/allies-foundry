import json

from observability.events import build_event, identifier_fingerprint, serialize_event


def test_event_is_allowlisted_and_redacts_sensitive_text(monkeypatch):
    monkeypatch.setenv("ALLIES_OBSERVABILITY_DIGEST_KEY", "test-digest-key")

    event = build_event(
        "http.request",
        request_id="req_123",
        workspace_id="workspace-123",
        route="/api/v1/workspaces/123?token=secret",
        message="password=secret alice@example.com https://private.example/x",
    )

    assert event["request_id"] == "req_123"
    assert event["workspace_id"].startswith("id_")
    assert "alice@example.com" not in json.dumps(event)
    assert "private.example" not in json.dumps(event)
    assert "token=secret" not in json.dumps(event)


def test_tenant_identifier_is_omitted_without_digest_key(monkeypatch):
    monkeypatch.delenv("ALLIES_OBSERVABILITY_DIGEST_KEY", raising=False)
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)

    assert identifier_fingerprint("workspace-123") is None


def test_serialized_event_respects_byte_bound():
    event = build_event("runtime.operation.failed", operation="profile_turn", error_type="RuntimeError")

    encoded = serialize_event(event, max_bytes=512)

    assert len(encoded) <= 512
    assert json.loads(encoded)["schema_version"] == 1


def test_event_strings_match_shared_contract_limit(monkeypatch):
    monkeypatch.setenv("ALLIES_OBSERVABILITY_DIGEST_KEY", "test-digest")
    event = build_event("runtime.operation.failed", message="x" * 512)

    assert len(event["message"]) <= 256
