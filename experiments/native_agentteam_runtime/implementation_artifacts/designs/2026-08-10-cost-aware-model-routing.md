# Cost-Aware Model Routing

Status: implemented default policy for ordinary Codex taskpacks.

## Decision

AgentTeam treats model tier and reasoning effort as independent controls. The
built-in `gpt-5.6-cost-aware.v1` policy selects a profile per dispatch from the
worker role, task risk, and retry number. It does not ask the worker to choose
its own model.

New ordinary Codex taskpacks that do not pin `--codex-model` use adaptive
routing. Supplying `--codex-model` selects fixed mode; `--reasoning-profile`
freezes its reasoning effort and defaults to `high` when omitted. Historical
frozen taskpacks without `model_routing_policy` retain their previous runtime
behavior.

## Default Matrix

| Work | Initial profile |
|---|---|
| Deterministic scheduling, leases, Git integration, command execution | no model |
| Taskpack and follow-up authoring | `gpt-5.6-terra`, `medium` |
| Repository map | `gpt-5.6-luna`, `medium` |
| Context building and planning | `gpt-5.6-terra`, `medium` |
| L0 implementation | `gpt-5.6-luna`, `medium` |
| L1 implementation | `gpt-5.6-terra`, `medium` |
| L2/L3 implementation | `gpt-5.6-sol`, `high` |
| Ordinary review or repair | `gpt-5.6-terra`, `medium` |
| Semantic feedback or semantic architecture | `gpt-5.6-sol`, `high` |

Schema-driven formatting and extraction should remain deterministic when a
script can produce the result. Luna is a fallback for semantic extraction, not
a replacement for an available parser.

## Retry Decision And Escalation

An attempt number is not evidence that a stronger model is needed. The
scheduler-owned deterministic retry controller classifies the structured
failure before another dispatch. Its compact `agentteam_retry_decision.v1`
record is retained in the attempt result, canonical event stream, replayed
state, recovery event, and next `retry_handoff`.

| Failure evidence | Action | Model change |
|---|---|---|
| timeout or transient provider failure | `retry_same_model` | none |
| invalid/missing model output, or integration verification failure with a known-clean baseline | `escalate_model` | one level |
| integration verification failure without clean-baseline evidence | `retry_same_model` | none |
| permission, launch, dependency, or host failure | `repair_environment` | no automatic retry |
| integration apply failure | `repair_integration` | none |
| scope or task-contract failure | `replan_task` or `review_required` | none |
| cancellation or non-retryable rejection | `block` | none |

Only `escalate_model` may change the model profile:

```text
Luna -> Terra medium
Terra -> Sol high
Sol high -> Sol xhigh
```

Further attempts keep the same bounded escalation level. Routing does not
modify task authority, risk, scope, acceptance criteria, verification, or
merge policy. A failed validation cannot be hidden by changing models.

Ambiguous failures return `review_required` and stop automatic retry. This
version does not let the implementation worker review its own failure. A
future read-only reviewer dispatch may resolve that state, but scheduler
validation remains the sole authority that can publish the resulting retry
decision.

## Runtime Binding

The scheduler resolves the route before dispatch and publishes the complete
selection in the dispatch authority. A long-lived worker creates an
attempt-local Codex adapter from that selection. This avoids binding one model
to the worker process for its whole lifetime and prevents the worker from
silently upgrading itself.

Every selection records:

- policy and schema version;
- mode (`adaptive` or `fixed`);
- role and task risk;
- attempt number and the scheduler retry-decision identity, when applicable;
- selected model and reasoning effort;
- escalation level and deterministic selection reason.

The record is retained in the dispatch, inflight state, runtime-session event,
and model-invocation lifecycle context. It is structured evidence and does not
create a prose trace file.

## Benchmark Boundary

Benchmark runs must use fixed mode or an independently frozen experiment model
contract. Adaptive routing is production behavior and must not silently enter
a controlled comparison. The benchmark authority remains responsible for
holding model, reasoning effort, service configuration, budgets, and mode order
constant where the experiment requires them to be constant.

## Failure Boundary

Model availability is verified by the provider at invocation time. An
unsupported or unavailable low-cost profile is an explicit failed attempt; it
may follow the bounded retry escalation path, but AgentTeam must not rewrite
the original invocation record or report the fallback as the initial route.
