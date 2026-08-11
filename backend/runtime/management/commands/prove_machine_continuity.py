from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from runtime.providers import FlyProvider, ProviderError
from runtime.services.continuity_proof import (
    ContinuityProofConfig,
    ContinuityProofResult,
    FlyCliSecretStore,
    ProofCheck,
    ProofCredentialBootstrap,
    ProofProfile,
    run_machine_replacement_proof,
)
from runtime.services.hermes_smoke import PINNED_HERMES_IMAGE
from runtime.services.profiles import ProfileSeed
from runtime.services.workspaces import WorkspaceSpec

_IMAGE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$", re.IGNORECASE)
_REFERENCE = re.compile(r"^[a-z][a-z0-9+.-]{1,31}://[^\s]{1,191}$", re.IGNORECASE)


class Command(BaseCommand):
    help = "Run the guarded FND-008 live Machine-replacement continuity proof."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--runtime-image", required=True)
        parser.add_argument("--foundry-origin", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--hermes-image", default=PINNED_HERMES_IMAGE)
        parser.add_argument("--hermes-credential-ref", required=True)
        parser.add_argument("--profile-a-credential-ref", required=True)
        parser.add_argument("--profile-b-credential-ref", required=True)
        parser.add_argument("--organization", default="allies")
        parser.add_argument("--region", default="ams")
        parser.add_argument("--provider", default="openai")
        parser.add_argument("--model", required=True)
        parser.add_argument("--base-url")
        parser.add_argument("--run-id")
        parser.add_argument("--timeout", type=float, default=120.0)
        parser.add_argument(
            "--confirm-runtime-credential-resolver",
            action="store_true",
            help="Confirm the runtime image can resolve the supplied Hermes/profile references.",
        )

    def handle(self, *args, **options):
        output = self._output_path(options["output"])
        run_id = options["run_id"] or f"fnd008-{uuid4().hex[:12]}"
        if not options["live"]:
            result = self._skipped(run_id, "live_flag_required")
            self._write(output, result)
            raise CommandError(
                "FND-008 requires explicit --live opt-in", returncode=result.exit_code
            )
        try:
            if not options["confirm_runtime_credential_resolver"]:
                raise ValueError("runtime credential resolver is not confirmed")
            config = self._config(run_id, options)
            self._check_foundry_reachable(config.foundry_origin)
            secret_store = FlyCliSecretStore()
            provider = FlyProvider(
                api_token=self._fly_api_token(secret_store.executable)
            )
            result = run_machine_replacement_proof(
                config,
                provider=provider,
                credential_bootstrap=ProofCredentialBootstrap(secret_store),
            )
        except (OSError, ProviderError, TypeError, ValueError) as exc:
            result = self._skipped(run_id, "command_preflight_failed")
            self._write(output, result)
            raise CommandError(
                "FND-008 command preflight failed", returncode=result.exit_code
            ) from exc
        self._write(output, result)
        if result.exit_code:
            raise CommandError(
                f"FND-008 ended with status {result.status}",
                returncode=result.exit_code,
            )
        self.stdout.write(self.style.SUCCESS(f"FND-008 evidence written to {output}"))

    def _config(self, run_id: str, options: dict) -> ContinuityProofConfig:
        runtime_image = self._image(options["runtime_image"], "runtime image")
        hermes_image = self._image(options["hermes_image"], "Hermes image")
        foundry_origin = self._foundry_origin(options["foundry_origin"])
        hermes_ref = self._reference(
            options["hermes_credential_ref"], "Hermes credential reference"
        )
        profile_refs = (
            self._reference(
                options["profile_a_credential_ref"],
                "Ally A credential reference",
            ),
            self._reference(
                options["profile_b_credential_ref"],
                "Ally B credential reference",
            ),
        )
        profiles = tuple(
            ProofProfile(
                alias=f"ally-{alias}",
                profile_id=uuid4(),
                ally_ref=f"ally-{alias}",
                seed=ProfileSeed(
                    personality=f"You are Ally {alias.upper()} in a continuity proof.",
                    provider=options["provider"],
                    model=options["model"],
                    base_url=options["base_url"],
                    first_chat_instruction="Answer briefly and retain the stated fact.",
                    credential_refs={"provider_api": profile_refs[index]},
                ),
                recognizable_fact=(
                    "the copper lighthouse is north"
                    if index == 0
                    else "the blue orchard is east"
                ),
            )
            for index, alias in enumerate(("a", "b"))
        )
        return ContinuityProofConfig(
            run_id=run_id,
            workspace_id=uuid4(),
            tenant_ref=f"proof-{run_id}",
            foundry_origin=foundry_origin,
            workspace_spec=WorkspaceSpec(
                organization=options["organization"],
                region=options["region"],
                hermes_image=hermes_image,
                runtime_image=runtime_image,
                runtime_credential_ref=hermes_ref,
            ),
            profiles=profiles,
            timeout_seconds=options["timeout"],
        )

    @staticmethod
    def _output_path(value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if (
            path.exists()
            or not path.parent.is_dir()
            or not os.access(path.parent, os.W_OK)
        ):
            raise CommandError(
                "--output must be a new file in a writable existing directory",
                returncode=2,
            )
        return path

    @staticmethod
    def _image(value: str, name: str) -> str:
        if not isinstance(value, str) or not _IMAGE.fullmatch(value.strip()):
            raise ValueError(f"{name} must be an immutable digest reference")
        return value.strip()

    @staticmethod
    def _reference(value: str, name: str) -> str:
        if not isinstance(value, str) or not _REFERENCE.fullmatch(value.strip()):
            raise ValueError(f"{name} must be opaque")
        return value.strip()

    @staticmethod
    def _foundry_origin(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Foundry origin must be a plain HTTPS origin")
        return value.rstrip("/")

    @staticmethod
    def _check_foundry_reachable(origin: str) -> None:
        request = Request(
            f"{origin}/api/v1/runtime/profiles/reconciliation",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        except (TimeoutError, URLError) as exc:
            raise ValueError("Foundry origin is unreachable") from exc
        if status not in {401, 403}:
            raise ValueError("Foundry runtime authentication boundary is unavailable")

    @staticmethod
    def _fly_api_token(executable: str) -> str:
        configured = os.environ.get("FLY_API_TOKEN")
        if configured:
            return configured
        try:
            completed = subprocess.run(
                (executable, "auth", "token"),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ValueError("Fly authentication is unavailable") from exc
        token = completed.stdout.strip()
        if completed.returncode != 0 or not token or "\r" in token or "\n" in token:
            raise ValueError("Fly authentication is unavailable")
        return token

    @staticmethod
    def _skipped(run_id: str, detail_code: str) -> ContinuityProofResult:
        return ContinuityProofResult(
            run_id=run_id,
            status="skipped",
            checks=(ProofCheck("command_preflight", "fail", detail_code),),
            workspace={"workspace_id": "", "old_generation": 0, "new_generation": 0},
            resources={},
            executions=(),
            sessions=(),
            cleanup="complete",
        )

    @staticmethod
    def _write(path: Path, result: ContinuityProofResult) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
