# M38 Git-Backed Runtime Release Store Design

## Purpose

M38 replaces project-local framework copies with a global git-backed runtime
release store. The immediate goal is local convenience and reproducibility, not
binary packaging or cross-platform distribution.

The operator should be able to install AgentTeam for a project from either a
local repository ref or a remote git URL:

```bash
agentteam update --from-git /home/liuql/projects/agentteam --ref native-runtime-m0
agentteam update --from-git https://github.com/liuqliang/agentteam.git --ref v0.1.3
```

Each install resolves the ref to an exact commit. That commit becomes the
version authority for the release.

## Storage Model

Runtime code is stored once in a global immutable cache:

```text
~/.local/share/agentteam/
  runtime-releases/
    <source-key>/
      <release-id>/
        agentteam
        experiments/native_agentteam_runtime/...
        manifest.json
```

Projects keep only release pointers and events:

```text
<work_root>/
  releases/
    active.json
    events.jsonl
    refs/
      <release-id>.json
  runs/
```

`source-key` is a safe stable key derived from the source repository identity.
For example, `https://github.com/liuqliang/agentteam.git` can become
`github-com-liuqliang-agentteam`. A local path can become a path-derived key.

`release-id` is generated from the requested ref and resolved commit, for
example:

```text
native-runtime-m0-247e46e
v0.1.3-247e46e
```

## Manifest

Every global release contains `manifest.json`:

```json
{
  "manifest_schema_version": "agentteam_release_manifest.v2",
  "install_method": "git_ref",
  "release_id": "native-runtime-m0-247e46e",
  "release_root": "/home/liuql/.local/share/agentteam/runtime-releases/github-com-liuqliang-agentteam/native-runtime-m0-247e46e",
  "source_key": "github-com-liuqliang-agentteam",
  "source_repo": "https://github.com/liuqliang/agentteam.git",
  "source_ref": "native-runtime-m0",
  "source_commit": "247e46e...",
  "installed_at": "2026-06-11T00:00:00Z",
  "launcher_path": ".../agentteam",
  "runtime_root": ".../experiments/native_agentteam_runtime/m0_runtime"
}
```

Each project reference under `<work_root>/releases/refs/<release-id>.json`
records the same release identity plus the project-local activation history.
`active.json` points at one of these project references.

## Install Flow

For a local git repository:

1. Validate the source is a git repository.
2. Resolve `--ref` to a full commit SHA with `git rev-parse`.
3. Generate `source-key` and `release-id`.
4. If the global release directory already exists and its manifest matches the
   source commit, reuse it.
5. Otherwise export the source tree for that commit into the global release
   directory. The preferred mechanism is `git archive` so releases do not become
   mutable development worktrees.
6. Write the global manifest.
7. Write or update the current project's release reference.
8. Activate the project pointer and emit an update lifecycle event.

For a remote git URL:

1. Resolve the requested ref with `git ls-remote`.
2. Clone or fetch into a temporary directory.
3. Check out the resolved commit.
4. Install through the same export and manifest path as local repos.
5. Remove the temporary checkout.

Remote install uses ordinary git transport. It does not require GitHub Releases,
binary assets, or platform-specific archives.

## Runtime Binding

M37 already records the active release id on new runs. M38 keeps that behavior.
The difference is that `runtime_release_root` may now point to the global store
instead of a project-local code copy.

Existing runs remain pinned to the release they started with. Activating a new
release only affects future commands and future runs.

## Rollback

Rollback remains project-local:

```bash
agentteam update --rollback <release-id>
```

The command must find `<work_root>/releases/refs/<release-id>.json`, verify that
the referenced global release root still exists, then make it active.

Rollback does not download code. If the global release was deleted, rollback
fails with a clear missing-release error.

## Cleanup

Project-local cleanup may remove stale project refs. Global cleanup must be more
conservative.

A global release is protected when:

- any known project has it active;
- any nonterminal run is pinned to it;
- a project-local ref still points to it and cleanup is not explicitly global.

M38c adds reference discovery across known work roots before deleting global
release directories. Global cleanup is explicit through
`agentteam gc --global-releases`; without `--force`, it reports protected and
deletable releases without deleting them.

## Errors

Important failure modes should be explicit:

- source repo is not a git repository;
- requested ref cannot be resolved;
- remote clone or fetch fails;
- exported release is missing the `agentteam` launcher or runtime package;
- release directory exists but manifest points at a different commit;
- rollback target has a project ref but the global release root is missing.

All failures leave the previous active release unchanged.

## Test Strategy

Normal tests use temporary local git repositories. Remote git behavior is tested
through a local bare repository URL or a mocked subprocess boundary, so unit
tests do not require network.

Core tests:

- install from local git ref writes global manifest and project pointer;
- reinstalling the same commit reuses the global release;
- rollback activates an existing project ref without copying code;
- existing run release pin remains unchanged after activation;
- remote-style install resolves a bare-repo ref and installs the resolved commit;
- missing refs and mismatched manifests fail without changing `active.json`.

## Non-Goals

- no binary packaging;
- no GitHub Release asset downloads;
- no cross-platform build matrix;
- no DB-backed release index yet;
- no automatic deletion of globally cached releases; shared global cleanup
  requires `agentteam gc --global-releases --force`.
