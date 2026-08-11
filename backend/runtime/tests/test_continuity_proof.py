from __future__ import annotations

import base64
import re
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from runtime.models import (
    Attempt,
    AttemptStatus,
    ConversationBinding,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    Lease,
    LeaseState,
    RuntimeCredential,
    RuntimeProfile,
    Workspace,
    WorkspaceProvisioningPhase,
)
from runtime.providers import ProviderUnsupportedTopologyError
from runtime.services.continuity_proof import (
    ContinuityProofConfig,
    ProofCredentialBootstrap,
    ProofProfile,
    run_machine_replacement_proof,
)
from runtime.services.profiles import ProfileSeed
from runtime.services.workspaces import WorkspaceSpec
from runtime.tests.test_workspace_lifecycle import FakeProvider


class FakeSecretStore:
    def __init__(self):
        self.staged = []
        self.unset = []

    def stage(self, app_ref, secret_name, encoded_value):
        self.staged.append((app_ref, secret_name, encoded_value))

    def remove(self, app_ref, secret_name):
        self.unset.append((app_ref, secret_name))


class ProofProvider(FakeProvider):
    def assert_proof_capabilities(self):
        return None

    def delete_volume(self, app_name, volume_id):
        self.volume = None

    def delete_app(self, app_name):
        self.app = None


def proof_config(*, run_id="fnd008-deterministic", timeout_seconds=5):
    workspace_id = uuid4()
    profiles = tuple(
        ProofProfile(
            alias=f"ally-{index}",
            profile_id=uuid4(),
            ally_ref=f"ally-{index}",
            seed=ProfileSeed(
                personality=f"Proof personality {index}",
                provider="openai",
                model="gpt-test",
                first_chat_instruction="Ask one useful question.",
                credential_refs={"provider_api": f"test://fnd008/provider-{index}"},
            ),
            recognizable_fact=f"fact-{index}",
        )
        for index in range(2)
    )
    return ContinuityProofConfig(
        run_id=run_id,
        workspace_id=workspace_id,
        tenant_ref=run_id,
        foundry_origin="https://foundry.example.com",
        workspace_spec=WorkspaceSpec(
            organization="allies",
            region="ams",
            hermes_image="hermes@sha256:test",
            runtime_image="runtime@sha256:test",
            runtime_credential_ref="test://fnd004/hermes",
        ),
        profiles=profiles,
        timeout_seconds=timeout_seconds,
        poll_interval=0.01,
    )


def assert_no_forbidden_evidence(value, *, key=""):
    assert not re.search(
        r"(?i)secret|credential|authorization|bearer|password|api[_-]?key", key
    )
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            assert_no_forbidden_evidence(child_value, key=str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_forbidden_evidence(child, key=key)
    elif isinstance(value, str):
        assert "Bearer " not in value
        assert "?token=" not in value


@pytest.fixture
def ready_workspace(db):
    return Workspace.objects.create(
        tenant_ref="fnd008-tenant",
        fly_app_ref="allies-proof-app",
        volume_ref="volume-1",
        machine_ref="machine-1",
        machine_generation=1,
        provisioning_phase=WorkspaceProvisioningPhase.IDLE,
    )


def test_proof_bootstrap_retains_one_random_bearer_and_stages_only_base64(
    ready_workspace,
):
    store = FakeSecretStore()
    bootstrap = ProofCredentialBootstrap(
        store,
        token_factory=lambda: "random-generation-bearer",
    )
    operation_id = uuid4()

    handle = bootstrap.prepare(
        ready_workspace.id,
        ready_workspace.fly_app_ref,
        generation=2,
        operation_id=operation_id,
    )
    replay = bootstrap.prepare(
        ready_workspace.id,
        ready_workspace.fly_app_ref,
        generation=2,
        operation_id=operation_id,
    )

    assert replay is handle
    assert handle.raw_token == "random-generation-bearer"
    assert handle.credential_ref == "file:///run/secrets/foundry-runtime-token"
    assert base64.b64decode(store.staged[0][2]).decode() == handle.raw_token
    assert len(store.staged) == 1
    assert "random-generation-bearer" not in repr(handle)
    credential = RuntimeCredential.objects.get(pk=operation_id)
    assert credential.machine_generation == 2
    assert credential.token_digest != handle.raw_token

    bootstrap.cleanup(handle)

    credential.refresh_from_db()
    assert credential.revoked_at is not None
    assert store.unset == [(ready_workspace.fly_app_ref, handle.secret_name)]


@pytest.mark.django_db(transaction=True)
def test_machine_replacement_proof_runs_full_deterministic_path():
    provider = ProofProvider()
    store = FakeSecretStore()
    bootstrap = ProofCredentialBootstrap(
        store,
        token_factory=iter(("generation-one", "generation-two")).__next__,
    )
    config = proof_config()
    workspace_id = config.workspace_id
    completed_stages = set()

    def progress(stage, state):
        if stage in completed_stages:
            return
        if stage == "profiles_generation_one":
            RuntimeProfile.objects.filter(pk__in=state.profile_ids).update(
                materialized_generation=1
            )
        elif stage == "first_turns":
            Execution.objects.filter(pk__in=state.execution_ids).update(
                status=ExecutionStatus.SUCCEEDED
            )
            for index, profile_id in enumerate(state.profile_ids):
                ConversationBinding.objects.update_or_create(
                    profile_id=profile_id,
                    defaults={
                        "cloud_conversation_ref": f"conversation-{index}",
                        "hermes_session_id": f"session-{index}",
                    },
                )
        elif stage == "active_streams":
            for index, execution_id in enumerate(state.execution_ids[:2]):
                execution = Execution.objects.get(pk=execution_id)
                execution.status = ExecutionStatus.RUNNING
                execution.save(update_fields=["status", "updated_at"])
                attempt = Attempt.objects.create(
                    execution=execution,
                    number=1,
                    status=AttemptStatus.RUNNING,
                    machine_generation=1,
                )
                Lease.objects.create(
                    attempt=attempt,
                    profile=execution.profile,
                    token_digest=f"{index + 1}" * 64,
                    expires_at=timezone.now() + timedelta(minutes=5),
                    machine_generation=1,
                    state=LeaseState.ACTIVE,
                )
                for sequence, event_type in (
                    (1, "execution.dispatched"),
                    (2, "message.delta"),
                ):
                    ExecutionEvent.objects.create(
                        attempt=attempt,
                        event_id=uuid4(),
                        stream_id=f"stream-{index}",
                        sequence=sequence,
                        event_type=event_type,
                        payload={"code": "proof_progress"},
                    )
        elif stage == "replacement_profiles":
            RuntimeProfile.objects.filter(pk__in=state.profile_ids).update(
                materialized_generation=2
            )
        elif stage == "replacement_recovery":
            for execution in Execution.objects.filter(pk__in=state.execution_ids[2:]):
                number = execution.attempts.count() + 1
                Attempt.objects.create(
                    execution=execution,
                    number=number,
                    status=AttemptStatus.SUCCEEDED,
                    machine_generation=2,
                )
                execution.status = ExecutionStatus.SUCCEEDED
                execution.save(update_fields=["status", "updated_at"])
        completed_stages.add(stage)

    result = run_machine_replacement_proof(
        config,
        provider=provider,
        credential_bootstrap=bootstrap,
        progress=progress,
        sleep=lambda _seconds: None,
    )

    assert result.status == "pass"
    assert result.exit_code == 0
    assert result.workspace["old_generation"] == 1
    assert result.workspace["new_generation"] == 2
    assert all(item["resumed"] and item["isolated"] for item in result.sessions)
    assert all(len(item["attempt_ids"]) >= 1 for item in result.executions)
    assert [item["terminal_state"] for item in result.executions[:2]] == [
        ExecutionStatus.FAILED,
        ExecutionStatus.FAILED,
    ]
    assert result.cleanup == "complete"
    assert not Workspace.objects.filter(pk=workspace_id).exists()
    assert len(store.staged) == len(store.unset) == 2
    rendered = repr(result.to_dict())
    assert "generation-one" not in rendered
    assert "generation-two" not in rendered
    assert_no_forbidden_evidence(result.to_dict())


@pytest.mark.django_db(transaction=True)
def test_proof_timeout_after_mutation_is_failed_and_cleaned():
    provider = ProofProvider()
    store = FakeSecretStore()
    bootstrap = ProofCredentialBootstrap(store, token_factory=lambda: "bounded-token")
    config = proof_config(run_id="fnd008-timeout", timeout_seconds=0.02)
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    result = run_machine_replacement_proof(
        config,
        provider=provider,
        credential_bootstrap=bootstrap,
        clock=lambda: now[0],
        sleep=advance,
    )

    assert result.status == "fail"
    assert result.exit_code == 1
    assert result.cleanup == "complete"
    assert result.checks[-1].detail_code == "profiles_generation_one_timeout"
    assert not Workspace.objects.filter(pk=config.workspace_id).exists()


@pytest.mark.django_db(transaction=True)
def test_incomplete_cleanup_takes_precedence_over_proof_failure():
    class BrokenCleanupProvider(ProofProvider):
        def delete_app(self, app_name):
            raise RuntimeError("raw provider cleanup detail")

    provider = BrokenCleanupProvider()
    bootstrap = ProofCredentialBootstrap(
        FakeSecretStore(), token_factory=lambda: "bounded-token"
    )
    config = proof_config(run_id="fnd008-cleanup", timeout_seconds=0.02)
    now = [0.0]

    result = run_machine_replacement_proof(
        config,
        provider=provider,
        credential_bootstrap=bootstrap,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert result.status == "incomplete_cleanup"
    assert result.exit_code == 3
    assert result.cleanup == "incomplete"
    assert "raw provider cleanup detail" not in repr(result.to_dict())
    assert Workspace.objects.filter(pk=config.workspace_id).exists()


@pytest.mark.django_db(transaction=True)
def test_failed_preflight_is_skipped_without_provider_mutation():
    class BlockedProvider:
        def assert_proof_capabilities(self):
            raise ProviderUnsupportedTopologyError("unsafe provider detail")

    store = FakeSecretStore()
    config = proof_config(run_id="fnd008-skipped")
    result = run_machine_replacement_proof(
        config,
        provider=BlockedProvider(),
        credential_bootstrap=ProofCredentialBootstrap(store),
    )

    assert result.status == "skipped"
    assert result.exit_code == 2
    assert result.resources == {}
    assert store.staged == []
    assert "unsafe provider detail" not in repr(result.to_dict())
