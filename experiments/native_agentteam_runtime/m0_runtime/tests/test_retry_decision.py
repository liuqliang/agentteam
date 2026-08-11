import unittest

from agentteam_runtime.retry_decision import decide_retry, validate_retry_decision


class RetryDecisionTest(unittest.TestCase):
    def test_retry_decision_matrix(self):
        cases = (
            {
                "case_id": "timeout",
                "failure_category": "timeout",
                "runtime_result": {
                    "result_status": "timed_out",
                    "changed_files": [],
                    "output": {"error": "timeout"},
                },
                "expected": ("retry_same_model", True, False),
            },
            {
                "case_id": "invalid-model-output",
                "failure_category": "runtime_error",
                "runtime_result": {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {"error": "invalid_output_last_message_json"},
                },
                "expected": ("escalate_model", True, True),
            },
            {
                "case_id": "permission-failure",
                "failure_category": "runtime_error",
                "runtime_result": {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "error": "provider_launch_failed",
                        "reason": "Operation not permitted",
                    },
                },
                "expected": ("repair_environment", False, False),
            },
            {
                "case_id": "transient-provider-launch",
                "failure_category": "runtime_error",
                "runtime_result": {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "error": "provider_launch_failed",
                        "reason": "503 service unavailable",
                    },
                },
                "expected": ("retry_same_model", True, False),
            },
            {
                "case_id": "integration-failure-clean-baseline",
                "failure_category": "integration_verification_failed",
                "runtime_result": {
                    "result_status": "completed",
                    "changed_files": ["src/change.py"],
                    "output": {},
                },
                "attempt_result": {
                    "integration_status": "applied",
                    "integration_verification_status": "failed",
                    "baseline_verification_status": "passed",
                },
                "expected": ("escalate_model", True, True),
                "evidence": ("integration_verification_status", "failed"),
            },
            {
                "case_id": "integration-failure-unknown-baseline",
                "failure_category": "integration_verification_failed",
                "runtime_result": {
                    "result_status": "completed",
                    "changed_files": ["src/change.py"],
                    "output": {},
                },
                "attempt_result": {
                    "integration_status": "applied",
                    "integration_verification_status": "failed",
                },
                "expected": ("retry_same_model", True, False),
            },
        )

        for case in cases:
            with self.subTest(case_id=case["case_id"]):
                decision = decide_retry(
                    attempt_id="ATTEMPT-001",
                    failure_category=case["failure_category"],
                    retryable=True,
                    runtime_result=case["runtime_result"],
                    attempt_result=case.get("attempt_result"),
                )
                validate_retry_decision(decision)
                self.assertEqual(
                    (
                        decision["action"],
                        decision["auto_retry"],
                        decision["model_escalation"],
                    ),
                    case["expected"],
                )
                if "evidence" in case:
                    key, value = case["evidence"]
                    self.assertEqual(decision["evidence"][key], value)


if __name__ == "__main__":
    unittest.main()
