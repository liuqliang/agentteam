"""Gold-isolated SWE-EVO evaluator for Phase 3 scored executions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path


class Phase3SweEvoEvaluatorError(RuntimeError):
    """Raised when frozen evaluator authority cannot be honored exactly."""


class Phase3SweEvoEvaluator:
    """Score a candidate patch with frozen SWE-bench code and image authority."""

    def __init__(
        self,
        *,
        arrow_path,
        arrow_sha256,
        harness_root,
        harness_commit,
        instances_by_id,
        evaluator_root,
        resource_envelope_binding,
        docker_socket="unix:///run/user/1013/podman/podman.sock",
        timeout_seconds=1800,
    ):
        self.arrow_path = Path(arrow_path).resolve(strict=True)
        self.harness_root = Path(harness_root).resolve(strict=True)
        self.harness_commit = harness_commit
        self.instances = copy.deepcopy(instances_by_id)
        self.evaluator_root = Path(evaluator_root).resolve()
        self.evaluator_root.mkdir(parents=True, exist_ok=True)
        if self.evaluator_root.is_symlink():
            raise Phase3SweEvoEvaluatorError("evaluator root cannot be a symlink")
        self.docker_socket = docker_socket
        self.timeout_seconds = timeout_seconds
        from .resource_envelope import mode_envelopes

        self.resource_limits_by_mode = {
            mode: mode_envelopes(resource_envelope_binding, mode)[
                "evaluator_envelope"
            ]
            for mode in ("single_codex", "agentteam_direct", "agentteam_full")
        }
        if _file_sha256(self.arrow_path) != arrow_sha256:
            raise Phase3SweEvoEvaluatorError("SWE-EVO Arrow authority changed")
        if _git_output(self.harness_root, "rev-parse", "HEAD") != harness_commit:
            raise Phase3SweEvoEvaluatorError("SWE-bench evaluator commit changed")

    def evaluate_resource_bound(
        self,
        entry,
        patch_path,
        *,
        resource_envelope_binding,
        resource_hierarchy_reference,
        evidence_path,
        command_runner=None,
        hierarchy_factory=None,
        monitor_factory=None,
    ):
        """Run the evaluator client in its verified transient service."""

        from .experiment_contract import publish_immutable_json
        from .resource_envelope import (
            ResourceUnitMonitor,
            SystemdResourceHierarchy,
        )

        hierarchy_factory = hierarchy_factory or SystemdResourceHierarchy
        monitor_factory = monitor_factory or ResourceUnitMonitor
        runner = command_runner or subprocess.run
        hierarchy = hierarchy_factory(
            resource_envelope_binding,
            run_id=resource_hierarchy_reference["run_id"],
            mode=entry["mode"],
            owner_reference=resource_hierarchy_reference,
            command_runner=runner,
        )
        hierarchy.prepare(check_host=False)
        request_id = hashlib.sha256(entry["entry_id"].encode("utf-8")).hexdigest()
        request_root = self.evaluator_root / "requests" / request_id
        request_root.mkdir(parents=True, exist_ok=True)
        request_path = request_root / "request.json"
        score_path = request_root / "score.json"
        score_path.unlink(missing_ok=True)
        _write_private_json(
            request_path,
            {
                "schema_version": "phase3_swe_evo_evaluator_request.v1",
                "configuration": self.configuration(),
                "entry": copy.deepcopy(entry),
                "patch_path": str(Path(patch_path).resolve(strict=True)),
            },
        )
        unit = f"agentteam-p3-{request_id[:16]}-evaluator.service"
        command = hierarchy.leaf_command(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.phase3_swe_evo_evaluator",
                "--request",
                str(request_path),
                "--output",
                str(score_path),
            ],
            unit=unit,
            evaluator=True,
        )
        monitor = monitor_factory(
            hierarchy,
            unit,
            scope="evaluator",
            evaluator=True,
        ).start()
        timed_out = False
        try:
            try:
                completed = runner(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=self.timeout_seconds + 120,
                )
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                hierarchy.stop_transient_unit(unit)
                raise Phase3SweEvoEvaluatorError(
                    "resource-bound evaluator exceeded its timeout"
                ) from exc
            evidence = monitor.finish(
                binding=resource_envelope_binding,
                timed_out=timed_out,
            )
        except Exception:
            monitor.cancel()
            hierarchy.stop_transient_unit(unit)
            raise
        if completed.returncode != 0:
            reason = (completed.stderr or completed.stdout or "").strip()[:500]
            raise Phase3SweEvoEvaluatorError(
                f"resource-bound evaluator failed: {reason}"
            )
        score = _read_private_json(score_path, "resource-bound evaluator score")
        publish_immutable_json(
            evidence_path,
            evidence,
            label="Phase 3 official evaluator resource evidence",
        )
        return score

    def configuration(self):
        return {
            "arrow_path": str(self.arrow_path),
            "arrow_sha256": _file_sha256(self.arrow_path),
            "harness_root": str(self.harness_root),
            "harness_commit": self.harness_commit,
            "instances_by_id": copy.deepcopy(self.instances),
            "evaluator_root": str(self.evaluator_root),
            "resource_envelope_binding": {
                "schema_version": "phase3_resource_envelope.v2",
                **_resource_binding_from_limits(self.resource_limits_by_mode),
            },
            "docker_socket": self.docker_socket,
            "timeout_seconds": self.timeout_seconds,
        }

    def __call__(self, entry, patch_path):
        started = time.monotonic()
        instance_id = entry.get("instance_id")
        authority = self.instances.get(instance_id)
        if not isinstance(authority, dict):
            raise Phase3SweEvoEvaluatorError("instance evaluator authority is missing")
        evaluator = authority.get("evaluator_only")
        image = evaluator.get("image") if isinstance(evaluator, dict) else None
        if not isinstance(image, dict):
            raise Phase3SweEvoEvaluatorError("instance image authority is missing")
        patch = Path(patch_path).resolve(strict=True).read_text(encoding="utf-8")
        row = self._load_row(instance_id)
        self._verify_gold_bindings(row, evaluator["gold_bindings"])
        modules = self._harness_modules()
        test_spec = modules["make_test_spec"](row, namespace="frozen")

        class BoundTestSpec(type(test_spec)):
            @property
            def instance_image_key(self):
                return self._frozen_image_key

        bound = BoundTestSpec(**asdict(test_spec))
        bound._frozen_image_key = image["reference"] + "@" + image["manifest_digest"]
        raw_client = modules["docker"].DockerClient(base_url=self.docker_socket)
        client = _ResourceBoundDockerClient(
            raw_client,
            self.resource_limits_by_mode[entry["mode"]],
        )
        prediction = {
            "instance_id": instance_id,
            "model_name_or_path": "agentteam-phase3",
            "model_patch": patch,
        }
        run_id = "phase3-" + hashlib.sha256(entry["entry_id"].encode()).hexdigest()[:16]
        run_root = self.evaluator_root / run_id
        if run_root.exists():
            shutil.rmtree(run_root)
        run_root.mkdir()
        previous_cwd = Path.cwd()
        try:
            os.chdir(run_root)
            result = modules["run_instance"](
                bound,
                prediction,
                False,
                False,
                client,
                run_id,
                self.timeout_seconds,
                base_commit=row["base_commit"],
            )
            inspected = client.images.get(bound.instance_image_key).attrs
        finally:
            os.chdir(previous_cwd)
            client.close()
        repo_digests = inspected.get("RepoDigests", [])
        if not any(value.endswith("@" + image["manifest_digest"]) for value in repo_digests):
            raise Phase3SweEvoEvaluatorError("evaluator image digest was not verified")
        report = {
            "completed": bool(result.get("completed")),
            "resolved": bool(result.get("resolved")),
        }
        if not report["completed"]:
            log_path = (
                run_root
                / "logs"
                / "run_evaluation"
                / run_id
                / prediction["model_name_or_path"]
                / instance_id
                / "run_instance.log"
            )
            log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            model_failure = not patch.strip() or "Patch Apply Failed" in log
            if not model_failure:
                raise Phase3SweEvoEvaluatorError(
                    "official evaluator did not complete and no model failure was proven"
                )
        upstream_report = (
            run_root
            / "logs"
            / "run_evaluation"
            / run_id
            / prediction["model_name_or_path"]
            / instance_id
            / "report.json"
        )
        report_bytes = (
            upstream_report.read_bytes()
            if upstream_report.is_file()
            else json.dumps(report, sort_keys=True, separators=(",", ":")).encode("ascii")
        )
        aggregate = _aggregate_upstream_report(report_bytes, instance_id)
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
        shutil.rmtree(run_root)
        return {
            "schema_version": "phase3_official_score.v1",
            "status": "completed" if report["completed"] else "failed",
            "resolved": report["resolved"],
            **aggregate,
            "image_manifest_digest": image["manifest_digest"],
            "evaluator_sha256": hashlib.sha256(
                (
                    self.harness_commit
                    + "\0"
                    + _git_output(self.harness_root, "rev-parse", "HEAD^{tree}")
                ).encode("ascii")
            ).hexdigest(),
            "report_sha256": report_sha256,
            "wall_time_seconds": time.monotonic() - started,
        }

    def _harness_modules(self):
        root = str(self.harness_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            import docker
            from swebench.harness.run_evaluation import run_instance
            from swebench.harness.test_spec.test_spec import make_test_spec
        except ImportError as exc:
            raise Phase3SweEvoEvaluatorError(
                "frozen SWE-bench evaluator dependencies are unavailable"
            ) from exc
        return {"docker": docker, "run_instance": run_instance, "make_test_spec": make_test_spec}

    def _load_row(self, instance_id):
        try:
            import pyarrow as pa
            import pyarrow.compute as pc
            import pyarrow.ipc as ipc
        except ImportError as exc:
            raise Phase3SweEvoEvaluatorError("pyarrow is required by the evaluator") from exc
        with self.arrow_path.open("rb") as source:
            try:
                table = ipc.open_stream(source).read_all()
            except pa.ArrowInvalid:
                source.seek(0)
                table = ipc.open_file(source).read_all()
        selected = table.filter(pc.equal(table["instance_id"], instance_id)).to_pylist()
        if len(selected) != 1:
            raise Phase3SweEvoEvaluatorError("selected evaluator row is not unique")
        return selected[0]

    @staticmethod
    def _verify_gold_bindings(row, bindings):
        text_fields = {
            "patch_sha256": "patch",
            "test_patch_sha256": "test_patch",
        }
        canonical_fields = {
            "all_patch_sha256": "all_patch",
            "fail_to_pass_sha256": "FAIL_TO_PASS",
            "pass_to_pass_sha256": "PASS_TO_PASS",
        }
        for digest_name, row_name in text_fields.items():
            value = row.get(row_name)
            if not isinstance(value, str) or hashlib.sha256(value.encode()).hexdigest() != bindings[digest_name]:
                raise Phase3SweEvoEvaluatorError("evaluator gold binding changed")
        for digest_name, row_name in canonical_fields.items():
            value = row.get(row_name)
            digest = hashlib.sha256(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest()
            if digest != bindings[digest_name]:
                raise Phase3SweEvoEvaluatorError("evaluator gold binding changed")


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_output(root, *arguments):
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _aggregate_upstream_report(report_bytes, instance_id):
    try:
        payload = json.loads(report_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    record = payload.get(instance_id, {}) if isinstance(payload, dict) else {}
    tests = record.get("tests_status", {}) if isinstance(record, dict) else {}

    def counts(name):
        group = tests.get(name, {}) if isinstance(tests, dict) else {}
        success = group.get("success", []) if isinstance(group, dict) else []
        failure = group.get("failure", []) if isinstance(group, dict) else []
        return len(success), len(success) + len(failure)

    f2p_success, f2p_total = counts("FAIL_TO_PASS")
    p2p_success, p2p_total = counts("PASS_TO_PASS")
    patch_applied = bool(record.get("patch_successfully_applied"))
    f2p_ratio = f2p_success / f2p_total if f2p_total else float(patch_applied)
    p2p_ratio = p2p_success / p2p_total if p2p_total else float(patch_applied)
    partial = 0.7 * f2p_ratio + 0.2 * p2p_ratio + 0.1 * float(patch_applied)
    return {
        "patch_applied": patch_applied,
        "f2p_success": f2p_success,
        "f2p_total": f2p_total,
        "p2p_success": p2p_success,
        "p2p_total": p2p_total,
        "swe_style_partial_score": round(partial, 6),
    }


class _ResourceBoundDockerClient:
    def __init__(self, client, limits):
        self._client = client
        self.images = client.images
        self.api = client.api
        self.containers = _ResourceBoundContainers(client.containers, limits)

    def close(self):
        self._client.close()


class _ResourceBoundContainers:
    def __init__(self, containers, limits):
        self._containers = containers
        self._limits = copy.deepcopy(limits)

    def create(self, *args, **kwargs):
        expected = {
            "cpu_period": 100_000,
            "cpu_quota": self._limits["cpu_quota"] * 100_000,
            "mem_limit": self._limits["memory_max_bytes"],
            "mem_reservation": self._limits["memory_high_bytes"],
            "memswap_limit": self._limits["memory_max_bytes"],
            "pids_limit": self._limits["tasks_max"],
        }
        overlap = set(expected).intersection(kwargs)
        if overlap:
            raise Phase3SweEvoEvaluatorError("upstream evaluator supplied resource limits")
        container = self._containers.create(*args, **kwargs, **expected)
        container.reload()
        host = container.attrs.get("HostConfig", {})
        observed = {
            "cpu_period": host.get("CpuPeriod"),
            "cpu_quota": host.get("CpuQuota"),
            "mem_limit": host.get("Memory"),
            "mem_reservation": host.get("MemoryReservation"),
            "memswap_limit": host.get("MemorySwap"),
            "pids_limit": host.get("PidsLimit"),
        }
        if observed != expected:
            try:
                container.remove(force=True)
            finally:
                raise Phase3SweEvoEvaluatorError(
                    "evaluator container resource readback differs from authority"
                )
        return container


def _resource_binding_from_limits(limits_by_mode):
    """Recover the approved binding without serializing mutable internals."""

    from .resource_envelope import approved_phase3_resource_envelope_binding

    binding = approved_phase3_resource_envelope_binding()
    expected = {
        mode: binding["envelopes"][
            binding["hierarchy"]["modes"][mode]["evaluator_envelope"]
        ]
        for mode in limits_by_mode
    }
    if expected != limits_by_mode:
        raise Phase3SweEvoEvaluatorError(
            "evaluator resource limits differ from approved authority"
        )
    body = copy.deepcopy(binding)
    body.pop("schema_version")
    return body


def _write_private_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n",
        encoding="ascii",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_private_json(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Phase3SweEvoEvaluatorError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Phase3SweEvoEvaluatorError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise Phase3SweEvoEvaluatorError(f"{label} is invalid")
    return value


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Run one frozen SWE-EVO evaluation")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    request = _read_private_json(args.request, "evaluator request")
    if request.get("schema_version") != "phase3_swe_evo_evaluator_request.v1":
        raise Phase3SweEvoEvaluatorError("evaluator request version is invalid")
    configuration = request.get("configuration")
    entry = request.get("entry")
    if not isinstance(configuration, dict) or not isinstance(entry, dict):
        raise Phase3SweEvoEvaluatorError("evaluator request fields are invalid")
    score = Phase3SweEvoEvaluator(**configuration)(entry, request.get("patch_path"))
    _write_private_json(args.output, score)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(_main())
