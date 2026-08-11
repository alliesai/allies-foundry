from __future__ import annotations

import base64
import re
from datetime import timedelta
from types import SimpleNamespace
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
from runtime.providers import (
    ProviderInvalidConfigurationError,
    ProviderUnsupportedTopologyError,
)
from runtime.services.continuity_proof import (
    ContinuityProofConfig,
    FlyCliSecretStore,
    ProofCredentialBootstrap,
    ProofCredentialHandle,
    ProofDependencyCredentialBootstrap,
    ProofDependencyCredentialHandle,
    ProofProfile,
    _assert_profile_fact_continuity,
    _cleanup_provider_resources,
    _execution_text,
    _record_current_machine,
    _safe_failure_code,
    _spec_for_handle,
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


def test_safe_failure_code_preserves_typed_provider_code_without_message():
    error = ProviderInvalidConfigurationError("unsafe provider response detail")

    assert _safe_failure_code(error) == "invalid_configuration"


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


def test_proof_bootstrap_stages_runtime_dependencies_and_cleans_each_secret():
    store = FakeSecretStore()
    bootstrap = ProofDependencyCredentialBootstrap(
        store,
        provider_api_key="provider-key-must-not-escape",
        hermes_key_factory=lambda: "generated-hermes-api-key-strong-enough",
    )

    handle = bootstrap.prepare("allies-proof-app")
    replay = bootstrap.prepare("allies-proof-app")

    assert isinstance(handle, ProofDependencyCredentialHandle)
    assert replay is handle
    decoded = {
        name: base64.b64decode(value).decode("utf-8")
        for _app, name, value in store.staged
    }
    assert decoded[handle.hermes_key_secret_name] == (
        "generated-hermes-api-key-strong-enough"
    )
    assert decoded[handle.provider_key_secret_name] == "provider-key-must-not-escape"
    assert "provider-key-must-not-escape" not in repr(bootstrap)
    assert "generated-hermes-api-key-strong-enough" not in repr(handle)

    bootstrap.cleanup(handle)

    assert set(store.unset) == {
        ("allies-proof-app", handle.hermes_key_secret_name),
        ("allies-proof-app", handle.provider_key_secret_name),
    }


def test_proof_spec_mounts_each_dependency_only_in_its_consumer():
    config = proof_config()
    generation = ProofCredentialHandle(
        workspace_id=config.workspace_id,
        app_ref="allies-proof-app",
        generation=1,
        operation_id=uuid4(),
        credential_id=uuid4(),
        secret_name="ALLIES_FND008_FOUNDRY_1",
        credential_ref="file:///run/secrets/foundry-runtime-token",
        raw_token="foundry-token-must-not-escape",
    )
    dependencies = ProofDependencyCredentialHandle(
        app_ref="allies-proof-app",
        hermes_key_secret_name="ALLIES_FND008_HERMES_KEY",
        provider_key_secret_name="ALLIES_FND008_OPENAI_KEY",
    )

    spec = _spec_for_handle(config, generation, dependencies)
    hermes, runtime = spec.containers or ()

    assert hermes.command[:2] == ("sh", "-c")
    assert "exec hermes gateway run --no-supervise" in hermes.command[2]
    assert {item.secret_name for item in hermes.secret_files} == {
        dependencies.hermes_key_secret_name
    }
    assert {item.secret_name for item in runtime.secret_files} == {
        dependencies.hermes_key_secret_name,
        dependencies.provider_key_secret_name,
    }
    assert spec.runtime_credential_ref == dependencies.hermes_credential_ref
    assert "must-not-escape" not in repr(spec)


def test_fly_secret_store_passes_secret_value_only_through_stdin(monkeypatch):
    from runtime.services import continuity_proof

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(continuity_proof.subprocess, "run", run)
    store = FlyCliSecretStore("fly")

    store.stage("allies-app", "FND008_SECRET", "base64-private-value")

    args, kwargs = calls[0]
    assert "base64-private-value" not in repr(args)
    assert kwargs["input"] == "base64-private-value"
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 15

    store.remove("allies-app", "FND008_SECRET")

    remove_args, remove_kwargs = calls[1]
    assert "--stage" in remove_args
    assert remove_kwargs["input"] is None


def test_fly_secret_store_reports_bounded_timeout(monkeypatch):
    from runtime.services import continuity_proof

    def timeout(*_args, **_kwargs):
        raise continuity_proof.subprocess.TimeoutExpired("fly", 15)

    monkeypatch.setattr(continuity_proof.subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        FlyCliSecretStore("fly").remove("allies-app", "FND008_SECRET")


def test_proof_bootstrap_attempts_secret_removal_when_revocation_fails(
    ready_workspace,
):
    store = FakeSecretStore()
    bootstrap = ProofCredentialBootstrap(
        store,
        token_factory=lambda: "random-generation-bearer",
        credential_revoker=lambda _credential_id: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        ),
    )
    handle = bootstrap.prepare(
        ready_workspace.id,
        ready_workspace.fly_app_ref,
        generation=2,
        operation_id=uuid4(),
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        bootstrap.cleanup(handle)

    assert store.unset == [(ready_workspace.fly_app_ref, handle.secret_name)]


def test_proof_bootstrap_attempts_revocation_when_secret_removal_fails(
    ready_workspace,
):
    class FailingSecretStore(FakeSecretStore):
        def remove(self, app_ref, secret_name):
            super().remove(app_ref, secret_name)
            raise RuntimeError("Fly unavailable")

    revoked = []
    bootstrap = ProofCredentialBootstrap(
        FailingSecretStore(),
        token_factory=lambda: "random-generation-bearer",
        credential_revoker=revoked.append,
    )
    handle = bootstrap.prepare(
        ready_workspace.id,
        ready_workspace.fly_app_ref,
        generation=2,
        operation_id=uuid4(),
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        bootstrap.cleanup(handle)

    assert revoked == [handle.credential_id]


@pytest.mark.parametrize("outputs", [("", "fact-1"), ("fact-1", "fact-0")])
def test_profile_fact_continuity_rejects_missing_or_cross_profile_output(
    monkeypatch, outputs
):
    from runtime.services import continuity_proof

    profiles = proof_config().profiles
    executions = (SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()))
    by_id = dict(zip((item.id for item in executions), outputs, strict=True))
    monkeypatch.setattr(
        continuity_proof,
        "_execution_text",
        lambda execution_id: by_id[execution_id],
    )

    with pytest.raises(RuntimeError, match="profile_fact_continuity_failed"):
        _assert_profile_fact_continuity(profiles, executions)


@pytest.mark.django_db
def test_execution_text_uses_only_ordered_successful_attempt_events(ready_workspace):
    profile = RuntimeProfile.objects.create(
        workspace=ready_workspace,
        ally_ref="ally-output",
        hermes_profile_key="ally-output",
    )
    execution = Execution.objects.create(
        workspace=ready_workspace,
        profile=profile,
        idempotency_key="ordered-output",
        input_payload={},
        status=ExecutionStatus.SUCCEEDED,
    )
    failed = Attempt.objects.create(
        execution=execution,
        number=1,
        status=AttemptStatus.FAILED,
        machine_generation=1,
    )
    succeeded = Attempt.objects.create(
        execution=execution,
        number=2,
        status=AttemptStatus.SUCCEEDED,
        machine_generation=2,
    )
    for attempt, sequence, text in (
        (failed, 1, "wrong-fact"),
        (succeeded, 2, "two"),
        (succeeded, 1, "one-"),
    ):
        ExecutionEvent.objects.create(
            attempt=attempt,
            event_id=uuid4(),
            stream_id="ordered-output",
            sequence=sequence,
            event_type="message.delta",
            payload={"text": text},
        )

    assert _execution_text(execution.id) == "one-two"


@pytest.mark.django_db
def test_cleanup_reconciles_and_removes_persisted_replacement_machine(
    ready_workspace,
):
    class RecordingProvider:
        def __init__(self):
            self.stopped = []
            self.destroyed = []

        def stop_machine(self, app_ref, machine_ref):
            self.stopped.append((app_ref, machine_ref))

        def destroy_machine(self, app_ref, machine_ref):
            self.destroyed.append((app_ref, machine_ref))

        def delete_volume(self, _app_ref, _volume_ref):
            return None

        def delete_app(self, _app_ref):
            return None

    Workspace.objects.filter(pk=ready_workspace.id).update(machine_ref="machine-2")
    resources = {
        "app": ready_workspace.fly_app_ref,
        "volume": ready_workspace.volume_ref,
        "old_machine": "machine-1",
    }
    provider = RecordingProvider()

    _record_current_machine(ready_workspace.id, resources)
    assert _cleanup_provider_resources(provider, resources)

    assert resources["current_machine"] == "machine-2"
    assert provider.stopped == [
        (ready_workspace.fly_app_ref, "machine-2"),
        (ready_workspace.fly_app_ref, "machine-1"),
    ]
    assert provider.destroyed == provider.stopped


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
                attempt = Attempt.objects.create(
                    execution=execution,
                    number=number,
                    status=AttemptStatus.SUCCEEDED,
                    machine_generation=2,
                )
                execution.status = ExecutionStatus.SUCCEEDED
                execution.save(update_fields=["status", "updated_at"])
                if execution.id in state.execution_ids[3:]:
                    profile_index = state.profile_ids.index(execution.profile_id)
                    ExecutionEvent.objects.create(
                        attempt=attempt,
                        event_id=uuid4(),
                        stream_id=f"resume-{profile_index}",
                        sequence=1,
                        event_type="message.delta",
                        payload={
                            "text": config.profiles[profile_index].recognizable_fact
                        },
                    )
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
