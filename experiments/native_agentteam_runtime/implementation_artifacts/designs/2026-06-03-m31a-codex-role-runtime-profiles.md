# M31a Codex Role Runtime Profiles Design

## Goal

Allow role-level Codex runtime configuration without requiring every agent entry
to duplicate the same `runtime_profile`.

## Design

The agent pool may now define:

```json
{
  "role_runtime_profiles": {
    "repo_map_agent": {
      "adapter": "codex",
      "model": "gpt-5.4-mini",
      "sandbox": "workspace-write",
      "timeout_seconds": 300
    }
  }
}
```

Runtime profile resolution is deterministic:

1. `agent.runtime_profile`;
2. `agent_pool.role_runtime_profiles[agent.role]`;
3. caller or CLI runtime defaults;
4. fake runtime.

Scheduler core and resident worker-pool startup both use this policy. CLI
Codex command defaults still merge into role profiles, so local executable
configuration can stay outside the artifact.

## Policy

This milestone keeps live LLM execution Codex-only. Fake and shell remain local
test harnesses. Future API models require their own adapter and result
extraction contract.

## Non-Goals

M31a does not define role prompt contracts, model selection policy, MCP tool
exposure, or API backends for non-Codex models.
