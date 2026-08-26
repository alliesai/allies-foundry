from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from django.test import Client

from runtime.contracts import (
    ExecutionCommand,
    FoundryEventEnvelope,
    command_fingerprint,
    event_fingerprint,
)
from runtime.exceptions import RuntimeConflictError
from runtime.models import (
    ConversationBinding,
    Execution,
    ExecutionEvent,
    ExecutionEventDelivery,
    RuntimeProfile,
    RuntimeProfileLifecycleState,
    Workspace,
    WorkspaceProvisioningPhase,
)
from runtime.services.claims import claim_next_execution
from runtime.services.events import append_runtime_event
from runtime.services.executions import create_execution_intent
from runtime.services.runtime_auth import (
    authenticate_runtime_token,
    issue_runtime_credential,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "contracts"
    / "foundry-execution-v1.json"
)
PROFILE_NAMESPACE = uuid5(NAMESPACE_URL, "allies-foundry-profile-v1")


@pytest.fixture
def contract():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def configured(settings):
    settings.ALLIES_CLOUD_SERVICE_TOKEN = "test-cloud-service-token"
    return settings.ALLIES_CLOUD_SERVICE_TOKEN


@pytest.fixture
def binding(db, contract):
    command = contract["command"]
    workspace = Workspace.objects.create(
        tenant_ref=command["scope"]["cloud_workspace_id"],
        fly_app_ref="app",
        volume_ref="volume",
        machine_ref="machine",
        machine_generation=1,
        provisioning_phase=WorkspaceProvisioningPhase.IDLE,
    )
    binding_id = UUID(command["cloud"]["cloud_binding_id"])
    profile = RuntimeProfile.objects.create(
        id=uuid5(PROFILE_NAMESPACE, str(binding_id)),
        workspace=workspace,
        ally_ref=command["cloud"]["ally_id"],
        hermes_profile_key="contract_ally",
        lifecycle_state=RuntimeProfileLifecycleState.ACTIVE,
        materialized_generation=workspace.machine_generation,
    )
    ConversationBinding.objects.create(
        profile=profile,
        cloud_conversation_ref=command["cloud"]["conversation_id"],
        hermes_session_id="session-1",
    )
    return workspace, profile


def test_fixture_is_strict_and_fingerprints_are_reproducible(contract):
    command = ExecutionCommand.model_validate(contract["command"])
    event = FoundryEventEnvelope.model_validate(contract["event"])

    assert command_fingerprint(command) == command.fingerprint
    assert event_fingerprint(event) == event.fingerprint
    assert command.payload.text == "normalized user text"


def test_command_exact_replay_and_conflict_do_not_duplicate(binding, contract):
    command = ExecutionCommand.model_validate(contract["command"])

    first = create_execution_intent(command)
    replay = create_execution_intent(command)

    assert first.status == "accepted"
    assert replay.status == "duplicate"
    assert replay.model_copy(update={"status": first.status}) == first
    assert Execution.objects.count() == 1

    changed = ExecutionCommand.model_validate(
        command.model_dump(mode="json")
        | {
            "command_id": str(uuid4()),
            "payload": {"kind": "execution_input", "text": "different"},
        }
    )
    changed = changed.model_copy(update={"fingerprint": command_fingerprint(changed)})
    with pytest.raises(RuntimeConflictError) as error:
        create_execution_intent(changed)
    assert "different execution" in str(error.value)
    assert Execution.objects.count() == 1


def test_command_api_is_authenticated_and_reconciles(binding, contract, configured):
    client = Client()
    payload = json.dumps(contract["command"])
    headers = {"Authorization": f"Bearer {configured}"}

    response = client.post(
        "/api/v1/internal/executions",
        data=payload,
        content_type="application/json",
        headers=headers,
    )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "accepted"
    assert "execution_id" not in response.json()
    assert "profile_id" not in response.json()

    replay = client.post(
        "/api/v1/internal/executions",
        data=payload,
        content_type="application/json",
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "duplicate"

    query = {
        "idempotency_key": contract["command"]["idempotency_key"],
        "fingerprint": contract["command"]["fingerprint"],
    }
    reconciled = client.get(
        "/api/v1/internal/executions/reconcile",
        query,
        headers=headers,
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "accepted"
    assert "execution_id" not in reconciled.json()

    missing = client.get(
        "/api/v1/internal/executions/reconcile",
        {
            "idempotency_key": str(uuid4()),
            "fingerprint": contract["command"]["fingerprint"],
        },
        headers=headers,
    )
    assert missing.status_code == 200
    assert missing.json()["status"] == "not_found"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.update({"schema_version": "v2"}),
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload.update(
            {"fingerprint": "canonical-json-sha256:v1:" + "0" * 64}
        ),
    ],
)
def test_command_api_rejects_unknown_or_conflicting_shapes(
    binding, contract, configured, mutator
):
    payload = dict(contract["command"])
    mutator(payload)
    response = Client().post(
        "/api/v1/internal/executions",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Bearer {configured}"},
    )
    assert response.status_code == 422
    assert Execution.objects.count() == 0


def test_dispatched_runtime_event_is_published_as_accepted(binding, contract):
    workspace, profile = binding
    execution = create_execution_intent(
        ExecutionCommand.model_validate(contract["command"])
    )
    record = Execution.objects.get(command_id=execution.command_id)
    issued = issue_runtime_credential(workspace.id, "runtime-contract-token")
    context = authenticate_runtime_token(issued.raw_token)
    claim = claim_next_execution(context, uuid4(), 1)
    assert claim is not None

    event = append_runtime_event(
        context,
        claim.attempt_id,
        claim.lease_token,
        uuid4(),
        claim.stream_id,
        1,
        "execution.dispatched",
        {"status": "dispatched"},
    )
    delivery = ExecutionEventDelivery.objects.get(event=event)
    wire = json.loads(bytes(delivery.envelope_bytes))

    assert record.profile_id == profile.id
    assert event.event_type == "execution.dispatched"
    assert wire["event_type"] == "execution.accepted"
    assert wire["payload"] == {"status": "accepted"}
    assert (
        wire["conversation_turn_ordinal"]
        == contract["command"]["conversation_turn_ordinal"]
    )
    assert wire["foundry"]["execution_id"] not in {
        str(record.profile_id),
        contract["command"]["command_id"],
    }
    assert ExecutionEvent.objects.filter(attempt=claim.attempt_id).count() == 1


def test_invalid_service_bearer_is_privacy_safe(binding, contract, configured):
    response = Client().post(
        "/api/v1/internal/executions",
        data=json.dumps(contract["command"]),
        content_type="application/json",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json() == {
        "code": "INVALID_CREDENTIAL",
        "message": "request is not authorized",
    }
    assert configured not in response.content.decode()
