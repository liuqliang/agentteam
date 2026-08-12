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
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


RESOURCE_ENVELOPE_SCHEMA_VERSION = "phase3_resource_envelope.v1"
RESOURCE_EVIDENCE_SCHEMA_VERSION = "phase3_resource_evidence.v1"
RESOURCE_FAILURE_CLASSIFICATION = "resource_limit_exhausted"
RESOURCE_FAIRNESS_POLICY = (
    "equal_workload_slots_with_metered_agentteam_control_plane_allowance"
)
GIB = 1024**3

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
            "common_workload_slot": _envelope(4, 8 * GIB, 12 * GIB, 256),
            "single_codex_mode": _envelope(4, 8 * GIB, 12 * GIB, 256),
            "agentteam_control_plane_allowance": _envelope(
                4, 8 * GIB, 12 * GIB, 256
            ),
            "agentteam_mode": _envelope(8, 16 * GIB, 24 * GIB, 512),
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
    returned.  ``attach_control_plane`` moves the complete controller process
    tree into a bounded scope; workload and evaluator transient services are
    then placed below the same mode slice.
    """

    def __init__(
        self,
        binding,
        *,
        run_id,
        mode,
        command_runner=None,
        cgroup_root="/sys/fs/cgroup",
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
            f"agentteam-p3-{digest}-{mode_token}-control.scope"
        )
        self._prepared = False
        self._control_attached = False
        self._identities = {}

    def prepare(self, *, check_host=True, host_capacity=None):
        try:
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

    def attach_control_plane(self, pid=None):
        if not self._prepared:
            raise ResourceEnvelopeError("resource parents must be prepared first")
        envelope = mode_envelopes(self.binding, self.mode)[
            "control_plane_envelope"
        ]
        if envelope is None:
            return None
        pid = os.getpid() if pid is None else pid
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ResourceEnvelopeError("control-plane pid is invalid")
        command = [
            "systemd-run",
            "--user",
            "--quiet",
            "--scope",
            f"--unit={self.control_scope}",
            f"--slice={self.mode_slice}",
            f"--pid={pid}",
        ]
        for name, value in systemd_properties(envelope).items():
            command.append(f"--property={name}={value}")
        self._checked(command)
        self._identities["control_plane"] = self._verify_unit(
            self.control_scope,
            envelope,
        )
        control_group = self._identities["control_plane"]["ControlGroup"]
        mode_group = self._identities["mode"]["ControlGroup"]
        if not control_group.startswith(mode_group.rstrip("/") + "/"):
            raise ResourceEnvelopeUnavailable(
                "control-plane cgroup escaped the verified mode cgroup"
            )
        self._control_attached = True
        return deepcopy(self._identities["control_plane"])

    def leaf_arguments(self, *, evaluator=False):
        if not self._prepared:
            raise ResourceEnvelopeError("resource parents must be prepared first")
        key = "evaluator_envelope" if evaluator else "workload_envelope"
        envelope = mode_envelopes(self.binding, self.mode)[key]
        arguments = [f"--slice={self.mode_slice}"]
        for name, value in systemd_properties(envelope).items():
            arguments.append(f"--property={name}={value}")
        return arguments

    def verify_leaf(self, unit):
        key = "workload_envelope"
        envelope = mode_envelopes(self.binding, self.mode)[key]
        identity = self._verify_unit(unit, envelope)
        if identity["ControlGroup"].rsplit("/", 1)[0] != self._identities[
            "mode"
        ]["ControlGroup"]:
            raise ResourceEnvelopeUnavailable(
                "workload leaf escaped the verified mode cgroup"
            )
        return identity

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
        }

    def cleanup(self):
        targets = []
        if self._control_attached:
            targets.append(self.control_scope)
        targets.extend((self.mode_slice, self.project_slice))
        stopped = []
        for unit in targets:
            completed = self._checked(
                ["systemctl", "--user", "stop", unit],
                ignore_errors=True,
            )
            stopped.append({"unit": unit, "returncode": completed.returncode})
        population = {}
        for label, identity in self._identities.items():
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
        }

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
