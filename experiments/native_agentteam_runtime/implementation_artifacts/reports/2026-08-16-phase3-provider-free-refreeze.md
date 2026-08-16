# Phase 3 provider-free runtime refreeze

## Status

The inactive release and replacement preparation bundle are frozen and
provider-free verified. No model call or scored mode execution occurred.

## Frozen identity

- Runtime commit: `9f0be43c7238e6de2e1a91093995aa29bc5f1289`
- Runtime release: `phase3-calibration-9f0be43`
- Release root: `/tmp/agentteam-phase3-calibration-release-v13`
- Release manifest SHA-256:
  `69461dfabd8e08c4237f9b2ac7757c85f7c417d5401cea8dde45401fc0f3ee0b`
- Preparation bundle root:
  `/tmp/agentteam-phase3-calibration-bundle-requests-3738-v15`
- Bundle SHA-256:
  `efdece0f715d20439223edda532c57e9b394eaf9fb69fd5eeb962e5332d98abc`
- Protocol SHA-256:
  `662ef93869e0e86202266ceaafce11d6eee24a11ca0da760fde1b29ebd1fcef7`
- Runtime taskpack SHA-256:
  `c4a794418d28fe1e789a588f00257662b632e026500096d2f3e0ba2382ecea09`
- Runtime-policy preflight SHA-256:
  `53081f11913d087bba1ae2a6ddafc42c7d0b790946972dbc6bae56e821aa8721`

The release store has no active release. Existing AgentTeam projects are not
updated or redirected by this preparation task.

## Bound behavior

The protocol retains the `12/16` model tool boundary and freezes
`production_with_supplemental_tests`. Complete worker output remains the audit
authority, production paths form the scored patch, and conventional candidate
test paths remain supplemental. The direct runtime taskpack and shared
protocol constraints both expose this policy.

The runtime-policy receipt also binds the narrow deferred-verification
contract: worker failure remains visible, primary controller verification and
non-empty verification additions are mandatory, and unrelated failures are
not recoverable.

## Verification

- Complete source-commit suite: `1215` passed, `7` skipped.
- Six changed runtime/schema files have identical checkout and release
  SHA-256 digests.
- Release imports for protocol building, scheduler, and candidate publication
  passed.
- Independent replay validated the bundle digest and all `15` artifact
  digests through their formal validators.
- Codex bounded-config `features list` probe returned `0` with no provider
  call.
- Release controller-handoff smoke passed, including rejection of a disguised
  environment failure.
- Release candidate-split smoke published immutable complete, scored, and
  supplemental patch artifacts with the expected disjoint paths.
- No `live-authorization.json`, `live-epoch.json`, provider JSONL, invocation
  usage, or score artifact exists.

The packaged release intentionally excludes test sources. Tests therefore run
from the clean checkout at the release-bound commit; file-digest parity and
release-level smokes verify that the packaged runtime is the tested code.

## Conclusion

The provider-free refreeze gate is complete. This bundle is not executable
authorization. The next decision may select a new SWE-EVO instance and define
its bounded paid execution contract; no quality or token reduction is inferred
from this result.
