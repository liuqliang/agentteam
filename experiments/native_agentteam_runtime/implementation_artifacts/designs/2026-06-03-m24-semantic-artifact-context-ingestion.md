# M24 Semantic Artifact Context Ingestion Design

Status: approved for implementation by standing roadmap authorization.

## Goal

M24 lets the planner context include compact, provenance-rich summaries of
selected design and implementation artifacts without embedding whole documents
or scanning the repository.

## Scope

M24 supports:

- an explicit allowlist of artifact paths for planner context construction;
- bounded per-source excerpts;
- source metadata with path, SHA-256 digest, byte size, modified timestamp, and
  excerpt budget;
- deterministic markdown heading extraction as a lightweight summary signal;
- clear warnings for missing, non-file, or unreadable artifacts;
- two-phase scheduler and CLI propagation for selected context artifacts.

M24 deliberately defers:

- LLM-based summarization;
- full repository indexing;
- language-aware code-map ingestion;
- automatic artifact discovery;
- changing proposal validation rules;
- allowing planner agents to edit authority artifacts.

## Architecture

`planner_context.py` remains the owner of context package construction. M24
extends `build_planner_context(...)` with:

```python
context_artifact_paths=None
context_artifact_excerpt_chars=1200
```

When paths are supplied, the returned context includes:

```json
{
  "artifact_context": {
    "schema_version": "artifact_context.v1",
    "excerpt_budget_chars": 1200,
    "sources": [
      {
        "path": "experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md",
        "sha256": "hex digest",
        "size_bytes": 4096,
        "modified_at": "2026-06-03T00:00:00Z",
        "heading_count": 4,
        "headings": ["Native Runtime Long-Term Roadmap", "Artifact Role"],
        "excerpt": "bounded text excerpt",
        "excerpt_chars": 1200,
        "omitted_chars": 2896
      }
    ],
    "warnings": []
  }
}
```

The excerpt is a deterministic bounded prefix of normalized text. It is not a
semantic summary and it is never a full-repository dump. If a source is missing,
the context records a warning such as:

```json
{"path": "/missing/doc.md", "warning": "missing"}
```

`TwoPhaseFileScheduler` receives the selected artifact list through
constructor parameters and passes it into `build_planner_context(...)` when it
writes `planner_context_path`.

The CLI adds repeatable artifact flags:

```text
--planner-context-artifact /path/to/doc.md
--planner-context-excerpt-chars 1200
```

The feature remains explicit. If no artifacts are supplied, M22 behavior is
unchanged.

## Acceptance

M24 is accepted when:

- `build_planner_context(...)` includes compact artifact summaries with path,
  digest, modified timestamp, headings, and bounded excerpt fields;
- long artifact bodies are not fully embedded by default;
- missing artifacts produce warnings rather than invented content;
- `TwoPhaseFileScheduler` writes planner context files that include selected
  artifact summaries;
- the two-phase worker-pool CLI can pass selected artifact paths into generated
  planner context files;
- M21-M23 planner proposal and Codex planner behavior still passes.
