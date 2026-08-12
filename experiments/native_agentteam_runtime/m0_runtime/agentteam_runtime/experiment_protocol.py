"""Additive Phase 3 resource bindings for immutable experiment protocols."""

from __future__ import annotations

from copy import deepcopy

from .resource_envelope import (
    ResourceEnvelopeError,
    canonical_resource_envelope_sha256,
    envelope_for,
    validate_resource_envelope_binding,
)


def validate_protocol_resource_envelope(
    protocol,
    resource_envelope_binding=None,
    *,
    require_binding=False,
):
    """Validate an optional sidecar binding without rewriting protocol v1.

    ``None`` is accepted for historical protocols unless the Phase 3 caller
    explicitly requires enforcement.  When present, the common workload slot
    must agree with the preregistered shared CPU and hard-memory limits.
    """

    if resource_envelope_binding is None:
        if require_binding:
            raise ResourceEnvelopeError(
                "Phase 3 execution requires a resource envelope binding"
            )
        return None
    validate_resource_envelope_binding(resource_envelope_binding)
    if not isinstance(protocol, dict):
        raise ResourceEnvelopeError("experiment protocol must be an object")
    environment = protocol.get("environment")
    if not isinstance(environment, dict):
        raise ResourceEnvelopeError(
            "experiment protocol environment is unavailable"
        )
    workload = envelope_for(
        resource_envelope_binding,
        "common_workload_slot",
    )
    if environment.get("cpu_limit") != workload["cpu_quota"]:
        raise ResourceEnvelopeError(
            "protocol cpu_limit differs from the common workload slot"
        )
    if environment.get("memory_limit_bytes") != workload["memory_max_bytes"]:
        raise ResourceEnvelopeError(
            "protocol memory_limit_bytes differs from the common workload slot"
        )
    return {
        "binding": deepcopy(resource_envelope_binding),
        "binding_sha256": canonical_resource_envelope_sha256(
            resource_envelope_binding
        ),
    }


def protocol_resource_envelope_reference(
    protocol,
    resource_envelope_binding=None,
    *,
    require_binding=False,
):
    """Return the digest reference carried by Phase 3 launch authority."""

    validated = validate_protocol_resource_envelope(
        protocol,
        resource_envelope_binding,
        require_binding=require_binding,
    )
    if validated is None:
        return None
    return {
        "schema_version": "experiment_resource_envelope_reference.v1",
        "resource_envelope_schema_version": validated["binding"][
            "schema_version"
        ],
        "resource_envelope_sha256": validated["binding_sha256"],
    }
