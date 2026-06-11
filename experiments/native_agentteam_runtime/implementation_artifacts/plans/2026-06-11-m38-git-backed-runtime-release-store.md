# M38 Git-Backed Runtime Release Store Implementation Plan

## Goal

Implement git-backed AgentTeam runtime releases so projects can install and
activate framework versions from explicit local or remote git refs without
copying the same framework code into every project.

## M38a Local Git Ref Install

Deliver the minimal global release store and local git install path.

- [x] Add failing tests for installing from a temporary local git repository
      with `agentteam update --from-git <repo> --ref <ref> --json`.
- [x] Add failing tests that assert the installed code lands under
      `~/.local/share/agentteam/runtime-releases/<source-key>/<release-id>/`
      or a test-overridden equivalent global root.
- [x] Add failing tests that assert the project stores only
      `<work_root>/releases/refs/<release-id>.json`, `active.json`, and
      release events, not a full framework copy under project-local releases.
- [x] Extend `release_manager.py` with:
      - source-key generation;
      - release-id generation from ref and commit;
      - local git ref resolution;
      - git archive export into the global store;
      - v2 manifest writing;
      - project ref writing.
- [x] Extend `agentteam update` parser and handler with
      `--from-git <repo>` and required `--ref <ref>`.
- [x] Preserve existing `--from <checkout>` behavior for compatibility.
- [x] Keep activation, rollback, prune status fields, release lifecycle events,
      and run release pinning compatible with both v1 and v2 manifests.
- [x] Run targeted release-manager and CLI update tests.

Acceptance:

- local `--from-git --ref` installs and activates a release;
- reinstalling the same commit reuses the existing global release root;
- `update --status` shows project release refs and active release correctly;
- `update --rollback <release-id>` works for a v2 project ref;
- existing v1 release tests still pass.

## M38b Remote Git URL Install

Add remote git support without introducing network-dependent unit tests.

- [x] Add tests using a temporary bare git repository as a remote URL.
- [x] Resolve remote refs with `git ls-remote`.
- [x] Clone or fetch the resolved commit into a temporary checkout.
- [x] Reuse the M38a export path after checkout.
- [x] Clean temporary checkouts on success and on failure.
- [x] Fail without changing `active.json` when the remote ref is missing or the
      checkout fails.

Acceptance:

- `agentteam update --from-git <bare-repo-url> --ref <branch-or-tag>` installs
  the resolved commit;
- remote install reuses an existing global release for the same source commit;
- normal tests do not require internet access.

## M38c Global Release Cleanup Protection

Make cleanup aware of shared global releases.

- [ ] Add reference discovery for known project profiles under
      `~/.local/share/agentteam`.
- [ ] Compute global release reference status from project active pointers,
      project refs, and nonterminal run pins.
- [ ] Extend `agentteam gc` or `agentteam update --prune` with dry-run metadata
      for protected and deletable global releases.
- [ ] Only delete global releases with explicit force and no active or
      nonterminal references.
- [ ] Add tests for protected active releases, pinned run releases, orphaned
      release roots, and dry-run explanations.

Acceptance:

- shared releases are not deleted while any known project references them;
- dry-run explains why a release is protected or deletable;
- project-local cleanup and global cleanup remain distinguishable.

## Verification

Run before marking M38 complete:

```bash
env PYTHONPATH=<runtime-root> python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack

env PYTHONPATH=<runtime-root> python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime

git diff --check
```

For M38b, run a manual local bare-repo smoke:

```bash
agentteam update --from-git /path/to/local/bare-agentteam.git --ref native-runtime-m0
agentteam update --status
agentteam update --rollback <previous-release-id>
```

## Out Of Scope

- binary release packaging;
- GitHub Release asset download;
- cross-platform build artifacts;
- DB-backed release index;
- automatic global release deletion before reference protection is implemented.
