#!/usr/bin/env python3
"""Generate persistent Phase 2 deterministic calibration authority."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
_BYTECODE_CACHE = Path(
    tempfile.mkdtemp(prefix="agentteam-calibration-pycache-")
)
_BYTECODE_CACHE.chmod(0o700)
atexit.register(shutil.rmtree, _BYTECODE_CACHE, True)
os.environ["PYTHONPYCACHEPREFIX"] = str(_BYTECODE_CACHE)
sys.pycache_prefix = str(_BYTECODE_CACHE)


def _release_root_for_this_script():
    return Path(__file__).resolve().parents[4]


def _path_contains_symlink(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _load_runtime_release(release_root):
    release_root = Path(release_root).expanduser().resolve()
    manifest_path = release_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_commit = (
        manifest.get("source_commit")
        or manifest.get("source_git_commit")
    )
    identity = {
        "release_id": manifest["release_id"],
        "release_root": str(release_root),
        "runtime_root": manifest["runtime_root"],
        "release_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "source_commit": source_commit,
        "git_object_format": manifest["git_object_format"],
    }
    from agentteam_runtime.experiment_gates import (
        _validate_runtime_release_identity,
    )

    _validate_runtime_release_identity(
        identity,
        source_commit=source_commit,
        require_files=True,
    )
    return identity


def _run(args):
    script_release_root = _release_root_for_this_script()
    requested_release_root = Path(
        args.runtime_release_root
    ).expanduser().resolve()
    if script_release_root != requested_release_root:
        raise RuntimeError(
            "calibration driver must run from the requested runtime release"
        )
    runtime_root = (
        requested_release_root
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    )
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))

    requested_output_root = Path(args.output_root).expanduser()
    if _path_contains_symlink(requested_output_root):
        raise RuntimeError(
            "deterministic calibration output root is unsafe"
        )
    output_root = requested_output_root.resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise RuntimeError(
            "deterministic calibration output root must be empty"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    runtime_release = _load_runtime_release(requested_release_root)

    from agentteam_runtime.experiment_calibration import (
        run_deterministic_calibration_from_manifest,
    )
    from agentteam_runtime.experiment_contract import (
        publish_immutable_json,
    )
    from tests.test_experiment_harness import (
        build_phase2_deterministic_fixture,
    )

    family_root = output_root / "sealed-run-family"
    family_root.mkdir()
    fixture = build_phase2_deterministic_fixture(
        family_root,
        runtime_release_identity=runtime_release,
    )
    protocol_path = output_root / "protocol.json"
    publish_immutable_json(
        protocol_path,
        fixture["protocol"],
        label="deterministic calibration protocol",
    )

    def run_record(record):
        return {
            "run_dir": record["run_dir"],
            "sandbox_authority_root": record[
                "sandbox_authority_root"
            ],
            "canary_path": record["canary_path"],
        }

    request = {
        "schema_version": (
            "phase2_deterministic_calibration_request.v1"
        ),
        "protocol_path": str(protocol_path),
        "projection_root": str(fixture["projection_root"]),
        "primary_runs": [
            run_record(record) for record in fixture["primary"]
        ],
        "repeat_run": run_record(fixture["repeat"]),
        "controlled_runs": [
            run_record(record) for record in fixture["controlled"]
        ],
        "duplicate_request_evidence": fixture["duplicate"],
        "fixture_roots": {
            name: str(path)
            for name, path in fixture["fixture_roots"].items()
        },
        "authority_roots": [
            str(output_root),
            str(next(iter(fixture["fixture_roots"].values())).parent),
        ],
    }
    request_path = output_root / "calibration-request.json"
    publish_immutable_json(
        request_path,
        request,
        label="deterministic calibration request",
    )
    report_path = output_root / "deterministic-calibration.json"
    summary = run_deterministic_calibration_from_manifest(
        request_path,
        output_path=report_path,
    )
    return {
        **summary,
        "runtime_release_id": runtime_release["release_id"],
        "sealed_run_count": (
            len(fixture["primary"])
            + 1
            + len(fixture["controlled"])
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Create a persistent deterministic Phase 2 calibration run "
            "family and canonical report without provider calls."
        )
    )
    parser.add_argument("--runtime-release-root", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    try:
        summary = _run(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
