"""Versioned Phase 3 resource envelopes and cgroup-v2 evidence.

The binding in this module is deliberately separate from
``experiment_protocol.v1``.  A historical protocol therefore remains valid
without a resource binding, while Phase 3 launchers can require this additive,
digest-bound contract before admitting any provider or evaluator process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


RESOURCE_ENVELOPE_SCHEMA_VERSION = "phase3_resource_envelope.v2"
RESOURCE_EVIDENCE_SCHEMA_VERSION = "phase3_resource_evidence.v1"
RESOURCE_PREFLIGHT_SCHEMA_VERSION = "phase3_resource_preflight.v1"
RESOURCE_FAILURE_CLASSIFICATION = "resource_limit_exhausted"
RESOURCE_FAIRNESS_POLICY = (
    "equal_workload_slots_with_metered_agentteam_control_plane_allowance"
)
GIB = 1024**3
RESOURCE_MONITOR_ACK_TIMEOUT_SECONDS = 10.0

_MODES = ("single_codex", "agentteam_direct", "agentteam_full")
_ENVELOPE_FIELDS = (
    "cpu_quota",
    "memory_high_bytes",
    "memory_max_bytes",
    "tasks_max",
    "memory_swap_max_bytes",
)
_SHOW_PROPERTIES = (
    "CPUQuotaPerSecUSec",
    "MemoryHigh",
    "MemoryMax",
    "TasksMax",
    "MemorySwapMax",
    "ControlGroup",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class ResourceEnvelopeError(RuntimeError):
    """The resource contract is invalid or cannot be enforced exactly."""


class ResourceEnvelopeUnavailable(ResourceEnvelopeError):
    """The host cannot provide a verified user-systemd cgroup hierarchy."""


def approved_phase3_resource_envelope_binding():
    """Return the frozen provider-free Phase 3B resource contract."""

    return {
        "schema_version": RESOURCE_ENVELOPE_SCHEMA_VERSION,
        "fairness_policy": RESOURCE_FAIRNESS_POLICY,
        "failure_classification": RESOURCE_FAILURE_CLASSIFICATION,
        "envelopes": {
            "common_workload_slot": _envelope(4, 8 * GIB, 12 * GIB, 384),
            "single_codex_mode": _envelope(4, 8 * GIB, 12 * GIB, 384),
            "agentteam_control_plane_allowance": _envelope(
                4, 8 * GIB, 12 * GIB, 384
            ),
            "agentteam_mode": _envelope(8, 16 * GIB, 24 * GIB, 768),
            "pilot_project": _envelope(16, 32 * GIB, 48 * GIB, 1024),
        },
        "hierarchy": {
            "project_envelope": "pilot_project",
            "modes": {
                "single_codex": {
                    "mode_envelope": "single_codex_mode",
                    "control_plane_envelope": None,
                    "workload_envelope": "common_workload_slot",
                    "evaluator_envelope": "common_workload_slot",
                },
                "agentteam_direct": {
                    "mode_envelope": "agentteam_mode",
                    "control_plane_envelope": (
                        "agentteam_control_plane_allowance"
                    ),
                    "workload_envelope": "common_workload_slot",
                    "evaluator_envelope": "common_workload_slot",
                },
                "agentteam_full": {
                    "mode_envelope": "agentteam_mode",
                    "control_plane_envelope": (
                        "agentteam_control_plane_allowance"
                    ),
                    "workload_envelope": "common_workload_slot",
                    "evaluator_envelope": "common_workload_slot",
                },
            },
        },
        "systemd": {
            "manager": "user",
            "cgroup_version": 2,
            "kill_mode": "control-group",
            "readback_required": True,
            "cleanup_requires_empty_cgroup": True,
        },
    }


def _envelope(cpu, memory_high, memory_max, tasks_max):
    return {
        "cpu_quota": cpu,
        "memory_high_bytes": memory_high,
        "memory_max_bytes": memory_max,
        "tasks_max": tasks_max,
        "memory_swap_max_bytes": 0,
    }


def validate_resource_envelope_binding(binding):
    """Validate the schema and the frozen cross-envelope invariants."""

    if not isinstance(binding, dict):
        raise ResourceEnvelopeError("resource envelope binding must be an object")
    schema_file = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "phase3_resource_envelope.schema.json"
    )
    try:
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceEnvelopeError("resource envelope schema is unavailable") from exc
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(binding),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        prefix = f" at {location}" if location else ""
        raise ResourceEnvelopeError(
            f"resource envelope schema violation{prefix}: {errors[0].message}"
        )
    if binding != approved_phase3_resource_envelope_binding():
        raise ResourceEnvelopeError(
            "resource envelope binding differs from the approved Phase 3 contract"
        )
    return binding


def canonical_resource_envelope_sha256(binding):
    validate_resource_envelope_binding(binding)
    payload = json.dumps(
        binding,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def envelope_for(binding, name):
    validate_resource_envelope_binding(binding)
    try:
        return deepcopy(binding["envelopes"][name])
    except KeyError as exc:  # pragma: no cover - schema guards this
        raise ResourceEnvelopeError(f"unknown resource envelope: {name}") from exc


def mode_envelopes(binding, mode):
    validate_resource_envelope_binding(binding)
    if mode not in _MODES:
        raise ResourceEnvelopeError(f"unsupported resource mode: {mode!r}")
    names = binding["hierarchy"]["modes"][mode]
    return {
        kind: (None if name is None else envelope_for(binding, name))
        for kind, name in names.items()
    }


def verify_host_capacity(
    binding,
    *,
    cpu_count=None,
    memory_total_bytes=None,
    reserved_cpu=1,
    reserved_memory_bytes=GIB,
):
    """Fail closed unless the project envelope leaves explicit host headroom."""

    project = envelope_for(binding, "pilot_project")
    cpu_count = os.cpu_count() if cpu_count is None else cpu_count
    if memory_total_bytes is None:
        memory_total_bytes = _host_memory_total_bytes()
    if (
        not isinstance(cpu_count, int)
        or isinstance(cpu_count, bool)
        or cpu_count < project["cpu_quota"] + reserved_cpu
    ):
        raise ResourceEnvelopeUnavailable(
            "host CPU capacity cannot provide the project envelope and headroom"
        )
    if (
        not isinstance(memory_total_bytes, int)
        or isinstance(memory_total_bytes, bool)
        or memory_total_bytes
        < project["memory_max_bytes"] + reserved_memory_bytes
    ):
        raise ResourceEnvelopeUnavailable(
            "host memory cannot provide the project envelope and headroom"
        )
    return {
        "cpu_count": cpu_count,
        "memory_total_bytes": memory_total_bytes,
        "reserved_cpu": reserved_cpu,
        "reserved_memory_bytes": reserved_memory_bytes,
    }


def systemd_properties(envelope):
    _validate_envelope_values(envelope)
    return {
        "CPUQuota": f"{envelope['cpu_quota'] * 100}%",
        "MemoryHigh": str(envelope["memory_high_bytes"]),
        "MemoryMax": str(envelope["memory_max_bytes"]),
        "TasksMax": str(envelope["tasks_max"]),
        "MemorySwapMax": str(envelope["memory_swap_max_bytes"]),
    }


class SystemdResourceHierarchy:
    """Prepare and verify project/mode/control-plane user-systemd cgroups.

    Parent slices are started and fully read back before a leaf command is
    returned. Control-plane, workload, and evaluator transient services are
    launched below the verified mode slice by callers holding an owner
    reference.
    """

    def __init__(
        self,
        binding,
        *,
        run_id,
        mode,
        command_runner=None,
        cgroup_root="/sys/fs/cgroup",
        owner_reference=None,
    ):
        validate_resource_envelope_binding(binding)
        if not isinstance(run_id, str) or not _SAFE_ID.fullmatch(run_id):
            raise ResourceEnvelopeError("resource run_id is invalid")
        if mode not in _MODES:
            raise ResourceEnvelopeError(f"unsupported resource mode: {mode!r}")
        self.binding = deepcopy(binding)
        self.binding_sha256 = canonical_resource_envelope_sha256(binding)
        self.run_id = run_id
        self.mode = mode
        self.command_runner = command_runner or subprocess.run
        self.cgroup_root = Path(cgroup_root)
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
        mode_token = mode.replace("_", "-")
        self.project_slice = f"agentteam-p3-{digest}.slice"
        self.mode_slice = f"agentteam-p3-{digest}-{mode_token}.slice"
        self.control_scope = (
            f"agentteam-p3-{digest}-{mode_token}-control.service"
        )
        self._prepared = False
        self._control_attached = False
        self._identities = {}
        self._owner_reference = deepcopy(owner_reference)
        self._owns_parents = owner_reference is None

    def prepare(self, *, check_host=True, host_capacity=None):
        try:
            if not self._owns_parents:
                self._load_owner_reference()
                self._verify_existing_parents()
                self._prepared = True
                return self.identity()
            if check_host:
                verify_host_capacity(self.binding, **(host_capacity or {}))
            project = envelope_for(self.binding, "pilot_project")
            mode = mode_envelopes(self.binding, self.mode)["mode_envelope"]
            self._start_and_verify_slice(self.project_slice, project)
            self._start_and_verify_slice(self.mode_slice, mode)
            project_group = self._identities["project"]["ControlGroup"]
            mode_group = self._identities["mode"]["ControlGroup"]
            if not mode_group.startswith(project_group.rstrip("/") + "/"):
                raise ResourceEnvelopeUnavailable(
                    "mode cgroup escaped the verified project cgroup"
                )
            self._prepared = True
            return self.identity()
        except Exception:
            self.cleanup()
            raise

    def control_plane_command(self, command):
        if not self._prepared:
            raise ResourceEnvelopeError(
                "resource parents must be prepared before control-plane launch"
            )
        arguments = [
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--service-type=exec",
            f"--unit={self.control_scope}",
            *self.control_plane_arguments(),
            "--property=KillMode=control-group",
            "--",
            *_resource_wrapped_command(command, unit=self.control_scope),
        ]
        return arguments

    def leaf_command(self, command, *, unit, evaluator=False):
        if not isinstance(unit, str) or not unit.endswith(".service"):
            raise ResourceEnvelopeError("resource leaf unit is invalid")
        return [
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--service-type=exec",
            f"--unit={unit}",
            *self.leaf_arguments(evaluator=evaluator),
            "--property=KillMode=control-group",
            "--",
            *_resource_wrapped_command(command, unit=unit),
        ]

    def verify_control_plane(self):
        envelope = mode_envelopes(self.binding, self.mode)[
            "control_plane_envelope"
        ]
        if envelope is None:
            return None
        identity = self._verify_unit(self.control_scope, envelope)
        mode_group = self._identities["mode"]["ControlGroup"]
        if not identity["ControlGroup"].startswith(mode_group.rstrip("/") + "/"):
            raise ResourceEnvelopeUnavailable(
                "control-plane cgroup escaped the verified mode cgroup"
            )
        self._identities["control_plane"] = identity
        self._control_attached = True
        return deepcopy(identity)

    def leaf_arguments(self, *, evaluator=False):
        if not self._prepared:
            raise ResourceEnvelopeError("resource parents must be prepared first")
        key = "evaluator_envelope" if evaluator else "workload_envelope"
        envelope = mode_envelopes(self.binding, self.mode)[key]
        arguments = [f"--slice={self.mode_slice}"]
        for name, value in systemd_properties(envelope).items():
            arguments.append(f"--property={name}={value}")
        return arguments

    def control_plane_arguments(self):
        if not self._prepared:
            raise ResourceEnvelopeError("resource parents must be prepared first")
        envelope = mode_envelopes(self.binding, self.mode)[
            "control_plane_envelope"
        ]
        if envelope is None:
            raise ResourceEnvelopeError(
                "single Codex mode has no control-plane allowance"
            )
        arguments = [f"--slice={self.mode_slice}"]
        for name, value in systemd_properties(envelope).items():
            arguments.append(f"--property={name}={value}")
        return arguments

    def verify_leaf(self, unit, *, evaluator=False):
        key = "evaluator_envelope" if evaluator else "workload_envelope"
        envelope = mode_envelopes(self.binding, self.mode)[key]
        identity = self._verify_unit(unit, envelope)
        if identity["ControlGroup"].rsplit("/", 1)[0] != self._identities[
            "mode"
        ]["ControlGroup"]:
            raise ResourceEnvelopeUnavailable(
                "workload leaf escaped the verified mode cgroup"
            )
        return identity

    def stop_transient_unit(self, unit):
        if not isinstance(unit, str) or not unit.endswith(".service"):
            raise ResourceEnvelopeError("transient resource unit is invalid")
        return self._checked(
            ["systemctl", "--user", "stop", unit],
            ignore_errors=True,
        ).returncode == 0

    def identity(self):
        return {
            "binding_sha256": self.binding_sha256,
            "project_slice": self.project_slice,
            "mode_slice": self.mode_slice,
            "control_scope": (
                self.control_scope
                if mode_envelopes(self.binding, self.mode)[
                    "control_plane_envelope"
                ]
                is not None
                else None
            ),
            "units": deepcopy(self._identities),
            "owner": self._owns_parents,
        }

    def owner_reference(self):
        if not self._prepared or not self._owns_parents:
            raise ResourceEnvelopeError(
                "only a prepared owner can publish a resource hierarchy reference"
            )
        return {
            "schema_version": "phase3_resource_hierarchy_reference.v1",
            "binding_sha256": self.binding_sha256,
            "run_id": self.run_id,
            "mode": self.mode,
            "project_slice": self.project_slice,
            "mode_slice": self.mode_slice,
            "units": deepcopy(self._identities),
        }

    def cleanup(self, *, include_project=True):
        if not self._owns_parents:
            return {
                "cleanup_attempted": False,
                "cleanup_complete": True,
                "cleanup_owner": False,
                "stopped_units": [],
                "cgroup_population": {},
            }
        targets = []
        if self._control_attached:
            targets.append(self.control_scope)
        targets.append(self.mode_slice)
        if include_project:
            targets.append(self.project_slice)
        stopped = []
        for unit in targets:
            completed = self._checked(
                ["systemctl", "--user", "stop", unit],
                ignore_errors=True,
            )
            stopped.append({"unit": unit, "returncode": completed.returncode})
        population = {}
        for label, identity in self._identities.items():
            if label == "project" and not include_project:
                continue
            control_group = identity.get("ControlGroup")
            population[label] = _cgroup_cleanup_state(
                control_group,
                cgroup_root=self.cgroup_root,
            )
        cleanup_complete = all(
            value in {"removed", "drained"} for value in population.values()
        )
        return {
            "cleanup_attempted": True,
            "cleanup_complete": cleanup_complete,
            "stopped_units": stopped,
            "cgroup_population": population,
            "cleanup_owner": True,
        }

    def _load_owner_reference(self):
        reference = self._owner_reference
        if (
            not isinstance(reference, dict)
            or reference.get("schema_version")
            != "phase3_resource_hierarchy_reference.v1"
            or reference.get("binding_sha256") != self.binding_sha256
            or reference.get("run_id") != self.run_id
            or reference.get("mode") != self.mode
            or reference.get("project_slice") != self.project_slice
            or reference.get("mode_slice") != self.mode_slice
            or not isinstance(reference.get("units"), dict)
        ):
            raise ResourceEnvelopeError(
                "resource hierarchy owner reference is invalid"
            )
        self._identities = deepcopy(reference["units"])

    def _verify_existing_parents(self):
        expected = (
            ("project", self.project_slice, envelope_for(self.binding, "pilot_project")),
            (
                "mode",
                self.mode_slice,
                mode_envelopes(self.binding, self.mode)["mode_envelope"],
            ),
        )
        for label, unit, envelope in expected:
            actual = self._verify_unit(unit, envelope)
            recorded = self._identities.get(label)
            if (
                not isinstance(recorded, dict)
                or recorded.get("ControlGroup") != actual.get("ControlGroup")
            ):
                raise ResourceEnvelopeUnavailable(
                    "resource hierarchy owner identity changed"
                )
            self._identities[label] = actual

    def _start_and_verify_slice(self, unit, envelope):
        self._checked(["systemctl", "--user", "start", unit])
        properties = systemd_properties(envelope)
        self._checked(
            [
                "systemctl",
                "--user",
                "set-property",
                "--runtime",
                unit,
                *(f"{name}={value}" for name, value in properties.items()),
            ]
        )
        label = "project" if unit == self.project_slice else "mode"
        self._identities[label] = self._verify_unit(unit, envelope)

    def _verify_unit(self, unit, envelope):
        completed = self._checked(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                *(
                    f"--property={property_name}"
                    for property_name in _SHOW_PROPERTIES
                ),
            ]
        )
        actual = _parse_show(completed.stdout)
        missing = [name for name in _SHOW_PROPERTIES if not actual.get(name)]
        if missing:
            raise ResourceEnvelopeUnavailable(
                f"systemd resource readback missing for {unit}: {','.join(missing)}"
            )
        expected = {
            "CPUQuotaPerSecUSec": envelope["cpu_quota"] * 1_000_000,
            "MemoryHigh": envelope["memory_high_bytes"],
            "MemoryMax": envelope["memory_max_bytes"],
            "TasksMax": envelope["tasks_max"],
            "MemorySwapMax": envelope["memory_swap_max_bytes"],
        }
        observed = {
            "CPUQuotaPerSecUSec": _parse_systemd_time_usec(
                actual["CPUQuotaPerSecUSec"]
            ),
            **{
                name: _parse_systemd_integer(actual[name])
                for name in (
                    "MemoryHigh",
                    "MemoryMax",
                    "TasksMax",
                    "MemorySwapMax",
                )
            },
        }
        if observed != expected:
            raise ResourceEnvelopeUnavailable(
                f"systemd resource readback mismatch for {unit}"
            )
        if not actual["ControlGroup"].startswith("/"):
            raise ResourceEnvelopeUnavailable(
                f"systemd cgroup identity is invalid for {unit}"
            )
        return {**actual, "verified_limits": observed}

    def _checked(self, command, ignore_errors=False):
        try:
            completed = self.command_runner(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if ignore_errors:
                return subprocess.CompletedProcess(command, 1, "", str(exc))
            raise ResourceEnvelopeUnavailable(
                f"systemd resource command unavailable: {command[0]}"
            ) from exc
        if completed.returncode != 0 and not ignore_errors:
            reason = (completed.stderr or completed.stdout or "").strip()[:500]
            raise ResourceEnvelopeUnavailable(
                f"systemd resource command failed: {command[0]}: {reason}"
            )
        return completed


class Phase3PilotResourceOwner:
    """Own one project envelope and all three mode envelopes for a pilot."""

    def __init__(
        self,
        binding,
        *,
        pilot_id,
        command_runner=None,
        cgroup_root="/sys/fs/cgroup",
        hierarchy_factory=SystemdResourceHierarchy,
    ):
        validate_resource_envelope_binding(binding)
        self.binding = deepcopy(binding)
        self.pilot_id = pilot_id
        self.command_runner = command_runner or subprocess.run
        self.cgroup_root = cgroup_root
        self.hierarchy_factory = hierarchy_factory
        self.hierarchies = {}
        self._prepared = False

    def prepare(self, *, host_capacity=None):
        if self._prepared:
            return self.references()
        verify_host_capacity(self.binding, **(host_capacity or {}))
        try:
            for mode in _MODES:
                hierarchy = self.hierarchy_factory(
                    self.binding,
                    run_id=self.pilot_id,
                    mode=mode,
                    command_runner=self.command_runner,
                    cgroup_root=self.cgroup_root,
                )
                hierarchy.prepare(check_host=False)
                self.hierarchies[mode] = hierarchy
            self._prepared = True
            return self.references()
        except Exception:
            self.cleanup()
            raise

    def references(self):
        if not self._prepared:
            raise ResourceEnvelopeError("pilot resource owner is not prepared")
        return {
            mode: hierarchy.owner_reference()
            for mode, hierarchy in self.hierarchies.items()
        }

    def evidence(self):
        if not self._prepared:
            raise ResourceEnvelopeError("pilot resource owner is not prepared")
        project_identity = self.hierarchies[_MODES[0]]._identities["project"]
        project = build_resource_evidence(
            binding=self.binding,
            scope="project",
            identity={
                "systemd_unit": self.hierarchies[_MODES[0]].project_slice,
                "control_group": project_identity["ControlGroup"],
            },
            counters=read_resource_counters(
                project_identity["ControlGroup"],
                cgroup_root=self.cgroup_root,
            ),
        )
        modes = {}
        for mode, hierarchy in self.hierarchies.items():
            identity = hierarchy._identities["mode"]
            modes[mode] = build_resource_evidence(
                binding=self.binding,
                scope="mode",
                identity={
                    "systemd_unit": hierarchy.mode_slice,
                    "control_group": identity["ControlGroup"],
                    "mode": mode,
                },
                counters=read_resource_counters(
                    identity["ControlGroup"],
                    cgroup_root=self.cgroup_root,
                ),
            )
        return {"project": project, "modes": modes}

    def cleanup(self):
        results = {}
        modes = list(reversed(_MODES))
        for mode in modes:
            hierarchy = self.hierarchies.get(mode)
            if hierarchy is None:
                continue
            results[mode] = hierarchy.cleanup(
                include_project=(mode == modes[-1])
            )
        complete = all(
            result.get("cleanup_complete") is True
            for result in results.values()
        )
        self._prepared = False
        return {
            "cleanup_attempted": bool(results),
            "cleanup_complete": complete,
            "mode_cleanup": results,
        }


class ResourceUnitMonitor:
    """Capture cgroup evidence while a short-lived transient unit exists."""

    def __init__(
        self,
        hierarchy,
        unit,
        *,
        scope,
        evaluator=False,
        discovery_timeout_seconds=10.0,
        sample_interval_seconds=0.25,
    ):
        if scope not in {"control_plane", "workload", "evaluator"}:
            raise ResourceEnvelopeError("resource monitor scope is invalid")
        self.hierarchy = hierarchy
        self.unit = unit
        self.scope = scope
        self.evaluator = bool(evaluator)
        self.discovery_timeout_seconds = float(discovery_timeout_seconds)
        self.sample_interval_seconds = float(sample_interval_seconds)
        self._stop = threading.Event()
        self._thread = None
        self._discovery_complete = threading.Event()
        self._identity = None
        self._counters = None
        self._last_error = None
        self._ack_path = _resource_monitor_ack_path(unit)

    def start(self):
        if self._thread is not None:
            raise ResourceEnvelopeError("resource monitor already started")
        self._ack_path.unlink(missing_ok=True)
        self._thread = threading.Thread(
            target=self._observe,
            name=f"resource-monitor-{self.unit}",
            daemon=True,
        )
        self._thread.start()
        return self

    def finish(self, *, binding, timed_out=False, cleanup=None):
        if self._thread is None:
            raise ResourceEnvelopeError("resource monitor was not started")
        self._discovery_complete.wait(self.discovery_timeout_seconds)
        self._stop.set()
        self._thread.join(timeout=max(self.sample_interval_seconds + 1.0, 2.0))
        if self._thread.is_alive():
            raise ResourceEnvelopeUnavailable("resource monitor did not stop")
        if self._identity is None or self._counters is None:
            if self._last_error is not None:
                raise ResourceEnvelopeUnavailable(
                    f"transient resource unit was not observable: {self.unit}"
                ) from self._last_error
            raise ResourceEnvelopeUnavailable(
                f"transient resource unit was not observable: {self.unit}"
            )
        try:
            return build_resource_evidence(
                binding=binding,
                scope=self.scope,
                identity={
                    "systemd_unit": self.unit,
                    "control_group": self._identity["ControlGroup"],
                    "hierarchy": self.hierarchy.identity(),
                },
                counters=self._counters,
                timed_out=timed_out,
                cleanup=cleanup,
            )
        finally:
            self._ack_path.unlink(missing_ok=True)

    def cancel(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=max(self.sample_interval_seconds + 1.0, 2.0))
        self._ack_path.unlink(missing_ok=True)

    def _observe(self):
        try:
            deadline = time.monotonic() + self.discovery_timeout_seconds
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    if self.scope == "control_plane":
                        identity = self.hierarchy.verify_control_plane()
                    else:
                        identity = self.hierarchy.verify_leaf(
                            self.unit,
                            evaluator=self.evaluator,
                        )
                    self._identity = identity
                    break
                except ResourceEnvelopeError as exc:
                    self._last_error = exc
                    self._stop.wait(0.05)
        finally:
            self._discovery_complete.set()
        while self._identity is not None and not self._stop.is_set():
            self._sample()
            self._stop.wait(self.sample_interval_seconds)
        if self._identity is not None:
            self._sample()

    def _sample(self):
        counters = read_resource_counters(
            self._identity["ControlGroup"],
            cgroup_root=self.hierarchy.cgroup_root,
        )
        if _resource_counters_observed(counters):
            self._counters = counters
            _publish_resource_monitor_ack(self._ack_path)


def run_phase3_provider_free_resource_preflight(
    binding,
    *,
    pilot_id,
    command_runner=None,
    owner_factory=Phase3PilotResourceOwner,
    hierarchy_factory=SystemdResourceHierarchy,
    monitor_factory=ResourceUnitMonitor,
):
    """Exercise the complete resource hierarchy without invoking a provider."""

    validate_resource_envelope_binding(binding)
    runner = command_runner or subprocess.run
    owner = owner_factory(
        binding,
        pilot_id=pilot_id,
        command_runner=runner,
    )
    records = []
    aggregate = None
    cleanup = None
    references = None
    digest = hashlib.sha256(pilot_id.encode("utf-8")).hexdigest()[:16]
    probes = {
        "single_codex": (("workload", False), ("evaluator", True)),
        "agentteam_direct": (
            ("control_plane", False),
            ("workload", False),
            ("evaluator", True),
        ),
        "agentteam_full": (
            ("control_plane", False),
            ("workload", False),
            ("evaluator", True),
        ),
    }
    try:
        references = owner.prepare()
        for mode in _MODES:
            hierarchy = hierarchy_factory(
                binding,
                run_id=pilot_id,
                mode=mode,
                owner_reference=references[mode],
                command_runner=runner,
            )
            hierarchy.prepare(check_host=False)
            for scope, evaluator in probes[mode]:
                token = f"{mode}-{scope}".replace("_", "-")
                unit = f"agentteam-p3-{digest}-{token}-probe.service"
                if scope == "control_plane":
                    unit = hierarchy.control_scope
                    command = hierarchy.control_plane_command(["/usr/bin/true"])
                else:
                    command = hierarchy.leaf_command(
                        ["/usr/bin/true"],
                        unit=unit,
                        evaluator=evaluator,
                    )
                monitor = monitor_factory(
                    hierarchy,
                    unit,
                    scope=scope,
                    evaluator=evaluator,
                ).start()
                try:
                    completed = runner(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                        timeout=15,
                    )
                    evidence = monitor.finish(binding=binding)
                except Exception:
                    monitor.cancel()
                    hierarchy.stop_transient_unit(unit)
                    raise
                if completed.returncode != 0:
                    hierarchy.stop_transient_unit(unit)
                    raise ResourceEnvelopeUnavailable(
                        f"resource preflight probe failed: {mode}/{scope}"
                    )
                records.append(
                    {
                        "probe_id": f"{mode}:{scope}",
                        "mode": mode,
                        "scope": scope,
                        "returncode": completed.returncode,
                        "evidence": evidence,
                    }
                )
        aggregate = owner.evidence()
    finally:
        cleanup = owner.cleanup()
    if aggregate is None or cleanup.get("cleanup_complete") is not True:
        raise ResourceEnvelopeUnavailable(
            "resource preflight aggregate or cleanup is incomplete"
        )
    receipt = {
        "schema_version": RESOURCE_PREFLIGHT_SCHEMA_VERSION,
        "status": "passed",
        "provider_free": True,
        "pilot_id": pilot_id,
        "binding_sha256": canonical_resource_envelope_sha256(binding),
        "probe_records": records,
        "aggregate_evidence": aggregate,
        "cleanup": cleanup,
        "provider_reconciliation": {
            "provider_calls": 0,
            "model_invocations": 0,
            "scored_mode_executions": 0,
        },
    }
    receipt["receipt_sha256"] = _resource_preflight_sha256(receipt)
    return validate_phase3_resource_preflight_receipt(receipt)


def validate_phase3_resource_preflight_receipt(receipt):
    value = deepcopy(receipt)
    expected = {
        "schema_version",
        "status",
        "provider_free",
        "pilot_id",
        "binding_sha256",
        "probe_records",
        "aggregate_evidence",
        "cleanup",
        "provider_reconciliation",
        "receipt_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ResourceEnvelopeError("resource preflight receipt fields are invalid")
    cleanup = value["cleanup"]
    reconciliation = value["provider_reconciliation"]
    if (
        value["schema_version"] != RESOURCE_PREFLIGHT_SCHEMA_VERSION
        or value["status"] != "passed"
        or value["provider_free"] is not True
        or not isinstance(reconciliation, dict)
        or reconciliation
        != {
            "provider_calls": 0,
            "model_invocations": 0,
            "scored_mode_executions": 0,
        }
        or not isinstance(cleanup, dict)
        or cleanup.get("cleanup_complete") is not True
        or value["receipt_sha256"] != _resource_preflight_sha256(value)
    ):
        raise ResourceEnvelopeError("resource preflight receipt is invalid")
    if (
        not isinstance(value["pilot_id"], str)
        or not _SAFE_ID.fullmatch(value["pilot_id"])
        or value["binding_sha256"]
        != canonical_resource_envelope_sha256(
            approved_phase3_resource_envelope_binding()
        )
    ):
        raise ResourceEnvelopeError("resource preflight authority is invalid")
    expected_probes = [
        "single_codex:workload",
        "single_codex:evaluator",
        "agentteam_direct:control_plane",
        "agentteam_direct:workload",
        "agentteam_direct:evaluator",
        "agentteam_full:control_plane",
        "agentteam_full:workload",
        "agentteam_full:evaluator",
    ]
    if (
        not isinstance(value["probe_records"], list)
        or not all(isinstance(item, dict) for item in value["probe_records"])
        or [item.get("probe_id") for item in value["probe_records"]]
        != expected_probes
    ):
        raise ResourceEnvelopeError("resource preflight probe inventory is invalid")
    for item in value["probe_records"]:
        evidence = item.get("evidence")
        mode, scope = item["probe_id"].split(":", 1)
        if (
            set(item) != {"probe_id", "mode", "scope", "returncode", "evidence"}
            or item.get("returncode") != 0
            or item.get("mode") != mode
            or item.get("scope") != scope
        ):
            raise ResourceEnvelopeError("resource preflight probe evidence is invalid")
        validate_resource_evidence(
            evidence,
            expected_binding_sha256=value["binding_sha256"],
            expected_scope=scope,
        )
    aggregate = value["aggregate_evidence"]
    if (
        not isinstance(aggregate, dict)
        or set(aggregate) != {"project", "modes"}
        or not isinstance(aggregate.get("modes"), dict)
        or set(aggregate["modes"]) != set(_MODES)
        or not isinstance(cleanup.get("mode_cleanup"), dict)
        or set(cleanup["mode_cleanup"]) != set(_MODES)
        or not all(
            isinstance(item, dict)
            for item in cleanup["mode_cleanup"].values()
        )
        or any(
            item.get("cleanup_complete") is not True
            for item in cleanup["mode_cleanup"].values()
        )
    ):
        raise ResourceEnvelopeError("resource preflight aggregate evidence is invalid")
    validate_resource_evidence(
        aggregate["project"],
        expected_binding_sha256=value["binding_sha256"],
        expected_scope="project",
    )
    for evidence in aggregate["modes"].values():
        validate_resource_evidence(
            evidence,
            expected_binding_sha256=value["binding_sha256"],
            expected_scope="mode",
        )
    return value


def publish_phase3_resource_preflight_receipt(path, receipt):
    """Persist one canonical real-host preflight receipt immutably."""

    from .experiment_contract import publish_immutable_json

    value = validate_phase3_resource_preflight_receipt(receipt)
    return publish_immutable_json(
        path,
        value,
        label="Phase 3 resource preflight receipt",
    )


def _resource_preflight_sha256(receipt):
    body = deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _resource_monitor_ack_path(unit):
    if not isinstance(unit, str) or not unit.endswith(".service"):
        raise ResourceEnvelopeError("resource monitor unit is invalid")
    root = Path("/tmp") / f"agentteam-resource-monitor-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.lstat()
    if root.is_symlink() or not root.is_dir() or metadata.st_uid != os.getuid():
        raise ResourceEnvelopeUnavailable("resource monitor directory is unsafe")
    os.chmod(root, 0o700)
    digest = hashlib.sha256(unit.encode("utf-8")).hexdigest()
    return root / f"{digest}.ack"


def _publish_resource_monitor_ack(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if not _valid_resource_monitor_ack(path):
            raise ResourceEnvelopeUnavailable(
                "resource monitor acknowledgement is unsafe"
            )
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _valid_resource_monitor_ack(path):
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _resource_wrapped_command(command, *, unit):
    return [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "_acknowledged_exec",
        str(_resource_monitor_ack_path(unit)),
        "--",
        *list(command),
    ]


def _acknowledged_exec(argv):
    if len(argv) < 2:
        return 2
    command = list(argv)
    ack_path = Path(command.pop(0))
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        return 2
    returncode = subprocess.run(command, check=False).returncode
    deadline = time.monotonic() + RESOURCE_MONITOR_ACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _valid_resource_monitor_ack(ack_path):
            return returncode
        time.sleep(0.05)
    return 125


def read_resource_counters(control_group, *, cgroup_root="/sys/fs/cgroup"):
    """Read bounded cgroup-v2 counters for one complete descendant tree."""

    if not isinstance(control_group, str) or not control_group.startswith("/"):
        raise ResourceEnvelopeError("cgroup identity is invalid")
    root = Path(cgroup_root) / control_group.lstrip("/")
    return {
        "control_group": control_group,
        "cpu": _read_key_value_file(root / "cpu.stat"),
        "memory": {
            "current": _read_single_counter(root / "memory.current"),
            "peak": _read_single_counter(root / "memory.peak"),
            "events": _read_key_value_file(root / "memory.events"),
        },
        "pids": {
            "current": _read_single_counter(root / "pids.current"),
            "peak": _read_single_counter(root / "pids.peak"),
            "events": _read_key_value_file(root / "pids.events"),
        },
        "cgroup": _read_key_value_file(root / "cgroup.events"),
        "observed_at": _utc_now(),
    }


def _resource_counters_observed(counters):
    return any(
        bool(value)
        for value in (
            counters.get("cpu"),
            (counters.get("memory") or {}).get("events"),
            (counters.get("pids") or {}).get("events"),
            counters.get("cgroup"),
        )
    )


def classify_resource_exhaustion(counters, *, timed_out=False):
    """Return structured resource exhaustion independent of model quality."""

    memory = ((counters or {}).get("memory") or {}).get("events") or {}
    pids = ((counters or {}).get("pids") or {}).get("events") or {}
    cpu = (counters or {}).get("cpu") or {}
    reasons = []
    if int(memory.get("oom", 0) or 0) > 0 or int(
        memory.get("oom_kill", 0) or 0
    ) > 0:
        reasons.append("memory_oom")
    if int(memory.get("max", 0) or 0) > 0:
        reasons.append("memory_max")
    if int(pids.get("max", 0) or 0) > 0:
        reasons.append("pids_max")
    pressure_observed = (
        int(cpu.get("nr_throttled", 0) or 0) > 0
        or int(memory.get("high", 0) or 0) > 0
        or bool(reasons)
    )
    if timed_out and pressure_observed:
        reasons.append("resource_driven_timeout")
    reasons = sorted(set(reasons))
    return {
        "classification": (
            RESOURCE_FAILURE_CLASSIFICATION if reasons else None
        ),
        "resource_exhausted": bool(reasons),
        "reasons": reasons,
        "model_quality_failure": False if reasons else None,
    }


def build_resource_evidence(
    *,
    binding,
    scope,
    identity,
    counters,
    timed_out=False,
    cleanup=None,
):
    if scope not in {"project", "mode", "control_plane", "workload", "evaluator"}:
        raise ResourceEnvelopeError("resource evidence scope is invalid")
    validate_resource_envelope_binding(binding)
    if not isinstance(identity, dict) or not identity:
        raise ResourceEnvelopeError("resource evidence identity is invalid")
    if not isinstance(counters, dict):
        raise ResourceEnvelopeError("resource evidence counters are invalid")
    return {
        "resource_evidence_schema_version": RESOURCE_EVIDENCE_SCHEMA_VERSION,
        "binding_sha256": canonical_resource_envelope_sha256(binding),
        "scope": scope,
        "identity": deepcopy(identity),
        "counters": deepcopy(counters),
        "outcome": classify_resource_exhaustion(
            counters,
            timed_out=timed_out,
        ),
        "timeout": {"timed_out": bool(timed_out)},
        "cleanup": deepcopy(cleanup),
    }


def validate_resource_evidence(
    evidence,
    *,
    expected_binding_sha256=None,
    expected_scope=None,
):
    """Validate one resource record and its derived outcome."""

    value = deepcopy(evidence)
    expected_fields = {
        "resource_evidence_schema_version",
        "binding_sha256",
        "scope",
        "identity",
        "counters",
        "outcome",
        "timeout",
        "cleanup",
    }
    scopes = {"project", "mode", "control_plane", "workload", "evaluator"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value["resource_evidence_schema_version"]
        != RESOURCE_EVIDENCE_SCHEMA_VERSION
        or not isinstance(value["binding_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["binding_sha256"]) is None
        or not isinstance(value["scope"], str)
        or value["scope"] not in scopes
        or not isinstance(value["identity"], dict)
        or not value["identity"]
        or not isinstance(value["counters"], dict)
        or not isinstance(value["timeout"], dict)
        or set(value["timeout"]) != {"timed_out"}
        or not isinstance(value["timeout"]["timed_out"], bool)
        or (
            value["cleanup"] is not None
            and not isinstance(value["cleanup"], dict)
        )
    ):
        raise ResourceEnvelopeError("resource evidence is invalid")
    if any(
        name in value["counters"]
        and not isinstance(value["counters"][name], dict)
        for name in ("cpu", "memory", "pids", "cgroup")
    ):
        raise ResourceEnvelopeError("resource evidence counters are invalid")
    if (
        expected_binding_sha256 is not None
        and value["binding_sha256"] != expected_binding_sha256
    ):
        raise ResourceEnvelopeError("resource evidence binding is invalid")
    if expected_scope is not None and value["scope"] != expected_scope:
        raise ResourceEnvelopeError("resource evidence scope is invalid")
    try:
        expected_outcome = classify_resource_exhaustion(
            value["counters"],
            timed_out=value["timeout"]["timed_out"],
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ResourceEnvelopeError("resource evidence counters are invalid") from exc
    if value["outcome"] != expected_outcome:
        raise ResourceEnvelopeError("resource evidence outcome is invalid")
    return value


def _validate_envelope_values(envelope):
    if not isinstance(envelope, dict) or set(envelope) != set(_ENVELOPE_FIELDS):
        raise ResourceEnvelopeError("resource envelope fields are invalid")
    if any(
        not isinstance(envelope[name], int)
        or isinstance(envelope[name], bool)
        for name in _ENVELOPE_FIELDS
    ):
        raise ResourceEnvelopeError("resource envelope values must be integers")
    if (
        envelope["cpu_quota"] <= 0
        or envelope["memory_high_bytes"] <= 0
        or envelope["memory_max_bytes"] < envelope["memory_high_bytes"]
        or envelope["tasks_max"] <= 0
        or envelope["memory_swap_max_bytes"] != 0
    ):
        raise ResourceEnvelopeError("resource envelope values are invalid")


def _host_memory_total_bytes():
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ResourceEnvelopeUnavailable("host memory capacity is unavailable") from exc
    for line in lines:
        key, separator, value = line.partition(":")
        if key == "MemTotal" and separator:
            fields = value.strip().split()
            if len(fields) == 2 and fields[1] == "kB" and fields[0].isdigit():
                return int(fields[0]) * 1024
    raise ResourceEnvelopeUnavailable("host memory capacity is unavailable")


def _parse_show(payload):
    result = {}
    for line in str(payload or "").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            result[name] = value
    return result


def _parse_systemd_integer(value):
    if value == "infinity":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ResourceEnvelopeUnavailable(
            f"invalid systemd integer readback: {value!r}"
        ) from exc


def _parse_systemd_time_usec(value):
    units = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
        "min": 60_000_000,
    }
    text = str(value or "")
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)]
            try:
                return int(float(number) * units[suffix])
            except ValueError as exc:
                raise ResourceEnvelopeUnavailable(
                    f"invalid systemd CPU quota readback: {value!r}"
                ) from exc
    try:
        return int(text)
    except ValueError as exc:
        raise ResourceEnvelopeUnavailable(
            f"invalid systemd CPU quota readback: {value!r}"
        ) from exc


def _read_single_counter(path):
    try:
        value = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_key_value_file(path):
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return {}
    result = {}
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            result[fields[0]] = int(fields[1])
        except ValueError:
            continue
    return result


def _cgroup_populated(control_group, *, cgroup_root):
    if not isinstance(control_group, str) or not control_group.startswith("/"):
        return None
    events = _read_key_value_file(
        Path(cgroup_root) / control_group.lstrip("/") / "cgroup.events"
    )
    populated = events.get("populated")
    return bool(populated) if populated in {0, 1} else None


def _cgroup_cleanup_state(control_group, *, cgroup_root):
    if not isinstance(control_group, str) or not control_group.startswith("/"):
        return "unknown"
    events_path = (
        Path(cgroup_root) / control_group.lstrip("/") / "cgroup.events"
    )
    if not events_path.exists():
        return "removed"
    populated = _cgroup_populated(control_group, cgroup_root=cgroup_root)
    if populated is False:
        return "drained"
    if populated is True:
        return "populated"
    return "unknown"


def _utc_now():
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    if sys.argv[1:2] == ["_acknowledged_exec"]:
        raise SystemExit(_acknowledged_exec(sys.argv[2:]))
    raise SystemExit(2)
