"""Digest-bound public dependency environments for Phase 3 verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .experiment_contract import canonical_json_sha256
from .experiment_sandbox import (
    DEPENDENCY_TREE_IDENTITY_POLICY,
    DEPENDENCY_TREE_MAX_BYTES,
    DEPENDENCY_TREE_MAX_ENTRIES,
    ExperimentSandboxError,
    _bounded_tree_identity,
)


PUBLIC_VERIFICATION_ENVIRONMENT_VERSION = (
    "phase3_public_verification_environment.v1"
)
PUBLIC_VERIFICATION_NAMESPACE_ROOT = "/opt/agentteam/benchmark-env"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_PODMAN = Path("/usr/bin/podman")
_IMAGE_ENVIRONMENT_PATH = "/opt/miniconda3/envs/testbed"


class Phase3PublicVerificationError(RuntimeError):
    """Raised when a public verification environment is unsafe or changed."""


def materialize_public_verification_environment(
    *,
    instance_id,
    image_reference,
    image_digest,
    destination_root,
    podman_path=_PODMAN,
):
    """Extract only the public testbed environment from a pinned OCI image."""

    image = _validated_image(image_reference, image_digest)
    podman = Path(podman_path)
    if podman != _PODMAN or not podman.is_file() or not os.access(podman, os.X_OK):
        raise Phase3PublicVerificationError(
            "controller-approved Podman executable is unavailable"
        )
    destination = Path(destination_root)
    if (
        not destination.is_absolute()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise Phase3PublicVerificationError(
            "public verification destination must be a new absolute path"
        )
    parent = destination.parent.resolve(strict=True)
    if parent != destination.parent or not parent.is_dir():
        raise Phase3PublicVerificationError(
            "public verification destination parent is unsafe"
        )
    observed_digest = _podman_output(
        podman,
        ["image", "inspect", image["reference"], "--format", "{{.Digest}}"],
        label="image inspection",
    )
    if observed_digest != image["digest"]:
        raise Phase3PublicVerificationError("local image digest differs")
    staging = Path(
        tempfile.mkdtemp(
            prefix=destination.name + ".",
            suffix=".tmp",
            dir=parent,
        )
    )
    extracted = staging / "environment"
    extracted.mkdir(mode=0o700)
    container_id = None
    try:
        container_id = _podman_output(
            podman,
            ["create", image["reference"], "true"],
            label="environment container creation",
        )
        if _CONTAINER_ID.fullmatch(container_id) is None:
            raise Phase3PublicVerificationError(
                "environment container identity is invalid"
            )
        _podman_run(
            podman,
            [
                "cp",
                f"{container_id}:{_IMAGE_ENVIRONMENT_PATH}/.",
                str(extracted),
            ],
            label="public environment extraction",
        )
        extracted.rename(destination)
        staging.rmdir()
        try:
            return build_public_verification_environment(
                instance_id=instance_id,
                image_reference=image["reference"],
                image_digest=image["digest"],
                source_root=destination,
            )
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    finally:
        if container_id is not None:
            try:
                subprocess.run(
                    [str(podman), "rm", "-f", container_id],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=30,
                    env=_podman_environment(),
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def build_public_verification_environment(
    *,
    instance_id,
    image_reference,
    image_digest,
    source_root,
    python_relative_path="bin/python3.9",
    namespace_root=PUBLIC_VERIFICATION_NAMESPACE_ROOT,
):
    """Inventory one extracted image environment and return immutable authority."""

    source = _canonical_directory(source_root)
    image = _validated_image(image_reference, image_digest)
    target = _validated_namespace_root(namespace_root)
    relative_python = _validated_relative_path(python_relative_path)
    python = _resolved_contained_file(source, relative_python)
    tree = _dependency_tree_identity(source)
    version = _python_version(python, source)
    body = {
        "schema_version": PUBLIC_VERIFICATION_ENVIRONMENT_VERSION,
        "instance_id": _required_string(instance_id, "instance_id"),
        "image": image,
        "dependency_tree": {
            "source": str(source),
            "target": str(target),
            "identity_policy": DEPENDENCY_TREE_IDENTITY_POLICY,
            "sha256": tree["sha256"],
            "files": tree["files"],
            "directories": tree["directories"],
            "bytes": tree["bytes"],
        },
        "python": {
            "relative_path": relative_python.as_posix(),
            "source_path": str(python),
            "namespace_path": str(target / relative_python),
            "sha256": _file_sha256(python),
            "version": version,
        },
        "excluded_authority": [
            "benchmark_dataset",
            "container_socket",
            "gold_patch",
            "test_patch",
            "trusted_evaluator",
        ],
    }
    body["authority_sha256"] = canonical_json_sha256(body)
    return validate_public_verification_environment(body)


def validate_public_verification_environment(value, *, instance_id=None):
    """Recompute mutable source identity before accepting an environment."""

    required = {
        "schema_version",
        "instance_id",
        "image",
        "dependency_tree",
        "python",
        "excluded_authority",
        "authority_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise Phase3PublicVerificationError(
            "public verification environment has invalid fields"
        )
    if value["schema_version"] != PUBLIC_VERIFICATION_ENVIRONMENT_VERSION:
        raise Phase3PublicVerificationError(
            "public verification environment version is invalid"
        )
    bound_instance = _required_string(value["instance_id"], "instance_id")
    if instance_id is not None and bound_instance != instance_id:
        raise Phase3PublicVerificationError(
            "public verification environment instance differs"
        )
    image = value["image"]
    if not isinstance(image, dict) or set(image) != {"reference", "digest"}:
        raise Phase3PublicVerificationError("public image authority is invalid")
    _validated_image(image["reference"], image["digest"])
    tree = value["dependency_tree"]
    if not isinstance(tree, dict) or set(tree) != {
        "source",
        "target",
        "identity_policy",
        "sha256",
        "files",
        "directories",
        "bytes",
    }:
        raise Phase3PublicVerificationError(
            "public dependency tree authority is invalid"
        )
    if tree["identity_policy"] != DEPENDENCY_TREE_IDENTITY_POLICY:
        raise Phase3PublicVerificationError(
            "public dependency tree identity policy is invalid"
        )
    source = _canonical_directory(tree["source"])
    target = _validated_namespace_root(tree["target"])
    observed_tree = _dependency_tree_identity(source)
    expected_tree = {
        key: tree[key]
        for key in ("sha256", "files", "directories", "bytes")
    }
    if observed_tree != expected_tree:
        raise Phase3PublicVerificationError(
            "public dependency tree content changed"
        )
    python_authority = value["python"]
    if not isinstance(python_authority, dict) or set(python_authority) != {
        "relative_path",
        "source_path",
        "namespace_path",
        "sha256",
        "version",
    }:
        raise Phase3PublicVerificationError(
            "public verification Python authority is invalid"
        )
    relative_python = _validated_relative_path(
        python_authority["relative_path"]
    )
    python = _resolved_contained_file(source, relative_python)
    if (
        python_authority["source_path"] != str(python)
        or python_authority["namespace_path"] != str(target / relative_python)
        or not _SHA256.fullmatch(python_authority["sha256"] or "")
        or _file_sha256(python) != python_authority["sha256"]
        or _python_version(python, source) != python_authority["version"]
    ):
        raise Phase3PublicVerificationError(
            "public verification Python changed"
        )
    expected_exclusions = [
        "benchmark_dataset",
        "container_socket",
        "gold_patch",
        "test_patch",
        "trusted_evaluator",
    ]
    if value["excluded_authority"] != expected_exclusions:
        raise Phase3PublicVerificationError(
            "public verification exclusion contract changed"
        )
    digest_body = dict(value)
    digest = digest_body.pop("authority_sha256")
    if not _SHA256.fullmatch(digest or "") or digest != canonical_json_sha256(
        digest_body
    ):
        raise Phase3PublicVerificationError(
            "public verification environment digest changed"
        )
    return json.loads(json.dumps(value, sort_keys=True))


def public_environment_library_view(authority):
    authority = validate_public_verification_environment(authority)
    tree = authority["dependency_tree"]
    return {
        "source": tree["source"],
        "target": tree["target"],
        "identity_policy": tree["identity_policy"],
    }


def public_environment_taskpack_command(authority, command):
    """Keep taskpack authority portable while preserving Python arguments."""

    validate_public_verification_environment(authority)
    return _portable_public_command(command)


def _portable_public_command(command):
    command = _validated_command(command)
    executable = Path(command[0]).name
    if executable == "pytest":
        return ["python3", "-m", "pytest", *command[1:]]
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        return ["python3", *command[1:]]
    raise Phase3PublicVerificationError(
        "public acceptance must use Python or pytest"
    )


def public_environment_namespace_command(authority, command):
    authority = validate_public_verification_environment(authority)
    portable = _portable_public_command(command)
    return [authority["python"]["namespace_path"], *portable[1:]]


def public_environment_host_command(authority, command):
    authority = validate_public_verification_environment(authority)
    portable = _portable_public_command(command)
    return [authority["python"]["source_path"], *portable[1:]]


def public_environment_path(authority, existing_path):
    authority = validate_public_verification_environment(authority)
    prefix = authority["dependency_tree"]["target"] + "/bin"
    return f"{prefix}:{existing_path}"


def _dependency_tree_identity(root):
    try:
        return _bounded_tree_identity(
            root,
            excluded_roots=set(),
            max_entries=DEPENDENCY_TREE_MAX_ENTRIES,
            max_bytes=DEPENDENCY_TREE_MAX_BYTES,
        )
    except ExperimentSandboxError as exc:
        raise Phase3PublicVerificationError(
            "public dependency tree exceeds its integrity bound"
        ) from exc


def _validated_image(reference, digest):
    reference = _required_string(reference, "image reference")
    digest = _required_string(digest, "image digest")
    if digest.startswith("sha256:"):
        digest_hex = digest.removeprefix("sha256:")
    else:
        digest_hex = digest
        digest = "sha256:" + digest
    if not _SHA256.fullmatch(digest_hex):
        raise Phase3PublicVerificationError("image digest is invalid")
    if "@sha256:" not in reference or not reference.endswith(digest):
        raise Phase3PublicVerificationError(
            "image reference must be pinned to the declared digest"
        )
    return {"reference": reference, "digest": digest}


def _validated_namespace_root(value):
    if value != PUBLIC_VERIFICATION_NAMESPACE_ROOT:
        raise Phase3PublicVerificationError(
            "public verification namespace root is not approved"
        )
    return Path(value)


def _validated_relative_path(value):
    value = _required_string(value, "Python relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise Phase3PublicVerificationError("Python relative path is unsafe")
    return path


def _resolved_contained_file(root, relative):
    lexical = root.joinpath(*relative.parts)
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Phase3PublicVerificationError(
            "public verification Python escapes the dependency tree"
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise Phase3PublicVerificationError(
            "public verification Python is unavailable"
        )
    return resolved


def _python_version(python, cwd):
    try:
        completed = subprocess.run(
            [str(python), "--version"],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Phase3PublicVerificationError(
            "public verification Python probe failed"
        ) from exc
    version = completed.stdout.strip()
    if (
        completed.returncode != 0
        or re.fullmatch(r"Python 3\.\d+\.\d+", version) is None
    ):
        raise Phase3PublicVerificationError(
            "public verification Python version is invalid"
        )
    return version


def _canonical_directory(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise Phase3PublicVerificationError(
            "public dependency tree root is unsafe"
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Phase3PublicVerificationError(
            "public dependency tree root is unavailable"
        ) from exc
    if resolved != path or not resolved.is_dir():
        raise Phase3PublicVerificationError(
            "public dependency tree root is not canonical"
        )
    return resolved


def _validated_command(command):
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
    ):
        raise Phase3PublicVerificationError(
            "public acceptance command is invalid"
        )
    return list(command)


def _required_string(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Phase3PublicVerificationError(f"{label} is invalid")
    return value


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _podman_output(podman, arguments, *, label):
    completed = _podman_run(podman, arguments, label=label)
    value = completed.stdout.strip()
    if not value or "\n" in value or len(value) > 1024:
        raise Phase3PublicVerificationError(f"{label} returned invalid output")
    return value


def _podman_run(podman, arguments, *, label):
    try:
        completed = subprocess.run(
            [str(podman), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=300,
            env=_podman_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Phase3PublicVerificationError(f"{label} failed") from exc
    if completed.returncode != 0:
        raise Phase3PublicVerificationError(f"{label} failed")
    return completed


def _podman_environment():
    environment = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": "/usr/bin:/bin",
    }
    for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract and bind a Phase 3 public verification environment."
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--destination-root", required=True)
    parser.add_argument("--authority-path", required=True)
    arguments = parser.parse_args(argv)
    authority_path = Path(arguments.authority_path)
    if (
        not authority_path.is_absolute()
        or authority_path.exists()
        or not authority_path.parent.is_dir()
    ):
        raise Phase3PublicVerificationError(
            "authority path must be a new absolute path"
        )
    authority = materialize_public_verification_environment(
        instance_id=arguments.instance_id,
        image_reference=arguments.image_reference,
        image_digest=arguments.image_digest,
        destination_root=arguments.destination_root,
    )
    payload = (
        json.dumps(authority, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    descriptor = os.open(
        authority_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        authority_path.unlink(missing_ok=True)
        raise
    print(json.dumps(authority, sort_keys=True))


if __name__ == "__main__":
    main()
