"""Freeze and run a locally authorized Phase 3 scored pilot bundle."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .experiment_contract import canonical_json_sha256, publish_immutable_json
from .phase3_pilot import LIVE_AUTHORIZATION_SCHEMA_VERSION
from .phase3_pilot_preparation import (
    APPROVED_PHASE3_ABORT_CONDITIONS,
    APPROVED_PHASE3_MODE_ORDER,
    APPROVED_PHASE3_RETRY_POLICY,
    approved_phase3_pilot_decisions,
    build_phase3_aggregate_pilot_contract,
    build_phase3_execution_authority,
    build_phase3_provider_free_preflight_receipt,
    materialize_phase3_instance_authorities,
    phase3_execution_profile_from_authority,
    validate_phase3_instance_authority_candidates,
    validate_phase3_selection_authority,
)
from .phase3_pilot_runner import (
    Phase3PilotRunner,
    Phase3ProductionExecutor,
    build_phase3_experiment_protocol,
    materialize_phase3_runtime_taskpack,
)
from .phase3_public_verification import (
    public_environment_host_command,
    public_environment_library_view,
    public_environment_path,
    validate_public_verification_environment,
)
from .phase3_swe_evo_evaluator import Phase3SweEvoEvaluator
from .resource_envelope import (
    Phase3PilotResourceOwner,
    approved_phase3_resource_envelope_binding,
    validate_phase3_resource_preflight_receipt,
)


LIVE_BUNDLE_VERSION = "phase3_live_execution_bundle.v1"
LIVE_EPOCH_VERSION = "phase3_live_epoch.v1"


class Phase3LivePilotError(RuntimeError):
    """The live bundle or its local execution dependencies are invalid."""


def build_phase3_live_bundle(
    *,
    bundle_root,
    candidates,
    selection_authority,
    resource_preflight_receipt,
    runtime_release,
    repository_sources,
    common_evaluator_artifact,
    arrow_path,
    arrow_sha256,
    harness_root,
    harness_commit,
    evaluator_python,
    evaluator_environment_lock,
    research_authority_path,
    operator_identity,
    epoch_number=1,
    docker_socket="unix:///run/user/1013/podman/podman.sock",
    public_verification_environments=None,
):
    """Materialize a provider-authorized bundle from frozen provider-free inputs."""

    root = Path(bundle_root).resolve()
    if root.exists():
        raise Phase3LivePilotError("live bundle root already exists")
    root.mkdir(parents=True, mode=0o700)
    candidate = validate_phase3_instance_authority_candidates(candidates)
    selection = validate_phase3_selection_authority(selection_authority)
    resource_receipt = validate_phase3_resource_preflight_receipt(
        resource_preflight_receipt
    )
    release = _validate_runtime_release(runtime_release)
    execution = build_phase3_execution_authority(
        agentteam_release_commit=release["source_commit"],
        codex_cli_version=_codex_version(),
        environment_version="ubuntu-24.04.4-linux-x86_64-cgroup-v2-v1",
        candidates=candidate,
    )
    decisions = approved_phase3_pilot_decisions()
    budget = decisions["execution"]["per_instance_budget"]
    materialization = materialize_phase3_instance_authorities(
        selection_authority=selection,
        decisions=decisions,
        task_inputs_by_instance={
            instance_id: candidate["instances_by_id"][instance_id]["worker_visible"]
            for instance_id in selection["selection"]["ordered_instance_ids"]
        },
        execution_profile=phase3_execution_profile_from_authority(
            execution,
            candidates=candidate,
        ),
        shared_budget={
            "max_total_tokens": budget["max_total_tokens"],
            "max_wall_time_seconds": budget["max_wall_time_seconds"],
            "max_operator_interactions": 0,
            "allowed_operator_input_types": ["decision_escalation"],
        },
        research_authority_sha256=_file_sha256(research_authority_path),
        mode_order=[list(order) for order in APPROVED_PHASE3_MODE_ORDER],
    )
    pilot_contract = build_phase3_aggregate_pilot_contract(
        selection_authority=selection,
        instance_materialization=materialization,
        retry_policy=APPROVED_PHASE3_RETRY_POLICY,
        abort_conditions=APPROVED_PHASE3_ABORT_CONDITIONS,
    )
    preflight = build_phase3_provider_free_preflight_receipt(
        pilot_contract=pilot_contract,
        selection_authority=selection,
        instance_materialization=materialization,
        resource_preflight_receipt=resource_receipt,
    )
    epoch = _build_epoch(pilot_contract, epoch_number)
    authorization = _build_live_authorization(
        pilot_contract,
        epoch,
        operator_identity=operator_identity,
    )
    evaluator_artifact = Path(common_evaluator_artifact).resolve(strict=True)
    environment_lock = Path(evaluator_environment_lock).resolve(strict=True)
    frozen_lock = root / "evaluator-environment.lock"
    shutil.copyfile(environment_lock, frozen_lock)
    os.chmod(frozen_lock, 0o400)
    protocols = {}
    runtime_taskpacks = {}
    sources = _validate_repository_sources(
        repository_sources,
        selection["selection"]["ordered_instance_ids"],
    )
    public_environments = _validate_public_verification_environments(
        public_verification_environments,
        selection["selection"]["ordered_instance_ids"],
    )
    for instance_id in selection["selection"]["ordered_instance_ids"]:
        public_environment = public_environments[instance_id]
        protocols[instance_id] = build_phase3_experiment_protocol(
            instance_id=instance_id,
            preregistration=materialization["preregistrations_by_instance"][instance_id],
            repository_source=sources[instance_id],
            common_evaluator_artifact=evaluator_artifact,
            public_verification_environment=public_environment,
        )
        runtime_taskpacks[instance_id] = materialize_phase3_runtime_taskpack(
            instance_id=instance_id,
            benchmark_taskpack=materialization["direct_taskpacks_by_instance"][instance_id],
            project_root=sources[instance_id],
            output_root=root / "taskpacks" / _compact_id(instance_id),
            model=pilot_contract["contract"]["execution_profile"]["model"]["model"],
            public_verification_environment=public_environment,
        )
    evaluator_environment = {
        "schema_version": "phase3_evaluator_environment.v1",
        "python": str(Path(evaluator_python).resolve(strict=True)),
        "environment_lock_path": str(frozen_lock),
        "environment_lock_sha256": _file_sha256(frozen_lock),
        "arrow_path": str(Path(arrow_path).resolve(strict=True)),
        "arrow_sha256": arrow_sha256,
        "harness_root": str(Path(harness_root).resolve(strict=True)),
        "harness_commit": harness_commit,
        "docker_socket": docker_socket,
    }
    authorities = {
        "candidate-authority.json": candidate,
        "selection-authority.json": selection,
        "execution-authority.json": execution,
        "instance-materialization.json": materialization,
        "pilot-contract.json": pilot_contract,
        "provider-free-preflight.json": preflight,
        "live-epoch.json": epoch,
        "live-authorization.json": authorization,
        "protocols.json": protocols,
        "runtime-taskpacks.json": runtime_taskpacks,
        "runtime-release.json": release,
        "repository-sources.json": sources,
        "evaluator-environment.json": evaluator_environment,
    }
    authorities["public-verification-environments.json"] = public_environments
    artifact_sha256 = {}
    for filename, value in authorities.items():
        publication = publish_immutable_json(
            root / filename,
            value,
            label=f"Phase 3 live {filename}",
        )
        artifact_sha256[filename] = publication["sha256"]
    bundle = {
        "schema_version": LIVE_BUNDLE_VERSION,
        "status": "authorized",
        "epoch_number": epoch_number,
        "epoch_sha256": epoch["epoch_sha256"],
        "pilot_contract_sha256": pilot_contract["contract_sha256"],
        "runtime_release_commit": release["source_commit"],
        "ordered_instance_ids": selection["selection"]["ordered_instance_ids"],
        "artifact_sha256": artifact_sha256,
        "provider_calls_at_freeze": 0,
        "scored_mode_executions_at_freeze": 0,
    }
    bundle["bundle_sha256"] = canonical_json_sha256(bundle)
    publish_immutable_json(root / "bundle.json", bundle, label="Phase 3 live bundle")
    return {"bundle_root": str(root), "bundle": bundle}


def run_phase3_live_bundle(bundle_root, pilot_root, *, max_executions=None):
    """Run or resume a frozen bundle with one owner for all resource parents."""

    root = Path(bundle_root).resolve(strict=True)
    bundle = _read_json(root / "bundle.json", "live bundle")
    if bundle.get("schema_version") != LIVE_BUNDLE_VERSION:
        raise Phase3LivePilotError("live bundle version is invalid")
    digest_body = dict(bundle)
    digest = digest_body.pop("bundle_sha256", None)
    if digest != canonical_json_sha256(digest_body):
        raise Phase3LivePilotError("live bundle digest changed")
    values = {}
    for filename, expected in bundle["artifact_sha256"].items():
        path = root / filename
        value = _read_json(path, filename)
        if canonical_json_sha256(value) != expected:
            raise Phase3LivePilotError(f"live bundle artifact changed: {filename}")
        values[filename] = value
    evaluator_environment = values["evaluator-environment.json"]
    if _file_sha256(evaluator_environment["environment_lock_path"]) != (
        evaluator_environment["environment_lock_sha256"]
    ):
        raise Phase3LivePilotError("frozen evaluator environment lock changed")
    if Path(os.path.realpath(os.sys.executable)) != Path(
        os.path.realpath(evaluator_environment["python"])
    ):
        raise Phase3LivePilotError(
            "live pilot must run with the frozen evaluator Python"
        )
    candidate = validate_phase3_instance_authority_candidates(
        values["candidate-authority.json"]
    )
    selection_authority = validate_phase3_selection_authority(
        values["selection-authority.json"]
    )
    materialization = values["instance-materialization.json"]
    public_environments = values.get("public-verification-environments.json")
    binding = approved_phase3_resource_envelope_binding()
    pilot_id = "phase3-live-" + bundle["bundle_sha256"][:16]
    owner = Phase3PilotResourceOwner(binding, pilot_id=pilot_id)
    references = owner.prepare()
    evidence = None
    cleanup = None
    try:
        evaluator = Phase3SweEvoEvaluator(
            arrow_path=evaluator_environment["arrow_path"],
            arrow_sha256=evaluator_environment["arrow_sha256"],
            harness_root=evaluator_environment["harness_root"],
            harness_commit=evaluator_environment["harness_commit"],
            instances_by_id=candidate["instances_by_id"],
            evaluator_root=Path(pilot_root).resolve() / "evaluator",
            resource_envelope_binding=binding,
            docker_socket=evaluator_environment["docker_socket"],
        )
        executor = Phase3ProductionExecutor(
            experiment_root=Path(pilot_root).resolve() / "mode-runs",
            protocols_by_instance=values["protocols.json"],
            runtime_release=values["runtime-release.json"],
            runtime_taskpacks_by_instance=values["runtime-taskpacks.json"],
            sandbox_configuration=(
                _live_sandbox_configuration(pilot_root)
                if public_environments is None
                else None
            ),
            sandbox_configurations_by_instance=(
                None
                if public_environments is None
                else {
                    instance_id: _live_sandbox_configuration(
                        pilot_root,
                        public_environment=public_environments[instance_id],
                    )
                    for instance_id in bundle["ordered_instance_ids"]
                }
            ),
            integration_verification_commands_by_instance=(
                None
                if public_environments is None
                else {
                    instance_id: public_environment_host_command(
                        public_environments[instance_id],
                        values["protocols.json"][instance_id]["acceptance"][
                            "command"
                        ],
                    )
                    for instance_id in bundle["ordered_instance_ids"]
                }
            ),
            common_evaluator_artifact=(
                Path(values["runtime-release.json"]["runtime_root"])
                / "agentteam_runtime"
                / "phase3_visible_acceptance_evaluator.py"
            ),
            official_evaluator=evaluator,
            resource_envelope_binding=binding,
            resource_hierarchy_references=references,
        )
        runner = Phase3PilotRunner(
            Path(pilot_root).resolve() / "state",
            pilot_id=pilot_id,
            pilot_contract=values["pilot-contract.json"],
            live_authorization=values["live-authorization.json"],
            selection=selection_authority["selection"],
            preregistrations_by_instance=materialization["preregistrations_by_instance"],
            expected_epoch_number=bundle["epoch_number"],
            expected_epoch_sha256=bundle["epoch_sha256"],
            executor=executor,
        )
        state = runner.run(max_executions=max_executions)
        evidence = owner.evidence()
        return state
    finally:
        cleanup = owner.cleanup()
        _write_session_evidence(pilot_root, evidence, cleanup)


def _build_epoch(pilot_contract, epoch_number):
    body = {
        "schema_version": LIVE_EPOCH_VERSION,
        "epoch_number": epoch_number,
        "decision_id": pilot_contract["contract"]["decision_id"],
        "pilot_contract_sha256": pilot_contract["contract_sha256"],
        "execution_profile": pilot_contract["contract"]["execution_profile"],
        "aggregate_budget_ceiling": pilot_contract["contract"][
            "aggregate_budget_ceiling"
        ],
        "modes": pilot_contract["contract"]["modes"],
    }
    body["epoch_sha256"] = canonical_json_sha256(body)
    return body


def _build_live_authorization(pilot_contract, epoch, *, operator_identity):
    body = pilot_contract["contract"]
    return {
        "schema_version": LIVE_AUTHORIZATION_SCHEMA_VERSION,
        "decision": "approved",
        "operator_identity": operator_identity,
        "authorized_at": _utc_now(),
        "gate_id": "P3-LIVE",
        "epoch_number": epoch["epoch_number"],
        "epoch_sha256": epoch["epoch_sha256"],
        "pilot_contract_sha256": pilot_contract["contract_sha256"],
        "readiness_evidence_sha256": body["readiness_binding"]["evidence_sha256"],
        "selection_sha256": body["selection"]["selection_sha256"],
        "agentteam_release_commit": body["execution_profile"]["runtime"][
            "agentteam_release_commit"
        ],
        "model": body["execution_profile"]["model"]["model"],
        "reasoning_profile": body["execution_profile"]["model"][
            "reasoning_profile"
        ],
        "max_total_tokens": body["aggregate_budget_ceiling"]["maximum_total_tokens"],
        "max_wall_time_seconds": body["aggregate_budget_ceiling"][
            "maximum_wall_time_seconds"
        ],
        "max_inflight_model_invocations": 1,
        "modes": body["modes"],
    }


def _live_sandbox_configuration(pilot_root, public_environment=None):
    root = Path(pilot_root).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    canary = root / "gold-canary"
    if not canary.exists():
        canary.write_text("phase3-live-gold-canary\n", encoding="ascii")
        os.chmod(canary, 0o600)
    codex = Path(shutil.which("codex") or "").resolve(strict=True)
    codex_code_mode_host = codex.with_name("codex-code-mode-host")
    if not codex_code_mode_host.is_file():
        raise Phase3LivePilotError(
            "Codex code-mode host is unavailable beside the Codex executable"
        )
    home = Path.home() / ".codex"
    credentials = []
    for source, target in (
        (home / "auth.json", "/run/agentteam-credentials/codex/auth.json"),
        (home / "config.toml", "/run/agentteam-credentials/codex/config.toml"),
    ):
        if source.is_file() and not source.is_symlink():
            credentials.append({"source": str(source), "target": target})
    environment = {
        "CODEX_HOME": "/run/agentteam-credentials/codex",
        "PATH": "/opt/agentteam/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    dependency_view = None
    if public_environment is not None:
        public_environment = validate_public_verification_environment(
            public_environment
        )
        dependency_view = public_environment_library_view(public_environment)
        environment["PATH"] = public_environment_path(
            public_environment,
            environment["PATH"],
        )
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return {
        "runtime_views": [
            *[
                {"source": path, "target": path}
                for path in ("/usr", "/lib", "/lib64", "/bin")
                if Path(path).exists()
            ],
            {"source": str(codex), "target": "/opt/agentteam/bin/codex"},
            {
                "source": str(codex_code_mode_host),
                "target": "/opt/agentteam/bin/codex-code-mode-host",
            },
        ],
        "library_views": [
            {"source": path, "target": path}
            for path in (
                "/run/systemd/resolve/stub-resolv.conf", "/etc/hosts",
                "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/ssl/certs",
            )
            if Path(path).exists()
        ] + ([dependency_view] if dependency_view is not None else []),
        "credential_mounts": credentials,
        "environment": environment,
        "canary_path": str(canary),
    }


def _validate_runtime_release(value):
    required = {
        "release_id", "release_root", "runtime_root", "release_manifest_sha256",
        "source_commit", "git_object_format",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise Phase3LivePilotError("runtime release identity is invalid")
    manifest = Path(value["release_root"]) / "manifest.json"
    if _file_sha256(manifest) != value["release_manifest_sha256"]:
        raise Phase3LivePilotError("runtime release manifest changed")
    return copy.deepcopy(value)


def _validate_repository_sources(sources, ordered_ids):
    if not isinstance(sources, dict) or set(sources) != set(ordered_ids):
        raise Phase3LivePilotError("repository sources do not cover selection")
    return {key: str(Path(sources[key]).resolve(strict=True)) for key in ordered_ids}


def _validate_public_verification_environments(environments, ordered_ids):
    if environments is None:
        raise Phase3LivePilotError(
            "new live bundles require public verification environments"
        )
    if not isinstance(environments, dict) or set(environments) != set(ordered_ids):
        raise Phase3LivePilotError(
            "public verification environments do not cover selection"
        )
    return {
        instance_id: validate_public_verification_environment(
            environments[instance_id],
            instance_id=instance_id,
        )
        for instance_id in ordered_ids
    }


def _codex_version():
    import subprocess

    return subprocess.run(
        ["codex", "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_output(root, *arguments):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Phase3LivePilotError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Phase3LivePilotError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise Phase3LivePilotError(f"{label} is invalid")
    return value


def _write_session_evidence(pilot_root, evidence, cleanup):
    root = Path(pilot_root).resolve() / "resource-sessions"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = {
        "schema_version": "phase3_live_resource_session.v1",
        "recorded_at": _utc_now(),
        "evidence": evidence,
        "cleanup": cleanup,
    }
    path = root / (hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() + ".json")
    publish_immutable_json(path, payload, label="Phase 3 resource session")


def _compact_id(value):
    return "".join(character if character.isalnum() else "-" for character in value)


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run or resume a frozen Phase 3 pilot")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--pilot-root", required=True)
    parser.add_argument("--max-executions", type=int)
    args = parser.parse_args(argv)
    state = run_phase3_live_bundle(
        args.bundle,
        args.pilot_root,
        max_executions=args.max_executions,
    )
    print(json.dumps(state, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
