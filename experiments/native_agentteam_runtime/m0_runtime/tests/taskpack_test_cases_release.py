try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ReleaseMixin:
    def test_blueprint_release_git_object_binding_and_generation_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "work"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["approval"]["runtime_release_binding_required"] = True
            blueprint["approval"]["pre04_ancestor_binding_required"] = True
            _write_json(repo / blueprint_path, blueprint)
            record = _write_blueprint_approval(repo, blueprint)
            pre04_integration_commit = subprocess.run(
                ["git", "rev-parse", "HEAD^"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            record["pre04_integration_commit"] = pre04_integration_commit
            _write_json(repo / blueprint["approval"]["record_path"], record)
            subprocess.run(
                [
                    "git",
                    "add",
                    blueprint_path,
                    blueprint["approval"]["record_path"],
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "approve release binding"],
                cwd=repo,
                env=_test_env(),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            release_id = record["preflight_release_id"]
            release_source_commit = record["preflight_release_source_commit"]
            _write_json(
                repo / ".agentteam" / "profile.json",
                {"work_root": str(work_root)},
            )
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            _write_json(
                work_root / "releases" / release_id / "manifest.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            output_root = tmp_path / "drafts"

            def fail_with_readonly_snapshot(taskpack_dir):
                readonly_dir = (
                    Path(taskpack_dir)
                    / "controller_authority"
                    / "readonly"
                )
                readonly_dir.mkdir(parents=True)
                readonly_file = readonly_dir / "taskpack.yaml"
                readonly_file.write_text("{}\n", encoding="utf-8")
                readonly_file.chmod(stat.S_IRUSR)
                readonly_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
                raise TaskpackValidationError(
                    "injected generated-package failure"
                )

            with mock.patch.object(
                taskpack_module,
                "validate_taskpack",
                side_effect=fail_with_readonly_snapshot,
            ):
                with self.assertRaisesRegex(
                    TaskpackValidationError,
                    "injected generated-package failure",
                ):
                    taskpack_module.materialize_taskpack_blueprint(
                        repo,
                        blueprint_path,
                        output_root,
                    )
            self.assertFalse(output_root.exists())

            tree_oid = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            unrelated_commit = subprocess.run(
                ["git", "commit-tree", tree_oid, "-m", "unrelated PRE-04"],
                cwd=repo,
                env=_test_env(),
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            record["preflight_release_source_commit"] = unrelated_commit
            _write_json(repo / blueprint["approval"]["record_path"], record)
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": release_id,
                    "source_git_commit": unrelated_commit,
                },
            )
            _write_json(
                work_root / "releases" / release_id / "manifest.json",
                {
                    "release_id": release_id,
                    "source_git_commit": unrelated_commit,
                },
            )
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unrelated-release-unused",
                dry_run=True,
            )
            self.assertTrue(
                any(
                    "not an ancestor of the blueprint source commit" in detail
                    for detail in dry_result["approval_diagnostics"]
                )
            )

            record["preflight_release_source_commit"] = release_source_commit
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            _write_json(
                work_root / "releases" / release_id / "manifest.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            record["pre04_integration_commit"] = unrelated_commit
            _write_json(repo / blueprint["approval"]["record_path"], record)
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unrelated-pre04-unused",
                dry_run=True,
            )
            self.assertTrue(
                any(
                    "not an ancestor" in detail
                    for detail in dry_result["approval_diagnostics"]
                )
            )

            record["pre04_integration_commit"] = "0" * 40
            _write_json(repo / blueprint["approval"]["record_path"], record)
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "pre04-unused",
                dry_run=True,
            )
            self.assertTrue(
                any("PRE-04" in detail for detail in dry_result["approval_diagnostics"])
            )

            record["pre04_integration_commit"] = pre04_integration_commit
            record["preflight_release_source_commit"] = "0" * 64
            _write_json(repo / blueprint["approval"]["record_path"], record)
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused",
                dry_run=True,
            )
            self.assertTrue(
                any("Git OID" in detail for detail in dry_result["approval_diagnostics"])
            )


    def test_install_local_replaces_existing_launcher_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            target = bin_dir / "agentteam"
            target.symlink_to(REPO_ROOT / "agentteam")
            env = {**os.environ, "HOME": str(home)}

            completed = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts" / "install-local.sh")],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(target.is_symlink())
            installed_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertIn(
                f"Installed launcher sha256: {installed_digest}",
                completed.stdout,
            )
            config = json.loads(
                (home / ".local" / "share" / "agentteam" / "launcher.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(config["development_repo_root"], str(REPO_ROOT))


    def test_agentteam_cli_gc_prunes_old_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-project")
            old_release = work_root / "releases" / "old-release"
            latest_release = work_root / "releases" / "latest-release"
            old_release.mkdir(parents=True)
            latest_release.mkdir(parents=True)
            _write_json(
                old_release / "manifest.json",
                {
                    "manifest_schema_version": "agentteam_release_manifest.v1",
                    "release_id": "old-release",
                    "release_root": str(old_release),
                    "installed_at": "2026-06-10T00:00:00Z",
                },
            )
            _write_json(
                latest_release / "manifest.json",
                {
                    "manifest_schema_version": "agentteam_release_manifest.v1",
                    "release_id": "latest-release",
                    "release_root": str(latest_release),
                    "installed_at": "2026-06-11T00:00:00Z",
                },
            )
            _write_json(
                work_root / "active_release.json",
                {
                    "pointer_schema_version": "agentteam_active_release.v1",
                    "release_id": "latest-release",
                    "release_root": str(latest_release),
                    "activated_at": "2026-06-11T00:00:00Z",
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--force",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["gc_status"], "completed")
            self.assertEqual(summary["release_prune"]["deleted_release_ids"], ["old-release"])
            self.assertFalse(old_release.exists())
            self.assertTrue(latest_release.exists())


    def test_agentteam_cli_update_status_reports_releases_and_run_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "release-a"
            managed_run = work_root / "runs" / "managed-run"
            unmanaged_run = work_root / "runs" / "unmanaged-run"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            release_root.mkdir(parents=True)
            (release_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "release_id": "release-a",
                        "release_root": str(release_root),
                        "source_root": str(tmp_path / "checkout"),
                    }
                ),
                encoding="utf-8",
            )
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "release-a", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            for run_dir, release_id in [(managed_run, "release-a"), (unmanaged_run, None)]:
                (run_dir / "state").mkdir(parents=True)
                state = {"scheduler_status": "running", "inflight_attempts": []}
                if release_id:
                    state["runtime_release_id"] = release_id
                    state["runtime_release_root"] = str(release_root)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps(state),
                    encoding="utf-8",
                )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--status",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            summary = json.loads(status_completed.stdout)
            self.assertEqual(summary["update_status"], "status")
            self.assertEqual(summary["active_release"]["release_id"], "release-a")
            self.assertEqual(summary["known_releases"][0]["release_id"], "release-a")
            self.assertEqual(summary["latest_installed_release"]["release_id"], "release-a")
            self.assertEqual(summary["runs_by_release"]["release-a"], ["managed-run"])
            self.assertEqual(summary["unmanaged_runs"], ["unmanaged-run"])


    def test_agentteam_cli_update_activate_and_rollback_record_release_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            for release_id in ["release-a", "release-b"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                _write_json(
                    release_root / "manifest.json",
                    {
                        "manifest_schema_version": "agentteam_release_manifest.v1",
                        "release_id": release_id,
                        "release_root": str(release_root),
                        "source_root": str(tmp_path / "checkout" / release_id),
                        "installed_at": (
                            "2026-06-08T09:00:00Z"
                            if release_id == "release-a"
                            else "2026-06-08T10:00:00Z"
                        ),
                    },
                )
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": "release-a",
                    "release_root": str(work_root / "releases" / "release-a"),
                },
            )

            activate_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--activate",
                    "release-b",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            rollback_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--rollback",
                    "release-a",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(activate_completed.returncode, 0, activate_completed.stderr)
            self.assertEqual(rollback_completed.returncode, 0, rollback_completed.stderr)
            activate_summary = json.loads(activate_completed.stdout)
            rollback_summary = json.loads(rollback_completed.stdout)
            self.assertEqual(activate_summary["release_event"]["event_type"], "update_activated")
            self.assertEqual(activate_summary["release_event"]["release_id"], "release-b")
            self.assertEqual(rollback_summary["release_event"]["event_type"], "rollback_activated")
            self.assertEqual(rollback_summary["release_event"]["release_id"], "release-a")
            release_events = _read_jsonl(work_root / "releases" / "events.jsonl")
            self.assertEqual(
                [event["event_type"] for event in release_events],
                ["update_activated", "rollback_activated"],
            )
            self.assertEqual([event["sequence"] for event in release_events], [1, 2])


    def test_agentteam_cli_update_status_text_lists_release_ids_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for release_id in ["release-a", "release-b"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                            "installed_at": (
                                "2026-06-08T09:00:00Z"
                                if release_id == "release-a"
                                else "2026-06-08T10:00:00Z"
                            ),
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "release-b",
                        "release_root": str(work_root / "releases" / "release-b"),
                    }
                ),
                encoding="utf-8",
            )
            unmanaged_run = work_root / "runs" / "unmanaged-run"
            (unmanaged_run / "state").mkdir(parents=True)
            (unmanaged_run / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--status",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("active_release: release-b\n", status_completed.stdout)
            self.assertIn("latest_installed_release: release-b\n", status_completed.stdout)
            self.assertIn("active_is_latest: true\n", status_completed.stdout)
            self.assertIn(
                "known_releases:\n  - release-a\n  - release-b\n",
                status_completed.stdout,
            )
            self.assertNotIn("active_release_root", status_completed.stdout)
            self.assertNotIn("unmanaged_runs", status_completed.stdout)
            self.assertNotIn(str(work_root), status_completed.stdout)


    def test_agentteam_cli_update_from_git_installs_global_release_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            source_commit = _write_agentteam_release_fixture(checkout, "git-fixture")
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            release = summary["release"]
            release_id = release["release_id"]
            release_root = Path(release["release_root"])
            self.assertEqual(summary["update_status"], "installed")
            self.assertEqual(release["manifest_schema_version"], "agentteam_release_manifest.v2")
            self.assertEqual(release["install_method"], "git_ref")
            self.assertEqual(release["source_repo"], str(checkout.resolve()))
            self.assertEqual(release["source_ref"], "HEAD")
            self.assertEqual(release["source_commit"], source_commit)
            self.assertEqual(release_root.parent.parent, global_store.resolve())
            self.assertEqual(
                Path(release["launcher_path"]),
                release_root / "agentteam",
            )
            self.assertEqual(
                Path(release["runtime_root"]),
                release_root
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime",
            )
            self.assertEqual(summary["active_release"]["release_id"], release_id)
            self.assertEqual(Path(summary["active_release"]["release_root"]), release_root)
            self.assertTrue((release_root / "manifest.json").exists())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue(Path(release["runtime_root"]).is_dir())
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "m0_runtime"
                    / "agentteam_runtime"
                    / "__init__.py"
                ).exists()
            )
            self.assertTrue((work_root / "releases" / "refs" / f"{release_id}.json").exists())
            self.assertFalse((work_root / "releases" / release_id).exists())
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], release_id)
            self.assertEqual(Path(active["release_root"]), release_root)
            known_by_id = {item["release_id"]: item for item in summary["known_releases"]}
            self.assertIn(release_id, known_by_id)
            self.assertEqual(known_by_id[release_id]["source_commit"], source_commit)


    def test_agentteam_cli_update_from_git_reuses_release_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            first_commit = _write_agentteam_release_fixture(checkout, "first")
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            first = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            repeat = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            second_commit = _write_agentteam_release_fixture(checkout, "second")
            second = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(repeat.returncode, 0, repeat.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_summary = json.loads(first.stdout)
            repeat_summary = json.loads(repeat.stdout)
            second_summary = json.loads(second.stdout)
            first_release = first_summary["release"]
            second_release = second_summary["release"]
            self.assertEqual(first_release["source_commit"], first_commit)
            self.assertEqual(repeat_summary["release"]["release_root"], first_release["release_root"])
            self.assertTrue(repeat_summary["release"]["reused_existing_release"])
            self.assertEqual(second_release["source_commit"], second_commit)
            self.assertNotEqual(second_release["release_id"], first_release["release_id"])
            self.assertNotEqual(second_release["release_root"], first_release["release_root"])

            rollback = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--rollback",
                    first_release["release_id"],
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            rollback_summary = json.loads(rollback.stdout)
            self.assertEqual(rollback_summary["active_release"]["release_id"], first_release["release_id"])
            self.assertEqual(rollback_summary["active_release"]["release_root"], first_release["release_root"])
            self.assertEqual(rollback_summary["release_event"]["event_type"], "rollback_activated")


    def test_agentteam_cli_update_from_git_installs_from_remote_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            bare_repo = tmp_path / "agentteam.git"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            source_commit = _write_agentteam_release_fixture(checkout, "remote-fixture")
            subprocess.run(
                ["git", "clone", "--bare", str(checkout), str(bare_repo)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            source_url = bare_repo.resolve().as_uri()
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    source_url,
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            release = summary["release"]
            release_root = Path(release["release_root"])
            self.assertEqual(release["source_repo"], source_url)
            self.assertEqual(release["source_ref"], "HEAD")
            self.assertEqual(release["source_commit"], source_commit)
            self.assertEqual(release["install_method"], "git_ref")
            self.assertEqual(release_root.parent.parent, global_store.resolve())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue((work_root / "releases" / "refs" / f"{release['release_id']}.json").exists())
            self.assertFalse((work_root / "releases" / release["release_id"]).exists())


    def test_agentteam_cli_update_from_git_missing_remote_ref_keeps_active_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            bare_repo = tmp_path / "agentteam.git"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            _write_agentteam_release_fixture(checkout, "remote-fixture")
            subprocess.run(
                ["git", "clone", "--bare", str(checkout), str(bare_repo)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            source_url = bare_repo.resolve().as_uri()
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            first = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    source_url,
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            missing = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    source_url,
                    "--ref",
                    "missing-ref",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            first_release = json.loads(first.stdout)["release"]
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("git ref not found", missing.stderr)
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], first_release["release_id"])
            self.assertEqual(active["release_root"], first_release["release_root"])


    def test_agentteam_cli_update_from_installs_and_activates_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            existing_run = work_root / "runs" / "existing-run"
            stale_release = work_root / "releases" / "stale-release"
            _init_repo(repo)
            _init_repo(checkout)
            runtime_pkg = checkout / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            schemas = checkout / "experiments" / "native_agentteam_runtime" / "schemas"
            runtime_pkg.mkdir(parents=True)
            schemas.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# fixture runtime\n", encoding="utf-8")
            _write_json(schemas / "taskpack_blueprint.schema.json", {"type": "object"})
            _write_json(schemas / "p0_experiment_readiness.schema.json", {"type": "object"})
            _write_json(schemas / "experiment_manifest.schema.json", {"type": "object"})
            _write_json(
                runtime_pkg / "data" / "p0_experiment_readiness.v1.json",
                {"schema_version": "p0_experiment_readiness.v1"},
            )
            (checkout / "agentteam").write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture agentteam release"],
                cwd=checkout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            stale_release.mkdir(parents=True)
            (stale_release / "manifest.json").write_text(
                json.dumps(
                    {
                        "release_id": "stale-release",
                        "release_root": str(stale_release),
                        "source_root": str(tmp_path / "old-checkout"),
                    }
                ),
                encoding="utf-8",
            )
            (existing_run / "state").mkdir(parents=True)
            (existing_run / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "runtime_release_id": "old-release",
                        "runtime_release_root": str(work_root / "releases" / "old-release"),
                    }
                ),
                encoding="utf-8",
            )

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from",
                    str(checkout),
                    "--release-id",
                    "fixture-release",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            self.assertEqual(summary["update_status"], "installed")
            self.assertEqual(summary["active_release"]["release_id"], "fixture-release")
            self.assertEqual(summary["latest_installed_release"]["release_id"], "fixture-release")
            self.assertTrue(summary["active_is_latest"])
            self.assertEqual(summary["release_prune"]["deleted_release_ids"], ["stale-release"])
            release_root = Path(summary["active_release"]["release_root"])
            self.assertTrue((release_root / "manifest.json").exists())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue((release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime" / "__init__.py").exists())
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / "taskpack_blueprint.schema.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / "p0_experiment_readiness.schema.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / "experiment_manifest.schema.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "m0_runtime"
                    / "agentteam_runtime"
                    / "data"
                    / "p0_experiment_readiness.v1.json"
                ).is_file()
            )
            self.assertFalse(stale_release.exists())
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], "fixture-release")
            existing_state = json.loads(
                (existing_run / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(existing_state["runtime_release_id"], "old-release")

            text_update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from",
                    str(checkout),
                    "--release-id",
                    "fixture-release-text",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(text_update_completed.returncode, 0, text_update_completed.stderr)
            self.assertIn("update_status: installed\n", text_update_completed.stdout)
            self.assertIn("active_release: fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("latest_installed_release: fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("active_is_latest: true\n", text_update_completed.stdout)
            self.assertIn("  - fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("pruned_releases:\n  - fixture-release\n", text_update_completed.stdout)
            self.assertNotIn("release_root", text_update_completed.stdout)
            self.assertNotIn(str(work_root), text_update_completed.stdout)


    def test_release_prune_keeps_active_and_running_run_release(self):
        from agentteam_runtime.release_manager import prune_releases

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "agentteam-work"
            releases = work_root / "releases"
            for release_id in [
                "active-release",
                "frozen-release",
                "running-release",
                "idle-release",
            ]:
                release_root = releases / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                        }
                    ),
                    encoding="utf-8",
                )
            (releases / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(releases / "active-release"),
                    }
                ),
                encoding="utf-8",
            )
            for run_id, scheduler_status, release_id in [
                ("running-run", "running", "running-release"),
                ("idle-run", "idle", "idle-release"),
            ]:
                state_dir = work_root / "runs" / run_id / "state"
                state_dir.mkdir(parents=True)
                (state_dir / "two_phase_scheduler_state.json").write_text(
                    json.dumps(
                        {
                            "scheduler_status": scheduler_status,
                            "runtime_release_id": release_id,
                            "runtime_release_root": str(releases / release_id),
                        }
                    ),
                    encoding="utf-8",
                )
            frozen_taskpack = (
                work_root
                / "frozen"
                / "v2"
                / "approved-before-run"
            )
            frozen_taskpack.mkdir(parents=True)
            _write_json(
                frozen_taskpack / "taskpack.yaml",
                {
                    "taskpack_id": "approved-before-run",
                    "status": "frozen",
                    "context": {
                        "runtime_release_id": "frozen-release",
                    },
                },
            )

            result = prune_releases(work_root, keep_latest=1)

            self.assertEqual(result["deleted_release_ids"], ["idle-release"])
            self.assertEqual(
                result["protected_release_ids"],
                [
                    "active-release",
                    "frozen-release",
                    "running-release",
                ],
            )
            self.assertTrue((releases / "active-release").exists())
            self.assertTrue((releases / "frozen-release").exists())
            self.assertTrue((releases / "running-release").exists())
            self.assertFalse((releases / "idle-release").exists())


    def test_agentteam_cli_update_prune_deletes_old_terminal_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for release_id in ["active-release", "old-release"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(work_root / "releases" / "active-release"),
                    }
                ),
                encoding="utf-8",
            )
            idle_run_state = work_root / "runs" / "idle-run" / "state"
            idle_run_state.mkdir(parents=True)
            (idle_run_state / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "runtime_release_id": "old-release",
                        "runtime_release_root": str(work_root / "releases" / "old-release"),
                    }
                ),
                encoding="utf-8",
            )

            prune_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--prune",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(prune_completed.returncode, 0, prune_completed.stderr)
            summary = json.loads(prune_completed.stdout)
            self.assertEqual(summary["update_status"], "pruned")
            self.assertEqual(summary["release_prune"]["deleted_release_ids"], ["old-release"])
            self.assertTrue((work_root / "releases" / "active-release").exists())
            self.assertFalse((work_root / "releases" / "old-release").exists())


    def test_global_release_prune_explains_protected_and_orphaned_roots(self):
        from agentteam_runtime.release_manager import prune_global_releases

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            global_store = tmp_path / "agentteam-home" / "runtime-releases"
            source_root = global_store / "source-a"
            work_root = tmp_path / "agentteam-home" / "project-a"
            other_work_root = tmp_path / "agentteam-home" / "project-b"

            release_roots = {}
            for release_id in [
                "active-release",
                "frozen-release",
                "ref-release",
                "running-release",
                "orphan-release",
            ]:
                release_root = source_root / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "manifest_schema_version": "agentteam_release_manifest.v2",
                            "install_method": "git_ref",
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_key": "source-a",
                            "installed_at": f"2026-06-12T00:00:0{len(release_roots)}Z",
                        }
                    ),
                    encoding="utf-8",
                )
                release_roots[release_id] = release_root

            (work_root / "releases").mkdir(parents=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(release_roots["active-release"]),
                    }
                ),
                encoding="utf-8",
            )
            refs_root = work_root / "releases" / "refs"
            refs_root.mkdir(parents=True)
            (refs_root / "ref-release.json").write_text(
                json.dumps(
                    {
                        "release_id": "ref-release",
                        "release_root": str(release_roots["ref-release"]),
                    }
                ),
                encoding="utf-8",
            )
            (refs_root / "frozen-release.json").write_text(
                json.dumps(
                    {
                        "release_id": "frozen-release",
                        "release_root": str(release_roots["frozen-release"]),
                    }
                ),
                encoding="utf-8",
            )
            frozen_taskpack = work_root / "frozen" / "v3" / "approved-before-run"
            frozen_taskpack.mkdir(parents=True)
            _write_json(
                frozen_taskpack / "taskpack.yaml",
                {
                    "taskpack_id": "approved-before-run",
                    "status": "frozen",
                    "context": {
                        "runtime_release_id": "frozen-release",
                    },
                },
            )
            running_state = other_work_root / "runs" / "running-run" / "state"
            running_state.mkdir(parents=True)
            (running_state / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "runtime_release_id": "running-release",
                        "runtime_release_root": str(release_roots["running-release"]),
                    }
                ),
                encoding="utf-8",
            )

            dry_run = prune_global_releases(
                work_root,
                release_store_root=global_store,
                force=False,
            )

            statuses = {item["release_id"]: item for item in dry_run["global_releases"]}
            self.assertEqual(dry_run["prune_status"], "dry_run")
            self.assertEqual(dry_run["deletable_global_release_ids"], ["orphan-release"])
            self.assertEqual(
                dry_run["protected_global_release_ids"],
                [
                    "active-release",
                    "frozen-release",
                    "ref-release",
                    "running-release",
                ],
            )
            self.assertIn("active_project", statuses["active-release"]["protection_reasons"])
            self.assertIn(
                "frozen_taskpack",
                statuses["frozen-release"]["protection_reasons"],
            )
            self.assertIn("project_ref", statuses["ref-release"]["protection_reasons"])
            self.assertIn("nonterminal_run", statuses["running-release"]["protection_reasons"])
            self.assertEqual(statuses["orphan-release"]["status"], "deletable")
            self.assertTrue(release_roots["orphan-release"].exists())

            pruned = prune_global_releases(
                work_root,
                release_store_root=global_store,
                force=True,
            )

            self.assertEqual(pruned["prune_status"], "pruned")
            self.assertEqual(pruned["deleted_global_release_ids"], ["orphan-release"])
            self.assertTrue(release_roots["active-release"].exists())
            self.assertTrue(release_roots["frozen-release"].exists())
            self.assertTrue(release_roots["ref-release"].exists())
            self.assertTrue(release_roots["running-release"].exists())
            self.assertFalse(release_roots["orphan-release"].exists())


    def test_agentteam_cli_gc_global_releases_requires_force_and_deletes_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            agentteam_home = tmp_path / "agentteam-home"
            global_store = agentteam_home / "runtime-releases"
            work_root = agentteam_home / "gc-global-project"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-global-project")

            source_root = global_store / "source-a"
            active_release = source_root / "active-release"
            orphan_release = source_root / "orphan-release"
            for release_id, release_root in [
                ("active-release", active_release),
                ("orphan-release", orphan_release),
            ]:
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "manifest_schema_version": "agentteam_release_manifest.v2",
                            "install_method": "git_ref",
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_key": "source-a",
                            "installed_at": "2026-06-12T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases").mkdir(parents=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(active_release),
                    }
                ),
                encoding="utf-8",
            )

            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)
            dry_run_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--global-releases",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(dry_run_completed.returncode, 0, dry_run_completed.stderr)
            dry_run_summary = json.loads(dry_run_completed.stdout)
            self.assertEqual(dry_run_summary["gc_status"], "dry_run")
            self.assertEqual(
                dry_run_summary["global_release_prune"]["deletable_global_release_ids"],
                ["orphan-release"],
            )
            self.assertTrue(orphan_release.exists())

            force_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--global-releases",
                    "--force",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(force_completed.returncode, 0, force_completed.stderr)
            force_summary = json.loads(force_completed.stdout)
            self.assertEqual(force_summary["gc_status"], "completed")
            self.assertEqual(
                force_summary["global_release_prune"]["deleted_global_release_ids"],
                ["orphan-release"],
            )
            self.assertTrue(active_release.exists())
            self.assertFalse(orphan_release.exists())


    def test_agentteam_cli_start_records_active_runtime_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "active-release"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "release-record-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            release_root.mkdir(parents=True)
            (release_root / "manifest.json").write_text(
                json.dumps({"release_id": "active-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "active-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )

            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Record active release on run.",
                    "--taskpack-id",
                    "release-record-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            run_state_dir = work_root / "runs" / "release-record-run" / "state"
            state_path = run_state_dir / "two_phase_scheduler_state.json"
            if not state_path.exists():
                state_path = run_state_dir / "scheduler_state.json"
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(state["runtime_release_id"], "active-release")
            self.assertEqual(state["runtime_release_root"], str(release_root))


    def test_repo_root_agentteam_launcher_dispatches_active_release_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "fixture-release"
            runtime_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            launcher = Path(__file__).resolve().parents[4] / "agentteam"
            repo.mkdir()
            (repo / ".agentteam").mkdir()
            (repo / ".agentteam" / "profile.json").write_text(
                json.dumps(
                    {
                        "profile_schema_version": "agentteam_profile.v1",
                        "project_key": "launcher-release",
                        "work_root": str(work_root),
                        "author_runtime": "fake",
                        "default_runtime": "fake",
                        "one_shot": True,
                        "max_inflight": 2,
                        "max_attempts": 1,
                        "commit_verified_integration": False,
                        "notification_project": "launcher-release",
                        "feishu": {"enabled": False, "webhook_env": None, "signing_secret_env": None},
                    }
                ),
                encoding="utf-8",
            )
            runtime_package.mkdir(parents=True)
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            (runtime_package / "agentteam.py").write_text(
                "def main(argv=None):\n    print('active release runtime marker')\n    return 0\n",
                encoding="utf-8",
            )
            (work_root / "releases").mkdir(parents=True, exist_ok=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "fixture-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)

            completed = subprocess.run(
                [str(launcher), "status", "--project-root", str(repo)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "active release runtime marker")


    def test_pre04_01_active_switch_keeps_bound_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            first = _pre04_release_fixture(work_root, "release-1")
            second = _pre04_release_fixture(work_root, "release-2", "2" * 40)
            pair = self._publish_pre04_run(work_root, first, "run-1")
            _write_json(work_root / "releases" / "active.json", second)

            validated = validate_run_binding(pair["run_dir"], expected_project_key="pre04")

            self.assertEqual(validated["binding"]["release_id"], "release-1")


    def test_pre04_02_publish_uses_already_selected_release_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            selected = _pre04_release_fixture(work_root, "release-1")
            active = _pre04_release_fixture(work_root, "release-2", "2" * 40)
            _write_json(work_root / "releases" / "active.json", active)

            pair = self._publish_pre04_run(work_root, selected, "run-1")

            self.assertEqual(pair["binding"]["release_id"], "release-1")


    def test_pre04_03_tampered_or_missing_bound_release_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            pair = self._publish_pre04_run(work_root, release, "run-1")
            binding_path = Path(pair["run_dir"]) / "state" / "runtime_release_binding.v1.json"
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            binding["source_commit"] = "f" * 40
            _write_json(binding_path, binding)

            with self.assertRaises(AgentTeamReleaseError):
                validate_run_binding(pair["run_dir"], expected_project_key="pre04")


    def test_pre04_04_bound_terminal_release_is_gc_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            bound = _pre04_release_fixture(work_root, "release-1")
            _pre04_release_fixture(work_root, "release-2", "2" * 40)
            self._publish_pre04_run(work_root, bound, "run-1")

            result = prune_releases(work_root, keep_latest=0)

            self.assertIn("release-1", result["protected_release_ids"])
            self.assertTrue(Path(bound["release_root"]).exists())


    def test_pre04_05_approval_expected_release_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")

            with self.assertRaises(AgentTeamReleaseError):
                publish_implementation_run(
                    work_root,
                    project_key="pre04",
                    run_id="run-1",
                    taskpack_id="run-1",
                    release_identity=release,
                    expected_release={"release_id": "release-2"},
                )


    def test_pre04_08_initial_run_launcher_resolves_frozen_expected_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            _init_repo(repo)
            release = _pre04_release_fixture(
                work_root,
                "release-1",
                _git_head(repo),
                runtime_source=Path(__file__).resolve().parents[1],
            )
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                    one_shot=True,
                ),
            )
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Exercise initial immutable runtime binding.",
                draft_root=work_root / "drafts",
                taskpack_id="run-1",
                write_scope=["src/"],
            )
            _set_taskpack_runtime_backend(draft["taskpack_dir"], "fake")
            taskpack_path = Path(draft["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["context"] = {
                "runtime_release_id": release["release_id"],
                "runtime_release_source_commit": release["source_commit"],
                "git_object_format": release["git_object_format"],
            }
            _write_json(taskpack_path, taskpack)
            frozen_result = freeze_taskpack(draft["taskpack_dir"], work_root / "frozen")
            frozen = Path(frozen_result["frozen_taskpack_dir"])
            launcher = runpy.run_path(str(Path(__file__).resolve().parents[4] / "agentteam"))

            selection = launcher["_initial_run_selection"](
                ["run", str(frozen), "--run-root", str(work_root / "runs")]
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[4] / "agentteam"),
                    "run",
                    str(frozen),
                    "--run-root",
                    str(work_root / "runs"),
                    "--one-shot",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(selection["release"]["release_id"], "release-1")
            self.assertEqual(selection["run_dir"], str(work_root / "runs" / "run-1"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            bound = validate_run_binding(
                work_root / "runs" / "run-1", expected_project_key="pre04"
            )
            self.assertEqual(bound["binding"]["release_id"], "release-1")


    def test_pre04_10_path_installed_launcher_uses_bound_release_after_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            bin_dir = tmp_path / "bin"
            _init_repo(repo)
            bin_dir.mkdir()
            launcher_source = Path(__file__).resolve().parents[4] / "agentteam"
            installed = bin_dir / "agentteam"
            shutil.copy2(launcher_source, installed)
            installed.chmod(0o755)
            runtime_source = Path(__file__).resolve().parents[1]
            first = _pre04_release_fixture(
                work_root, "release-1", runtime_source=runtime_source
            )
            second = _pre04_release_fixture(
                work_root, "release-2", "2" * 40, runtime_source=runtime_source
            )
            pair = self._publish_pre04_run(work_root, first, "run-1")
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                ),
            )
            _write_json(work_root / "releases" / "active.json", second)
            _write_json(
                Path(pair["run_dir"]) / "state" / "scheduler_state.json",
                {"scheduler_status": "completed"},
            )
            env = _test_env()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"

            completed = subprocess.run(
                [
                    shutil.which("agentteam", path=env["PATH"]),
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    pair["run_dir"],
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(shutil.which("agentteam", path=env["PATH"]), str(installed))
            self.assertEqual(completed.returncode, 0, completed.stderr)


    def test_pre04_16_update_command_adopts_only_with_explicit_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "work"
            _init_repo(repo)
            release = _pre04_release_fixture(work_root, "release-1")
            _write_json(work_root / "releases" / "active.json", release)
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                ),
            )
            (work_root / "runs" / "legacy-run").mkdir(parents=True)

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rejected = agentteam_module.main(
                    [
                        "update",
                        "--project-root",
                        str(repo),
                        "--adopt-run",
                        "legacy-run",
                        "--json",
                    ]
                )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                adopted = agentteam_module.main(
                    [
                        "update",
                        "--project-root",
                        str(repo),
                        "--adopt-run",
                        "legacy-run",
                        "--force",
                        "--json",
                    ]
                )

            self.assertEqual(rejected, 1)
            self.assertIn("--force", stderr.getvalue())
            self.assertEqual(adopted, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["update_status"], "legacy_run_adopted")
            self.assertEqual(
                payload["runtime_release_binding"]["release_id"],
                "release-1",
            )
