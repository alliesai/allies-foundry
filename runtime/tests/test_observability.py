import json

import pytest

from allies_runtime import observability


def test_build_event_redacts_payload_and_hashes_tenant_id(monkeypatch):
    monkeypatch.setenv("ALLIES_OBSERVABILITY_DIGEST_KEY", "runtime-test-key")

    event = observability.build_event(
        "runtime.operation.failed",
        workspace_id="workspace-123",
        route="/api/v1/workspaces/123?token=secret",
        message="password=secret alice@example.com https://private.example",
    )

    assert event["workspace_id"].startswith("id_")
    encoded = json.dumps(event)
    assert "alice@example.com" not in encoded
    assert "private.example" not in encoded
    assert "password=secret" not in encoded


def test_build_event_rejects_unknown_name():
    with pytest.raises(ValueError):
        observability.build_event("not-allowlisted")


def test_field_normalization_drops_invalid_values(monkeypatch):
    monkeypatch.setenv("ALLIES_OBSERVABILITY_DIGEST_KEY", "runtime-test-key")

    event = observability.build_event(
        "http.request",
        request_id="bad id",
        route=123,
        status_code=99,
        duration_ms="slow",
        retry_count=-1,
        sampled="yes",
    )

    assert "request_id" not in event
    assert "route" not in event
    assert "status_code" not in event
    assert "duration_ms" not in event
    assert "retry_count" not in event
    assert event["sampled"] is True


def test_serialize_event_bounds_large_message():
    event = observability.build_event("worker.failed", message="x" * 5000)

    encoded = observability.serialize_event(event, max_bytes=512)

    assert len(encoded) <= 512
    assert json.loads(encoded)["schema_version"] == 1


def test_event_strings_match_shared_contract_limit(monkeypatch):
    monkeypatch.setenv("ALLIES_OBSERVABILITY_DIGEST_KEY", "test-digest")
    event = observability.build_event("runtime.operation.failed", message="x" * 512)

    assert len(event["message"]) <= 256


def test_serialize_event_rejects_impossibly_small_limit():
    event = observability.build_event("worker.failed")

    with pytest.raises(ValueError, match="exceeds configured"):
        observability.serialize_event(event, max_bytes=20)


def test_invalid_runtime_observability_settings_are_fail_open(monkeypatch):
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_MAX_BYTES", "not-an-int")

    assert observability._settings().max_bytes >= 512


def test_emit_runtime_event_writes_json_and_counts(monkeypatch, capsys):
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_SUCCESS_SAMPLE_RATE", "1")
    before = observability.event_counters()["events_emitted"]

    observability.emit_runtime_event(observability.build_event("worker.idle"))

    assert json.loads(capsys.readouterr().out)["event"] == "worker.idle"
    assert observability.event_counters()["events_emitted"] == before + 1


def test_emit_runtime_event_drops_sampled_success(monkeypatch):
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_SUCCESS_SAMPLE_RATE", "0")
    before = observability.event_counters()["events_sampled_out"]

    observability.emit_runtime_event(observability.build_event("worker.idle"))

    assert observability.event_counters()["events_sampled_out"] == before + 1


def test_sink_failure_is_fail_open(monkeypatch):
    class BrokenSink:
        def offer(self, envelope):
            raise RuntimeError("sink unavailable")

    monkeypatch.setenv("ALLIES_WIDE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_SINK_ENABLED", "true")
    observability.configure_runtime_observability(sink=BrokenSink())

    observability.emit_runtime_event(observability.build_event("worker.idle"))

    observability.configure_runtime_observability(sink=None)


def test_disabled_and_rejected_sink_paths_are_counted(monkeypatch):
    before = observability.event_counters()["events_dropped"]
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_ENABLED", "false")
    observability.emit_runtime_event(observability.build_event("worker.idle"))

    class RejectingSink:
        def offer(self, envelope):
            return type("OfferResult", (), {"accepted": False})()

    monkeypatch.setenv("ALLIES_WIDE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_SINK_ENABLED", "true")
    monkeypatch.setenv("ALLIES_WIDE_EVENTS_SUCCESS_SAMPLE_RATE", "1")
    observability.configure_runtime_observability(sink=RejectingSink())
    observability.emit_runtime_event(observability.build_event("worker.idle"))
    observability.configure_runtime_observability(sink=None)

    assert observability.event_counters()["events_dropped"] >= before + 2
