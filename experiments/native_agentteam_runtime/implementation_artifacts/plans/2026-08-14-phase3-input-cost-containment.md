# Phase 3 input-cost containment

Status: completed provider-free

## Decision

Bind scored Phase 3 Codex invocations to a 4,000-token per-tool-output context
limit and disable Codex web search. Apply the same frozen policy to
`single_codex`, the preregistered direct worker, and both the full-mode author
and worker. Keep provider API access, shell execution, candidate patches,
controller verification, and the official evaluator unchanged.

## Evidence

The retained Requests calibration consumed 2,122,262 tokens. JSONL profiling
shows that failed pytest commands returned 243,125 to 331,745 characters to
workers. Those outputs then remained in later model requests. The direct mode
also performed six external web searches even though the frozen repository and
public task were sufficient authority. This is model-context amplification,
not decision-trace or SQLite write cost.

Codex 0.147.0 accepts `tool_output_token_limit` as a context-manager hard limit
and `web_search="disabled"` as a tool policy. Strict local configuration
validation accepts both settings. AgentTeam therefore does not need to replace
Codex's exec tool or parse arbitrary shell output itself.

## Contract

- New scored protocols bind `environment.tool_output_token_limit=4000` and
  `environment.web_search_policy="disabled"`.
- These values become part of the immutable model policy, mode authority, and
  every provider launch registration.
- Every registered Codex command must contain exactly the bound values. A
  missing, duplicate, or different override fails before provider execution.
- The settings are injected into single mode, full-mode taskpack authoring, and
  all AgentTeam benchmark workers. Ordinary non-benchmark AgentTeam runs keep
  their existing configuration.
- Existing protocols and sealed historical artifacts without these fields
  remain readable and replayable; they cannot silently claim the new policy.
- Disabling web search does not disable provider network access. The sandbox's
  `provider_access` policy remains necessary for the model API.
- The tool-output limit bounds model-visible context only. Controller-owned
  acceptance verification and official SWE-EVO evaluation retain their
  existing bounded evidence paths and do not inherit the Codex context limit.

## Acceptance

1. Schema and authority validation accept both legacy and extended policies,
   while rejecting partial extended policies.
2. Command validation rejects missing, duplicate, or mismatched policy
   overrides before a provider launch.
3. Single, direct, full-author, and full-worker provider-free tests observe the
   same two Codex overrides.
4. A newly materialized protocol and frozen bundle contain the exact policy.
5. Focused Phase 3 and complete provider-free suites pass without a model call.

## Execution Gate

Do not repeat the Requests scored calibration merely to exercise the new
mechanism. First complete provider-free validation and re-profile the retained
JSONL as the baseline. The next real call must use a new instance or a
preregistered cost-containment repetition whose decision states the expected
information gain and token ceiling.

## Implementation Evidence

- Runtime commit: `d2f22075c8858faddc4223438c35b2a9c9b886bc`
- Runtime release: `phase3-calibration-d2f2207`
- Frozen bundle SHA-256:
  `a54007b0620f8e0db8930937c2f951426cc6359ee394213588c1e297c4c54a79`
- Protocol artifact SHA-256:
  `672df3aea7ef9523248d6ed611d77f5e1dd0b1389e40eeee89d9fb4031b53d98`
- Provider-free suite: `1,198` tests passed, `7` skipped.
- The zero-execution initialization at
  `/tmp/agentteam-phase3-calibration-preflight-d2f2207` materialized and
  validated the frozen policy with no provider evidence files and zero
  reported usage.
- Resource cleanup completed for `single_codex`, `agentteam_direct`, and
  `agentteam_full`; all experiment-owned systemd slices were removed.

This proves policy propagation and pre-launch rejection behavior. It does not
yet prove a reduction in provider-reported tokens. That claim requires a
subsequent scored execution under a separately frozen execution decision.
