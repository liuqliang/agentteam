import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agentteam_runtime.model_context_budget import (
    CONTEXT_BUDGET_POLICY_FIELDS,
    HOOK_TRUST_BYPASS_OPTION,
    TOOL_BUDGET_POLICY,
    codex_context_policy_arguments,
    codex_tool_budget_hook_command,
    codex_tool_budget_hook_configurations,
    measure_codex_jsonl_tool_calls,
    normalize_context_budget_policy,
)


def _policy(**overrides):
    value = {
        "tool_output_token_limit": 4_000,
        "web_search_policy": "disabled",
        "model_auto_compact_token_limit": 32_768,
        "tool_call_soft_limit": 3,
        "tool_call_hard_limit": 5,
        "tool_budget_policy": TOOL_BUDGET_POLICY,
    }
    value.update(overrides)
    return value


class ModelContextBudgetTests(unittest.TestCase):
    def test_normalizes_legacy_and_complete_policies(self):
        legacy = normalize_context_budget_policy(
            {
                "tool_output_token_limit": 4_000,
                "web_search_policy": "disabled",
            }
        )
        self.assertEqual(
            set(legacy),
            {"tool_output_token_limit", "web_search_policy"},
        )
        complete = normalize_context_budget_policy(_policy())
        self.assertEqual(set(complete), CONTEXT_BUDGET_POLICY_FIELDS)

    def test_rejects_partial_policy_and_invalid_limit_order(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            normalize_context_budget_policy(
                {
                    "tool_output_token_limit": 4_000,
                    "web_search_policy": "disabled",
                    "tool_call_soft_limit": 3,
                }
            )
        with self.assertRaisesRegex(ValueError, "must exceed"):
            normalize_context_budget_policy(
                _policy(tool_call_soft_limit=5, tool_call_hard_limit=5)
            )

    def test_codex_arguments_bind_compaction_hook_and_trust_policy(self):
        arguments = codex_context_policy_arguments(_policy())
        configurations = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "-c"
        ]
        self.assertEqual(
            configurations[:3],
            [
                "tool_output_token_limit=4000",
                'web_search="disabled"',
                "model_auto_compact_token_limit=32768",
            ],
        )
        self.assertEqual(
            configurations[3:],
            codex_tool_budget_hook_configurations(3, 5),
        )
        self.assertEqual(arguments.count(HOOK_TRUST_BYPASS_OPTION), 1)

    def test_hook_warns_then_blocks_with_per_turn_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            responses = [
                self._invoke_hook(state_root, turn_id="turn-a")
                for _ in range(6)
            ]
            self.assertEqual(responses[0], {})
            self.assertIn(
                "2 tool calls remain",
                responses[2]["hookSpecificOutput"]["additionalContext"],
            )
            self.assertEqual(responses[3], {})
            self.assertIn(
                "hard tool-call limit reached",
                responses[4]["hookSpecificOutput"]["additionalContext"],
            )
            blocked = responses[5]
            self.assertEqual(blocked["decision"], "block")
            self.assertIn("No further tools", blocked["reason"])
            self.assertEqual(
                self._invoke_hook(state_root, turn_id="turn-b"),
                {},
            )

    def test_hook_counter_is_locked_across_concurrent_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(
                    pool.map(
                        lambda _index: self._invoke_hook(
                            state_root,
                            turn_id="parallel-turn",
                            soft_limit=12,
                            hard_limit=16,
                        ),
                        range(20),
                    )
                )
            warnings = [
                value
                for value in responses
                if "hookSpecificOutput" in value
            ]
            blocked = [
                value
                for value in responses
                if value.get("decision") == "block"
            ]
            self.assertEqual(len(warnings), 2)
            self.assertEqual(len(blocked), 4)
            counter_files = list(state_root.glob("*.count"))
            self.assertEqual(len(counter_files), 1)
            self.assertEqual(counter_files[0].read_text(), "16")

    def test_transcript_replay_counts_only_completed_allowed_tools(self):
        transcript = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "command_execution"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {"type": "command_execution"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "file_change"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message"},
                    }
                ),
                "not-json",
            ]
        )
        self.assertEqual(
            measure_codex_jsonl_tool_calls(transcript),
            {
                "completed_tool_calls": 2,
                "completed_by_type": {
                    "command_execution": 1,
                    "file_change": 1,
                },
                "malformed_lines": 1,
            },
        )

    def _invoke_hook(
        self,
        state_root,
        *,
        turn_id,
        soft_limit=3,
        hard_limit=5,
        hook_event_name="PreToolUse",
    ):
        environment = dict(os.environ)
        environment["AGENTTEAM_TOOL_BUDGET_STATE_DIR"] = str(state_root)
        completed = subprocess.run(
            codex_tool_budget_hook_command(soft_limit, hard_limit),
            shell=True,
            input=json.dumps(
                {
                    "session_id": "test-session",
                    "turn_id": turn_id,
                    "tool_name": "Bash",
                    "hook_event_name": hook_event_name,
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
