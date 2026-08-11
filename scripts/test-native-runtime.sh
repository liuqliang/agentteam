#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime_root="$repo_root/experiments/native_agentteam_runtime/m0_runtime"
tests_package="experiments.native_agentteam_runtime.m0_runtime.tests"
lane=${1:-fast}

export PYTHONPATH="$runtime_root${PYTHONPATH:+:$PYTHONPATH}"
cd "$repo_root"

case "$lane" in
  fast)
    python3 -m unittest \
      "$tests_package.test_benchmark_adapter" \
      "$tests_package.test_benchmark_preregistration" \
      "$tests_package.test_decision_artifact_lifecycle" \
      "$tests_package.test_decision_ledger" \
      "$tests_package.test_decision_runtime" \
      "$tests_package.test_invocation_projection" \
      "$tests_package.test_model_invocation_usage" \
      "$tests_package.test_model_routing" \
      "$tests_package.test_monolith_ownership" \
      "$tests_package.test_retry_decision" \
      "$tests_package.test_phase3_pilot" \
      "$tests_package.test_pre03b_completion"
    ;;
  integration)
    python3 -m unittest \
      "$tests_package.test_m0_runtime" \
      "$tests_package.test_taskpack" \
      "$tests_package.test_git_code_state" \
      "$tests_package.test_projection_db_operator_paths"
    ;;
  experiment)
    python3 -m unittest \
      "$tests_package.experiment_suite_budget" \
      "$tests_package.experiment_suite_results" \
      "$tests_package.experiment_suite_runtime" \
      "$tests_package.experiment_suite_calibration" \
      "$tests_package.test_experiment_ownership" \
      "$tests_package.test_experiment_readiness" \
      "$tests_package.test_phase3_benchmark_readiness" \
      "$tests_package.test_phase1_usage_end_to_end"
    ;;
  host)
    AGENTTEAM_HOST_TESTS=1 python3 -m unittest \
      "$tests_package.test_taskpack.TaskpackTests.test_agentteam_cli_notify_test_sends_feishu_message_from_profile" \
      "$tests_package.test_taskpack.TaskpackTests.test_agentteam_cli_notify_run_completed_sends_existing_run_summary" \
      "$tests_package.test_experiment_harness.ExperimentSandboxTests.test_real_supervisor_revalidates_release_source_before_exec"
    ;;
  full)
    python3 -m unittest discover \
      -s experiments/native_agentteam_runtime/m0_runtime/tests \
      -p 'test_*.py'
    ;;
  *)
    printf 'usage: %s {fast|integration|experiment|host|full}\n' "$0" >&2
    exit 2
    ;;
esac
