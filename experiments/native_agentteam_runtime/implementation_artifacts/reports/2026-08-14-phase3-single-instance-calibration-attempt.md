# Phase 3 Single-Instance Calibration Attempt

## Status

The first authorized execution attempt is invalid for the planned three-mode
comparison and is retained as infrastructure-failure evidence. It completed
`single_codex`, reached one terminal `agentteam_direct` provider call, and did
not launch `agentteam_full`. No comparative quality or efficiency claim is
allowed from this attempt.

## Frozen Identity

- Instance: `psf__requests_v2.12.2_v2.12.3`
- Source commit: `ca15d4808734c86801c8f3d80c9152c35a163dc3`
- Runtime commit: `42bc51d6c46474730d5603268d8037769b26a35b`
- Bundle SHA-256:
  `f1213fe75965a5e039d072a6e8fb191da1ec326ddde5a40580c3ebe918616074`
- Model and reasoning profile: `gpt-5.6-sol`, `high`
- Valid attempt root:
  `/tmp/agentteam-phase3-calibration-run-requests-3738-v2`

## Observed Cost

Provider-reported total tokens equal input plus output; reasoning is a subset
of output and is not added again.

| Mode | Input | Cached input | Uncached input | Output | Reasoning | Total | Provider wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_codex` | 412,040 | 354,304 | 57,736 | 5,946 | 1,681 | 417,986 | 155 s |
| `agentteam_direct` | 2,313,482 | 2,162,176 | 151,306 | 11,287 | 3,327 | 2,324,769 | 313 s |
| retained attempt total | 2,725,522 | 2,516,480 | 209,042 | 17,233 | 5,008 | 2,742,755 | 468 s |

An earlier discarded launch consumed another 324,023 tokens before a public
verification executable-binding defect stopped it. That cost is
infrastructure waste and is not mixed into the table above. Total provider
cost spent across both attempts was therefore 3,066,778 tokens.

The frozen protocol budget was 1,500,000 total tokens. The retained attempt
exceeded it by 1,242,755 tokens. `agentteam_full` was not launched after this
was observed.

## Quality Evidence

`single_codex` produced a patch that passed the public assertion but did not
resolve the official instance: FAIL_TO_PASS was 0/4 and PASS_TO_PASS was
104/109. `agentteam_direct` produced the expected source condition and focused
regression tests, but the mode could not be sealed or officially evaluated.
Its quality outcome is therefore unknown, not failed or passed.

## Infrastructure Findings

1. The first direct-mode control-plane service failed and remained as a failed
   transient systemd unit. Reusing the stable unit name prevented clean
   relaunch and made resource readback fail.
2. While the resource parent was unavailable, the resident worker restarted
   148 times because experiment launches had no worker restart ceiling.
3. Every failed prelaunch allocated an `INV-*` directory before durable
   `started.json` publication. The 148 empty directories made invocation-set
   sealing impossible even though the one real provider call had complete
   terminal usage.
4. Failure finalization swallowed its secondary exception, reducing the
   operator-visible error to `terminal_ready`/registry symptoms.
5. The cost profiler previously ignored an unsealed run without an invocation
   set reference, hiding 2,324,769 reported tokens.

## Repairs

- Discard an invocation allocation after any exception that occurs before its
  durable start record.
- Limit experiment worker restarts to three while retaining ordinary runtime
  defaults outside experiments.
- Stop and reset a prior failed control-plane transient unit before relaunch.
- Publish bounded failure-finalization diagnostics with the secondary error.
- Profile complete durable terminals from an unsealed run as explicitly
  labelled infrastructure waste, while reporting prelaunch allocation counts
  as evidence gaps.

Focused tests, the 194-test Phase 3 harness, and the 394-test taskpack suite
pass after these repairs. A new runtime release and provider-free live
preflight are required before another scored calibration attempt.

