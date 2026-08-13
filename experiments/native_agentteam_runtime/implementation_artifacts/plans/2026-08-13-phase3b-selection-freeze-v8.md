# Phase 3B selection freeze v8

- Decision: `DEC-P3B-multi-instance-pilot`
- Decision authority: `L3`, revision `1`
- Status: approved for provider-free selection freeze
- Live boundary: no provider invocation and no `P3-LIVE` authorization

## Decision

Persist the already approved pilot parameters and the production replay of the
fixed SWE-EVO Arrow artifact as one immutable selection bundle. The publication
must contain decisions, trusted complexity projection, gold-blind routing
manifest, selection authority, and a completion receipt. The completion receipt
is published last and binds every preceding authority.

The historical proposal digest is
`622be205e6ab9ad9ec19a3551329fc3c6f775541826e982c0c54352007156c10`
at accepted baseline commit `45d75376759f375eb32a29eb830e64d103ed1896`.
The approving v4 review's canonical JSON digest is
`d290d1b108fc2f08a56e841701b63035ed940f244a713de6d5bf99c55d0b4299`.
Later resource-only edits to the current Markdown proposal do not rewrite the
historical decision input.

## Acceptance

- The production Arrow artifact hash, 48-row populations, and three selected
  IDs reproduce the approved preview exactly.
- Projection, routing, selection, decision input, and selection-authority
  digests are fixed production constants; fixture projections cannot be
  published as production authority.
- Reopening the bundle revalidates and replays the complete authority chain.
- Repeated publication is byte-identical and reports `created=false`.
- Conflicting files, symlinks, rehashed decision drift, missing authorities,
  or digest drift fail closed before a completion receipt is accepted.
- Provider calls and scored executions remain zero.

## Boundary

This freezes P3B-01C selection authority only. It does not invent or approve
the three instance-specific goals, target commits, evaluator images, acceptance
commands, direct taskpacks, or preregistrations required by P3B-02.
