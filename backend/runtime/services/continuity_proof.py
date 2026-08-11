from __future__ import annotations

import base64
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from runtime.services.runtime_auth import (
    RuntimeCredentialIssue,
    issue_runtime_credential_for_generation,
    revoke_runtime_credential,
)

_CREDENTIAL_REF = "file:///run/secrets/foundry-runtime-token"


class ProofSecretStore(Protocol):
    def stage(self, app_ref: str, secret_name: str, encoded_value: str) -> None: ...

    def remove(self, app_ref: str, secret_name: str) -> None: ...


class FlyCliSecretStore:
    """Stage proof secrets through stdin so values never enter argv."""

    def __init__(self, executable: str = "fly") -> None:
        self.executable = executable

    def stage(self, app_ref: str, secret_name: str, encoded_value: str) -> None:
        self._run(
            ("secrets", "set", f"{secret_name}=-", "--stage", "--app", app_ref),
            input_value=encoded_value,
        )

    def remove(self, app_ref: str, secret_name: str) -> None:
        self._run(("secrets", "unset", secret_name, "--app", app_ref))

    def _run(self, args: tuple[str, ...], *, input_value: str | None = None) -> None:
        try:
            completed = subprocess.run(
                (self.executable, *args),
                input=input_value,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise RuntimeError("Fly secret command is unavailable") from exc
        if completed.returncode != 0:
            raise RuntimeError("Fly secret command failed")


@dataclass(frozen=True, slots=True)
class ProofCredentialHandle:
    workspace_id: UUID
    app_ref: str
    generation: int
    operation_id: UUID
    credential_id: UUID
    secret_name: str
    credential_ref: str
    raw_token: str = field(repr=False)


class ProofCredentialBootstrap:
    """Own one in-memory bearer and one staged Fly secret per generation."""

    def __init__(
        self,
        secret_store: ProofSecretStore,
        *,
        token_factory: Callable[[], str] | None = None,
        credential_issuer: Callable[..., RuntimeCredentialIssue] = (
            issue_runtime_credential_for_generation
        ),
        credential_revoker: Callable[[UUID], None] = revoke_runtime_credential,
    ) -> None:
        self.secret_store = secret_store
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(48))
        self.credential_issuer = credential_issuer
        self.credential_revoker = credential_revoker
        self._handles: dict[UUID, ProofCredentialHandle] = {}

    def prepare(
        self,
        workspace_id: UUID,
        app_ref: str,
        *,
        generation: int,
        operation_id: UUID,
    ) -> ProofCredentialHandle:
        existing = self._handles.get(operation_id)
        if existing is not None:
            if (
                existing.workspace_id != workspace_id
                or existing.app_ref != app_ref
                or existing.generation != generation
            ):
                raise ValueError("proof credential operation was reused")
            return existing
        token = self.token_factory()
        secret_name = f"ALLIES_FND008_G{generation}_{operation_id.hex[:16].upper()}"
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
        self.secret_store.stage(app_ref, secret_name, encoded)
        try:
            issued = self.credential_issuer(
                workspace_id,
                generation,
                token,
                operation_id,
            )
        except Exception:
            self.secret_store.remove(app_ref, secret_name)
            raise
        handle = ProofCredentialHandle(
            workspace_id=workspace_id,
            app_ref=app_ref,
            generation=generation,
            operation_id=operation_id,
            credential_id=issued.credential.id,
            secret_name=secret_name,
            credential_ref=_CREDENTIAL_REF,
            raw_token=token,
        )
        self._handles[operation_id] = handle
        return handle

    def cleanup(self, handle: ProofCredentialHandle) -> None:
        self.credential_revoker(handle.credential_id)
        try:
            self.secret_store.remove(handle.app_ref, handle.secret_name)
        finally:
            self._handles.pop(handle.operation_id, None)


__all__ = [
    "FlyCliSecretStore",
    "ProofCredentialBootstrap",
    "ProofCredentialHandle",
    "ProofSecretStore",
]
