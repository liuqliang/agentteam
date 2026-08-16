from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentteam_runtime.experiment_sandbox import (
    DEPENDENCY_TREE_IDENTITY_POLICY,
    build_provider_sandbox_descriptor,
)
from agentteam_runtime.phase3_live_pilot import (
    Phase3LivePilotError,
    _live_sandbox_configuration,
    _validate_public_verification_environments,
)
from agentteam_runtime.phase3_pilot_runner import _trusted_acceptance_argv
from agentteam_runtime.phase3_public_verification import (
    PUBLIC_DEPENDENCY_TREE_MAX_BYTES,
    PUBLIC_DEPENDENCY_TREE_MAX_ENTRIES,
    Phase3PublicVerificationError,
    _dependency_tree_identity,
    build_public_verification_environment,
    public_environment_host_command,
    public_environment_namespace_command,
    public_environment_taskpack_command,
    validate_public_verification_environment,
)


IMAGE_DIGEST = "sha256:" + "a" * 64
IMAGE_REFERENCE = "example.invalid/benchmark@" + IMAGE_DIGEST


def _environment(root, *, instance_id="fixture-instance"):
    dependency_root = root / "testbed"
    (dependency_root / "bin").mkdir(parents=True)
    python = dependency_root / "bin" / "python3.9"
    python.write_text(
        "#!/bin/sh\nprintf 'Python 3.9.20\\n'\n",
        encoding="ascii",
    )
    python.chmod(0o755)
    (dependency_root / "library.txt").write_text("fixture\n", encoding="ascii")
    return build_public_verification_environment(
        instance_id=instance_id,
        image_reference=IMAGE_REFERENCE,
        image_digest=IMAGE_DIGEST,
        source_root=dependency_root,
    )


class Phase3PublicVerificationTests(unittest.TestCase):
    def test_public_environment_uses_its_own_bounded_capacity(self):
        with patch(
            "agentteam_runtime.phase3_public_verification._bounded_tree_identity",
            return_value={
                "sha256": "a" * 64,
                "files": 1,
                "directories": 1,
                "bytes": 1,
            },
        ) as inventory:
            result = _dependency_tree_identity(Path("/fixture"))

        self.assertEqual(result["files"], 1)
        inventory.assert_called_once_with(
            Path("/fixture"),
            excluded_roots=set(),
            max_entries=PUBLIC_DEPENDENCY_TREE_MAX_ENTRIES,
            max_bytes=PUBLIC_DEPENDENCY_TREE_MAX_BYTES,
        )
        self.assertEqual(PUBLIC_DEPENDENCY_TREE_MAX_ENTRIES, 100_000)
        self.assertEqual(
            PUBLIC_DEPENDENCY_TREE_MAX_BYTES,
            2 * 1024 * 1024 * 1024,
        )

    def test_authority_binds_tree_python_and_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            authority = _environment(Path(temporary))

            validated = validate_public_verification_environment(
                authority,
                instance_id="fixture-instance",
            )
            self.assertEqual(validated, authority)
            self.assertEqual(
                public_environment_taskpack_command(
                    authority,
                    ["pytest", "-q"],
                ),
                ["python3", "-m", "pytest", "-q"],
            )
            self.assertEqual(
                public_environment_namespace_command(
                    authority,
                    ["pytest", "-q"],
                ),
                [
                    "/opt/agentteam/benchmark-env/bin/python3.9",
                    "-m",
                    "pytest",
                    "-q",
                ],
            )
            self.assertEqual(
                public_environment_host_command(
                    authority,
                    ["python3", "-c", "print('ok')"],
                )[1:],
                ["-c", "print('ok')"],
            )

    def test_authority_rejects_dependency_tree_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority = _environment(root)
            (root / "testbed" / "library.txt").write_text(
                "changed\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                Phase3PublicVerificationError,
                "content changed",
            ):
                validate_public_verification_environment(authority)

    def test_dependency_tree_view_is_explicit_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            authority = _environment(root)
            tree = authority["dependency_tree"]

            descriptor = build_provider_sandbox_descriptor(
                repository,
                runtime_views=["/usr"],
                library_views=[
                    {
                        "source": tree["source"],
                        "target": tree["target"],
                        "identity_policy": DEPENDENCY_TREE_IDENTITY_POLICY,
                    }
                ],
            )

        view = descriptor["library_views"][0]
        self.assertFalse(view["writable"])
        self.assertEqual(
            view["source_identity"]["kind"],
            "bounded_dependency_directory",
        )

    def test_live_configuration_routes_python_through_public_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority = _environment(root)
            binary_root = root / "codex-release" / "bin"
            binary_root.mkdir(parents=True)
            codex = binary_root / "codex"
            host = binary_root / "codex-code-mode-host"
            codex.write_bytes(b"codex")
            host.write_bytes(b"host")

            with patch(
                "agentteam_runtime.phase3_live_pilot.shutil.which",
                return_value=str(codex),
            ):
                configuration = _live_sandbox_configuration(
                    root / "pilot",
                    public_environment=authority,
                )
            self.assertTrue(
                configuration["environment"]["PATH"].startswith(
                    "/opt/agentteam/benchmark-env/bin:"
                )
            )
            self.assertIn(
                {
                    "source": authority["dependency_tree"]["source"],
                    "target": authority["dependency_tree"]["target"],
                    "identity_policy": DEPENDENCY_TREE_IDENTITY_POLICY,
                },
                configuration["library_views"],
            )
            self.assertEqual(
                _trusted_acceptance_argv(
                    ["pytest", "-q"],
                    public_verification_environment=authority,
                )[0],
                "/opt/agentteam/benchmark-env/bin/python3.9",
            )

    def test_image_reference_must_be_digest_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dependency_root = root / "testbed"
            (dependency_root / "bin").mkdir(parents=True)
            python = dependency_root / "bin" / "python3.9"
            python.write_text("#!/bin/sh\n", encoding="ascii")
            python.chmod(0o755)

            with self.assertRaisesRegex(
                Phase3PublicVerificationError,
                "pinned",
            ):
                build_public_verification_environment(
                    instance_id="fixture-instance",
                    image_reference="example.invalid/benchmark:latest",
                    image_digest=IMAGE_DIGEST,
                    source_root=dependency_root,
                )

    def test_new_live_bundle_cannot_fall_back_to_host_python(self):
        with self.assertRaisesRegex(
            Phase3LivePilotError,
            "require public verification",
        ):
            _validate_public_verification_environments(
                None,
                ["fixture-instance"],
            )


if __name__ == "__main__":
    unittest.main()
