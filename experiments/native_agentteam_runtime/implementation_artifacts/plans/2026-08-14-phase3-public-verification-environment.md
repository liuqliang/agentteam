# Phase 3 public verification environment

## Decision

Bind every Phase 3 instance to a provider-visible, read-only Python environment
extracted from that instance's digest-pinned official image. The worker,
taskpack integration verification, and common visible evaluator must execute the
same public acceptance command with that Python environment. The official
evaluator remains a separate controller-only process and retains the hidden test
patch, benchmark Arrow data, and Podman socket.

## Problem

The current protocol resolves `pytest` to the AgentTeam host Python. That is not
equivalent to the Python and dependencies in the official SWE-EVO image. It made
Requests 2.4.0 fail during host collection and allowed a provider run to finish
without meaningful common verification. Running the complete public suite in
the official image is also unsuitable for the first pilot because old network
tests are slow and unstable.

## Contract

- A public-environment authority binds the instance ID, digest-pinned image,
  extracted dependency-tree digest, Python executable digest and version, and a
  fixed namespace target.
- Only `/opt/miniconda3/envs/testbed` is extracted. The candidate repository,
  hidden test patch, benchmark dataset, evaluator harness, and container socket
  are excluded.
- The dependency tree is mounted read-only. Large-tree identity is opt-in and
  bounded to 20,000 entries and 512 MiB; ordinary sandbox views retain their
  existing 10,000-entry and 64 MiB limits.
- The protocol binds the authority digest through its dependency-cache policy
  and uses the authority's absolute namespace Python path.
- Runtime taskpacks keep a portable `python3 ...` command. Worker PATH resolves
  it to the bound environment, while integration and common evaluation use the
  trusted absolute executable.
- A changed tree, executable, version, image digest, or instance mapping blocks
  launch before a provider call.

## Calibration task

Use the previously uncalibrated
`psf__requests_v2.12.2_v2.12.3` instance at commit
`ca15d4808734c86801c8f3d80c9152c35a163dc3`. Its public assertion verifies that
parameters are appended to an `http+unix` URL. Three provider-free repetitions
failed on the base commit in 0.04 seconds each; an independently derived
one-line implementation change made the same assertion pass in 0.04-0.10
seconds. The temporary fix is diagnostic evidence only and is not benchmark
gold authority.

## Acceptance

1. Environment authority creation and validation reject path, tree, Python,
   image, or instance drift.
2. Provider sandbox construction accepts the explicitly bounded 204 MiB
   dependency tree without relaxing ordinary view limits.
3. Taskpack-visible commands stay portable, while trusted integration and
   common evaluation resolve to the same bound Python executable.
4. Hidden evaluator material and the Podman socket are absent from provider
   views and environment variables.
5. Focused and complete runtime unit suites pass without a provider invocation.

## Deferred

- No scored or provider-backed pilot starts under this implementation change.
- Replacing the existing three-instance selection and freezing a new epoch is a
  separate operator-visible experiment decision after provider-free acceptance.
