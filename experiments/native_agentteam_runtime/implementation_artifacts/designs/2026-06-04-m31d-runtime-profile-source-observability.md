# M31d Runtime Profile Source Observability Implementation Notes

## Goal

Make runtime session records explain where their effective runtime adapter
configuration came from.

## Implemented Behavior

`runtime_session_started` events now include `runtime_profile_source`. Event
replay and the SQLite state index preserve the same value, so the existing
`sessions` observability view can expose it without a new view.

Recorded source values are:

- `explicit_runtime_adapter`;
- `runtime_adapter_factory`;
- `agent_runtime_profile`;
- `role_runtime_profile`;
- `runtime_profile_defaults`;
- `external_mailbox_adapter`;
- `default_fake`.

## Boundary

M31d does not change profile precedence or adapter construction. It only records
the source selected by the existing precedence chain.

## Validation

Regression coverage verifies agent-level profiles, role-level profiles, and
explicit runtime adapters through the scheduler state index.
