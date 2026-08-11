from __future__ import annotations

import base64
from uuid import uuid4

import pytest

from runtime.models import RuntimeCredential, Workspace, WorkspaceProvisioningPhase
from runtime.services.continuity_proof import ProofCredentialBootstrap


class FakeSecretStore:
    def __init__(self):
        self.staged = []
        self.unset = []

    def stage(self, app_ref, secret_name, encoded_value):
        self.staged.append((app_ref, secret_name, encoded_value))

    def remove(self, app_ref, secret_name):
        self.unset.append((app_ref, secret_name))


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
