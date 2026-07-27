# Phase 2 P2-02B Review Repair

Status: accepted on Phase 2 feature branch; P2-06 production wiring pending

## Reason

The first P2-02B worker patch implemented the intended primitives but reported
incomplete L2 evidence. Host verification later proved the real bubblewrap
canary denial and the full test suite, but independent review found that the
initial implementation could still produce false promotion evidence.

The rejected patch was not merged to the source branch as an accepted result.
It was staged on the Phase 2 feature branch for operator repair.

## Required Repairs

- bind evaluation to the exact preregistered command and executable artifact;
- load the experiment protocol only through an immutable controller authority
  reference and bind it to the certified Git baseline identity;
- reject output truncation as promotion evidence;
- use bounded incremental artifact traversal and descriptor-based reads;
- reject canary content, digest, and path in provider mounts or environment;
- publish sandbox and scan scope through immutable controller-owned references;
- validate formal invocation start and terminal schemas;
- bind and seal the complete invocation set to run, taskpack, and sandbox;
- derive each invocation policy from its immutable, canary-probed sandbox
  reference and bind both reference and policy digests through lifecycle,
  seal, manifest, and evaluation evidence;
- support one controller-owned manifest spanning authoring, worker, and
  follow-up lifecycle roots across multiple taskpacks;
- prevent late invocation creation after the seal;
- bind terminal records to the exact start digest and all shared lifecycle
  identities;
- run evaluation in a fixed-path systemd user service with cgroup limits;
- clear inherited service environment, start certified bwrap directly from
  systemd, and stream digest-checked evaluator bytes into a fixed loader inside
  namespace-private tmpfs;
- make the fixed loader independently execute the preregistered acceptance argv
  after the digest-bound evaluator contract check succeeds;
- restrict acceptance interpreters to fixed system paths and retain their
  executable digest;
- reject overlapping mount targets, recheck candidate Git ancestry, hash a
  bounded workspace-structure and Git-control inventory before and after
  acceptance, overlay candidate `.git` read-only, and keep the run authority
  root independent from per-role lifecycle roots;
- parse standalone Git config against a closed safe-key set and sanitize every
  host Git invocation so candidate configuration cannot launch external code;
- bind canonical mount sources to their root object identities and reject
  post-probe source replacement before launch;
- hash bounded mutable directory mounts and restrict system-directory identity
  exceptions to root-owned, non-group/other-writable trees;
- bind every invocation-root manifest entry to the sandbox policy of its own
  author, worker-attempt, or candidate workspace;
- propagate the runtime sandbox reference through model invocation contexts;
- require explicit run authority and lifecycle roots outside every
  provider-visible host mount;
- derive invocation roots only from a fixed controller registry under the run
  authority, publish immutable registration records, and require the manifest
  to cover both ledger and registry exactly;
- document that registration append-only semantics trust the controller and
  exclude same-UID controller tampering from the provider threat model;
- keep host-specific authority references out of frozen taskpacks;
- require standalone sanitized experiment repositories and reject linked
  worktree `.git` files.

## Cross-Task Boundary

P2-02B supplies the enforced primitives and runtime context transport. P2-05
must publish the complete retained artifact roots used by the scan-scope
authority. P2-06 must create and inject the sandbox reference into every
model-using mode and role. Phase 2 cannot claim blind-gold isolation until
those production adapters pass their L2 checks.

## Accepted Residual Risks

- Registration append-only semantics trust the controller and do not defend
  against deliberate same-UID controller deletion.
- Mutable mount trees are revalidated immediately before launch, but bwrap's
  path-based interface leaves a narrow same-UID host check-to-use race.
- Workspace identity covers paths, entry types, modes, contents, ignored files,
  empty directories, and Git control state; mtime, xattr, and ACL-only changes
  are outside the promotion-relevant identity.

These are outside the provider threat model. Defending against an adversarial
same-UID host process would require controller-owned immutable runtime snapshots
or an FD-based mount mechanism.

## Verification Contract

The repair is accepted only after:

- focused adversarial tests reject detached evaluators, non-terminal or
  schema-invalid lifecycle records, omitted sandbox references, canary
  environment leaks, scope drift, truncation, late invocation creation,
  terminal/start drift, untrusted interpreter paths, and cross-taskpack
  invocation omission;
- a real `/usr/bin/bwrap` probe proves the canary path and content are denied;
- candidate acceptance code cannot read the gold canary through a relative
  host path;
- an incorrect evaluator cannot peel off the bwrap prefix because it starts
  only after the namespace exists;
- a real `/usr/bin/systemd-run` probe proves detached children do not survive;
- a combined real systemd-to-bwrap probe proves namespace descendants do not
  survive the evaluator timeout;
- the related model-invocation suite and full native runtime suite pass;
- independent security and integration review report no blocker or major
  finding within the P2-02B primitive boundary.
