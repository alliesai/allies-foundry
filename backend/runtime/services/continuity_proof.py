from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from runtime.exceptions import RuntimeConflictError, RuntimeFencedError
from runtime.models import (
    Attempt,
    ConversationBinding,
    Execution,
    ExecutionStatus,
    RuntimeProfile,
    Workspace,
)
from runtime.providers import ProviderNotFoundError
from runtime.services.executions import create_execution
from runtime.services.profiles import ProfileSeed, ensure_runtime_profile
from runtime.services.runtime_auth import (
    RuntimeCredentialIssue,
    authenticate_runtime_token_for_claim,
    issue_runtime_credential_for_generation,
    revoke_runtime_credential,
)
from runtime.services.workspaces import (
    ReplacementProofPrecondition,
    WorkspaceLifecycle,
    WorkspaceSpec,
)

_CREDENTIAL_REF = "file:///run/secrets/foundry-runtime-token"
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class ProofSecretStore(Protocol):
    def stage(self, app_ref: str, secret_name: str, encoded_value: str) -> None: ...

    def remove(self, app_ref: str, secret_name: str) -> None: ...


class ProofProgressDriver(Protocol):
    def __call__(self, stage: str, state: ProofRunState) -> None: ...


class FlyCliSecretStore:
    """Stage proof secrets through stdin so values never enter argv."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or _default_fly_executable()

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


@dataclass(frozen=True, slots=True)
class ProofProfile:
    alias: str
    profile_id: UUID
    ally_ref: str
    seed: ProfileSeed
    recognizable_fact: str


@dataclass(frozen=True, slots=True)
class ContinuityProofConfig:
    run_id: str
    workspace_id: UUID
    tenant_ref: str
    foundry_origin: str
    workspace_spec: WorkspaceSpec
    profiles: tuple[ProofProfile, ProofProfile]
    timeout_seconds: float = 120.0
    poll_interval: float = 0.25

    def __post_init__(self) -> None:
        if not _SAFE_SLUG.fullmatch(self.run_id):
            raise ValueError("run_id must be a bounded lowercase slug")
        if len({profile.profile_id for profile in self.profiles}) != 2:
            raise ValueError("proof profiles must be distinct")
        if not 0 < self.timeout_seconds <= 900:
            raise ValueError("timeout_seconds must be between 0 and 900")
        if not 0 < self.poll_interval <= 5:
            raise ValueError("poll_interval must be between 0 and 5")


@dataclass(frozen=True, slots=True)
class ProofCheck:
    name: str
    status: Literal["pass", "fail"]
    detail_code: str

    def __post_init__(self) -> None:
        if not _SAFE_CODE.fullmatch(self.name) or not _SAFE_CODE.fullmatch(
            self.detail_code
        ):
            raise ValueError("proof checks require bounded safe codes")


@dataclass(frozen=True, slots=True)
class ProofRunState:
    workspace_id: UUID
    profile_ids: tuple[UUID, UUID]
    execution_ids: tuple[UUID, ...] = ()
    generation: int = 0


@dataclass(frozen=True, slots=True)
class ContinuityProofResult:
    run_id: str
    status: Literal["pass", "fail", "skipped", "incomplete_cleanup"]
    checks: tuple[ProofCheck, ...]
    workspace: dict[str, str | int]
    resources: dict[str, str]
    executions: tuple[dict[str, Any], ...]
    sessions: tuple[dict[str, str | bool], ...]
    cleanup: Literal["complete", "incomplete"]

    @property
    def exit_code(self) -> int:
        return {
            "pass": 0,
            "fail": 1,
            "skipped": 2,
            "incomplete_cleanup": 3,
        }[self.status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "checks": [asdict(item) for item in self.checks],
            "workspace": dict(self.workspace),
            "resources": dict(self.resources),
            "executions": [dict(item) for item in self.executions],
            "sessions": [dict(item) for item in self.sessions],
            "cleanup": self.cleanup,
        }


def run_machine_replacement_proof(
    config: ContinuityProofConfig,
    *,
    provider: Any,
    credential_bootstrap: ProofCredentialBootstrap,
    progress: ProofProgressDriver | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    old_token_probe: Callable[[str], bool] | None = None,
) -> ContinuityProofResult:
    """Run one owned, deterministic Machine-replacement continuity proof."""

    progress = progress or (lambda _stage, _state: None)
    mutated = False
    handles: list[ProofCredentialHandle] = []
    resources: dict[str, str] = {}
    checks: list[ProofCheck] = []
    executions: list[dict[str, Any]] = []
    sessions: list[dict[str, str | bool]] = []
    old_generation = 0
    new_generation = 0
    failure_code: str | None = None
    cleanup_complete = True
    deadline = clock() + config.timeout_seconds
    lifecycle = WorkspaceLifecycle(provider, sleep=sleep, jitter=False)
    state = ProofRunState(
        workspace_id=config.workspace_id,
        profile_ids=tuple(profile.profile_id for profile in config.profiles),
    )

    try:
        capability = getattr(provider, "assert_proof_capabilities", None)
        if capability is None:
            capability = provider.assert_topology_supported
        capability()
        checks.append(ProofCheck("provider_preflight", "pass", "capabilities_ready"))

        workspace, _ = Workspace.objects.get_or_create(
            id=config.workspace_id,
            defaults={"tenant_ref": config.tenant_ref},
        )
        if (
            workspace.tenant_ref != config.tenant_ref
            or workspace.machine_generation != 0
        ):
            raise RuntimeError("workspace_not_fresh")
        app = provider.ensure_app(config.workspace_spec.app_spec(config.workspace_id))
        mutated = True
        resources["app"] = app.name

        first_handle = credential_bootstrap.prepare(
            workspace.id,
            app.name,
            generation=1,
            operation_id=UUID(bytes=secrets.token_bytes(16)),
        )
        handles.append(first_handle)
        first_spec = _spec_for_handle(config, first_handle)
        first_binding = lifecycle.ensure_workspace(workspace.id, first_spec)
        old_generation = first_binding.machine_generation
        resources.update(
            {
                "volume": first_binding.volume_ref,
                "old_machine": first_binding.machine_ref,
            }
        )
        checks.append(ProofCheck("initial_machine", "pass", "generation_one_ready"))

        for profile in config.profiles:
            ensure_runtime_profile(
                workspace.id,
                profile.profile_id,
                profile.ally_ref,
                profile.seed,
            )
        state = replace(state, generation=old_generation)
        _wait_for(
            "profiles_generation_one",
            state,
            progress,
            lambda: (
                RuntimeProfile.objects.filter(
                    pk__in=state.profile_ids,
                    materialized_generation=old_generation,
                ).count()
                == 2
            ),
            deadline,
            clock,
            sleep,
            config.poll_interval,
        )

        first_turns = tuple(
            create_execution(
                workspace.id,
                profile.profile_id,
                f"{config.run_id}-first-{profile.alias}",
                {
                    "message": (
                        f"Remember this proof fact for {profile.alias}: "
                        f"{profile.recognizable_fact}"
                    ),
                    "cloud_conversation_ref": f"{config.run_id}-{profile.alias}",
                },
            )
            for profile in config.profiles
        )
        state = replace(state, execution_ids=tuple(item.id for item in first_turns))
        _wait_for(
            "first_turns",
            state,
            progress,
            lambda: _executions_succeeded(state.execution_ids),
            deadline,
            clock,
            sleep,
            config.poll_interval,
        )
        before_sessions = _session_map(state.profile_ids)
        if len(before_sessions) != 2:
            raise RuntimeError("first_sessions_missing")
        checks.append(ProofCheck("isolated_first_turns", "pass", "sessions_bound"))

        active = tuple(
            create_execution(
                workspace.id,
                profile.profile_id,
                f"{config.run_id}-active-{profile.alias}",
                {
                    "message": f"Recall the proof fact for {profile.alias}.",
                    "cloud_conversation_ref": f"{config.run_id}-{profile.alias}",
                    "proof_hold_after_first_safe_event": True,
                },
            )
            for profile in config.profiles
        )
        queued = create_execution(
            workspace.id,
            config.profiles[0].profile_id,
            f"{config.run_id}-queued-{config.profiles[0].alias}",
            {
                "message": f"Continue as {config.profiles[0].alias}.",
                "cloud_conversation_ref": f"{config.run_id}-{config.profiles[0].alias}",
            },
        )
        state = replace(
            state,
            execution_ids=tuple(item.id for item in (*active, queued)),
        )
        precondition = _wait_for_active_precondition(
            state,
            queued.id,
            progress,
            deadline,
            clock,
            sleep,
            config.poll_interval,
        )
        checks.append(ProofCheck("active_overlap", "pass", "two_streams_and_queue"))

        second_handle = credential_bootstrap.prepare(
            workspace.id,
            app.name,
            generation=old_generation + 1,
            operation_id=UUID(bytes=secrets.token_bytes(16)),
        )
        handles.append(second_handle)
        second_spec = _spec_for_handle(config, second_handle)
        replacement = lifecycle.replace_machine(
            workspace.id,
            second_spec,
            old_generation,
            precondition,
        )
        new_generation = replacement.machine_generation
        resources["replacement_machine"] = replacement.machine_ref
        checks.append(ProofCheck("machine_replacement", "pass", "volume_preserved"))

        probe = old_token_probe or _default_old_token_probe
        if not probe(first_handle.raw_token):
            raise RuntimeError("old_generation_token_accepted")
        checks.append(ProofCheck("generation_fence", "pass", "old_claim_rejected"))

        state = replace(state, generation=new_generation)
        _wait_for(
            "replacement_profiles",
            state,
            progress,
            lambda: (
                RuntimeProfile.objects.filter(
                    pk__in=state.profile_ids,
                    materialized_generation=new_generation,
                ).count()
                == 2
            ),
            deadline,
            clock,
            sleep,
            config.poll_interval,
        )
        resumed = tuple(
            create_execution(
                workspace.id,
                profile.profile_id,
                f"{config.run_id}-resume-{profile.alias}",
                {
                    "message": f"Recall the proof fact for {profile.alias}.",
                    "cloud_conversation_ref": f"{config.run_id}-{profile.alias}",
                },
            )
            for profile in config.profiles
        )
        state = replace(
            state,
            execution_ids=tuple(item.id for item in (*active, queued, *resumed)),
        )
        _wait_for(
            "replacement_recovery",
            state,
            progress,
            lambda: (
                Execution.objects.filter(
                    pk__in=tuple(item.id for item in active),
                    status=ExecutionStatus.FAILED,
                ).count()
                == 2
                and _executions_succeeded(tuple(item.id for item in (queued, *resumed)))
            ),
            deadline,
            clock,
            sleep,
            config.poll_interval,
        )
        after_sessions = _session_map(state.profile_ids)
        for profile in config.profiles:
            sessions.append(
                {
                    "profile_alias": profile.alias,
                    "resumed": before_sessions.get(profile.profile_id)
                    == after_sessions.get(profile.profile_id),
                    "isolated": after_sessions.get(profile.profile_id)
                    not in {
                        value
                        for key, value in after_sessions.items()
                        if key != profile.profile_id
                    },
                }
            )
        if not all(item["resumed"] and item["isolated"] for item in sessions):
            raise RuntimeError("session_continuity_failed")
        evidence_profiles = (*config.profiles, config.profiles[0], *config.profiles)
        evidence_executions = (*active, queued, *resumed)
        for index, (profile, execution) in enumerate(
            zip(evidence_profiles, evidence_executions, strict=True)
        ):
            attempt_rows = tuple(
                Attempt.objects.filter(execution=execution)
                .order_by("number")
                .values_list("id", "machine_generation")
            )
            generations = {generation for _attempt_id, generation in attempt_rows}
            expected_generations = {old_generation} if index < 2 else {new_generation}
            if generations != expected_generations:
                raise RuntimeError("execution_generation_history_invalid")
            attempts = tuple(str(attempt_id) for attempt_id, _ in attempt_rows)
            executions.append(
                {
                    "execution_id": str(execution.id),
                    "profile_alias": profile.alias,
                    "attempt_ids": attempts,
                    "terminal_state": (
                        ExecutionStatus.FAILED
                        if index < 2
                        else ExecutionStatus.SUCCEEDED
                    ),
                }
            )
        checks.append(ProofCheck("continuity_recovery", "pass", "history_complete"))
    except Exception as exc:  # noqa: BLE001 - result exposes only a bounded code
        failure_code = _safe_failure_code(exc)
        checks.append(ProofCheck("proof_run", "fail", failure_code))
    finally:
        for handle in reversed(handles):
            try:
                credential_bootstrap.cleanup(handle)
            except Exception:  # noqa: BLE001 - cleanup detail is intentionally bounded
                cleanup_complete = False
        if mutated:
            cleanup_complete = (
                _cleanup_provider_resources(provider, resources) and cleanup_complete
            )
        if cleanup_complete:
            Workspace.objects.filter(pk=config.workspace_id).delete()

    if not cleanup_complete:
        status: Literal["pass", "fail", "skipped", "incomplete_cleanup"] = (
            "incomplete_cleanup"
        )
    elif failure_code is not None:
        status = "fail" if mutated else "skipped"
    else:
        status = "pass"
    return ContinuityProofResult(
        run_id=config.run_id,
        status=status,
        checks=tuple(checks),
        workspace={
            "workspace_id": str(config.workspace_id),
            "old_generation": old_generation,
            "new_generation": new_generation,
        },
        resources=resources,
        executions=tuple(executions),
        sessions=tuple(sessions),
        cleanup="complete" if cleanup_complete else "incomplete",
    )


def _spec_for_handle(
    config: ContinuityProofConfig,
    handle: ProofCredentialHandle,
) -> WorkspaceSpec:
    return replace(
        config.workspace_spec,
        foundry_origin=config.foundry_origin,
        foundry_runtime_credential_ref=handle.credential_ref,
        foundry_runtime_credential_secret_name=handle.secret_name,
    )


def _wait_for(
    stage: str,
    state: ProofRunState,
    progress: ProofProgressDriver,
    predicate: Callable[[], bool],
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float,
) -> None:
    while True:
        progress(stage, state)
        if predicate():
            return
        if clock() >= deadline:
            raise RuntimeError(f"{stage}_timeout")
        sleep(interval)


def _wait_for_active_precondition(
    state: ProofRunState,
    queued_execution_id: UUID,
    progress: ProofProgressDriver,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float,
) -> ReplacementProofPrecondition:
    result: ReplacementProofPrecondition | None = None

    def ready() -> bool:
        nonlocal result
        attempts = tuple(
            Attempt.objects.filter(
                execution_id__in=state.execution_ids[:2],
                status="running",
            )
            .order_by("execution_id")
            .values_list("id", flat=True)
        )
        if len(attempts) != 2:
            return False
        candidate = ReplacementProofPrecondition(attempts, queued_execution_id)
        workspace = Workspace.objects.get(pk=state.workspace_id)
        try:
            candidate.assert_satisfied(workspace)
        except RuntimeConflictError:
            return False
        result = candidate
        return True

    _wait_for(
        "active_streams",
        state,
        progress,
        ready,
        deadline,
        clock,
        sleep,
        interval,
    )
    if result is None:  # pragma: no cover - guarded by _wait_for
        raise RuntimeError("active_streams_missing")
    return result


def _executions_succeeded(execution_ids: tuple[UUID, ...]) -> bool:
    return Execution.objects.filter(
        pk__in=execution_ids,
        status=ExecutionStatus.SUCCEEDED,
    ).count() == len(execution_ids)


def _session_map(profile_ids: tuple[UUID, UUID]) -> dict[UUID, str]:
    return dict(
        ConversationBinding.objects.filter(profile_id__in=profile_ids).values_list(
            "profile_id", "hermes_session_id"
        )
    )


def _default_old_token_probe(raw_token: str) -> bool:
    try:
        authenticate_runtime_token_for_claim(raw_token)
    except RuntimeFencedError:
        return True
    return False


def _cleanup_provider_resources(provider: Any, resources: dict[str, str]) -> bool:
    complete = True
    app = resources.get("app")
    machine = resources.get("replacement_machine") or resources.get("old_machine")
    if app and machine:
        try:
            provider.stop_machine(app, machine)
        except ProviderNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - cleanup must attempt remaining resources
            complete = False
        try:
            provider.destroy_machine(app, machine)
        except ProviderNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - cleanup must attempt remaining resources
            complete = False
    if app and resources.get("volume"):
        try:
            provider.delete_volume(app, resources["volume"])
        except ProviderNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - cleanup must attempt remaining resources
            complete = False
    if app:
        try:
            provider.delete_app(app)
        except ProviderNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - cleanup must report any provider failure
            complete = False
    return complete


def _safe_failure_code(exc: Exception) -> str:
    value = str(exc)
    return value if _SAFE_CODE.fullmatch(value) else "proof_step_failed"


def _default_fly_executable() -> str:
    configured = os.environ.get("FLYCTL_PATH")
    if configured:
        return configured
    discovered = shutil.which("fly") or shutil.which("flyctl")
    if discovered:
        return discovered
    for candidate in (
        Path.home() / ".fly" / "bin" / "fly.exe",
        Path.home() / ".fly" / "bin" / "flyctl.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return "fly"


__all__ = [
    "ContinuityProofConfig",
    "ContinuityProofResult",
    "FlyCliSecretStore",
    "ProofCheck",
    "ProofCredentialBootstrap",
    "ProofCredentialHandle",
    "ProofProfile",
    "ProofRunState",
    "ProofSecretStore",
    "run_machine_replacement_proof",
]
