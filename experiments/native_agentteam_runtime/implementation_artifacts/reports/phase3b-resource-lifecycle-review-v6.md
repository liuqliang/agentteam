# Phase 3B resource lifecycle review v6

- Decision: `DEC-P3B-v6-resource-lifecycle-repair`
- Evidence level: `L2`
- Review disposition: provider-free resource mechanism repaired and verified
- Live status: `P3-LIVE` remains unauthorized

## Why v6 was required

The v5 provider-free tests passed, but operator review and a real user-systemd
preflight found that the implementation was not ready for a scored run:

- each model invocation could create and clean shared project/mode slices;
- the standard bound-mode entry did not require a pilot-owned hierarchy;
- control-plane and evaluator code attempted cgroup readback after
  `systemd-run --wait` had removed the transient cgroup;
- project and mode aggregate evidence had no owner API;
- `TasksMax=256` left only narrow headroom above the locally observed Codex
  process-tree range of 234-240 tasks;
- resource evidence was not digest-bound into the sealed result bundle.

The v5 report remains historical evidence of the checks that ran. This review
supersedes only its readiness interpretation.

## Implemented repair

Resource envelope v2 keeps the approved CPU and memory ceilings and raises PID
capacity to 384 for each workload/control slot and 768 for AgentTeam mode
parents. A `Phase3PilotResourceOwner` now owns one project slice and all three
mode slices. Workers, control planes, and evaluators receive immutable owner
references, revalidate parent identity and limits, and never clean shared
parents.

Short-lived transient services use an acknowledgement handshake. The monitor
verifies the unit and cgroup hierarchy and samples cgroup-v2 counters while the
unit exists; only then may the wrapper exit. The model-worker gated supervisor
uses the same ordering. Missing identity or evidence fails closed.

Each invocation retains `resource.json`, the evaluator retains
`common-evaluation.json.resources.json`, and AgentTeam modes retain a control
plane resource record. A compact `phase3-resource-evidence.json` indexes every
required record by path and SHA-256. Its own path and digest are optional Phase
3 fields in the sealed result bundle. Resource-bound execution refuses to seal
when any required invocation, evaluator, or control-plane record is absent.

## Real host preflight

The final preflight used the current user systemd manager, a pilot owner, an
`agentteam_direct` borrower, and a control-plane transient service whose
payload was `/usr/bin/sleep 0.2`.

Observed result:

```json
{"cleanup_complete":true,"elapsed_seconds":0.306,"returncode":0,"usage_usec":32952}
```

The verified control group was nested under the run-specific project and
`agentteam_direct` mode slices. The command returned its original exit code,
resource counters were present, and owner cleanup reported complete.

## Verification

Focused resource/model/experiment regression:

```text
Ran 207 tests in 79.886s
OK (skipped=6)
```

Provider-free integration lane:

```text
Ran 707 tests in 79.543s
OK (skipped=2)
```

Additional focused tests cover owner-reference drift, borrower cleanup,
project/mode aggregate evidence, short-lived unit monitoring, resource-sidecar
contract binding, pre-snapshot rejection without an owner reference, and
complete resource-evidence indexing. `git diff --check` also passes.

## Boundary

This work completes the reusable P3B-01B enforcement mechanism and keeps
historical Phase 2 execution compatible when no resource sidecar is supplied.
It does not implement the P3B-03 aggregate pilot scheduler, freeze selected
instance taskpacks, authorize a provider call, merge source, push, or activate
a release. P3B-03 must create one pilot owner before the first mode, pass the
corresponding owner reference to every mode execution, persist owner aggregate
evidence before cleanup, and clean the hierarchy in a `finally` boundary.
