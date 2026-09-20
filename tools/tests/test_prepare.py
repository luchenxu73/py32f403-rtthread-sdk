"""Preparation boundaries: preserve existing work and verify pinned inputs."""
# SPDX-License-Identifier: Apache-2.0

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "prepare.py"
SPEC = importlib.util.spec_from_file_location("prepare", SCRIPT)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.sdk = self.base / "sdk-source"
        self.env = self.base / "env"
        self.index = self.base / "test-index"
        self.scripts = self.env / "tools/scripts"
        self.cmsis = self.base / "CMSIS-Core"
        self.bsp = self.base / "rt-thread/bsp/py32/board"
        self.bsp.mkdir(parents=True)
        self.bridge = "# pinned HAL SDK bridge\n"
        self.entry = "# pinned package entry\n"
        self.write(self.sdk / "SConscript", "# SDK entry\n")
        self.write(self.sdk / "tools/package/Kconfig", "config PKG_USING_PY32F403_SDK\n    bool\n")
        self.write(self.sdk / "tools/package/package.json.in", json.dumps({
            "name": "py32f403_sdk", "author": {"email": ""},
            "site": [{"version": "latest", "filename": ""}],
        }))
        self.write(self.scripts / "cmds/cmd_package/cmd_package_update.py",
                   "HAL_SDK_BRIDGE_SCONSCRIPT = " + repr(self.bridge) + "\n")
        self.write(self.scripts / "package.py", "Bridge_SConscript = " + repr(self.entry) + "\n")
        self.write(self.bsp.parent / "libraries/Kconfig", "# series drivers\n")
        self.write(self.cmsis / "Include/core_cm4.h", "#define CORE_CM4\n")
        self.write(self.bsp / ".config",
                   'CONFIG_PKG_PY32F403_SDK_VER="candidate-123"\n'
                   'CONFIG_PKG_CMSIS_CORE_VER="v1.0.0"\n')
        self.parent_menu = self.index / "packages/peripherals/hal-sdk/Kconfig"
        self.write(self.parent_menu, 'menu "HAL SDK"\nendmenu\n')
        self.revisions = {
            self.scripts: prepare.ENV_SCRIPTS_REVISION,
            self.cmsis: prepare.CMSIS_REVISION,
            self.env / "packages/packages": prepare.INDEX_REVISION,
            self.index / "packages": prepare.INDEX_REVISION,
            self.bsp: "b" * 40,
            self.sdk: "s" * 40,
        }
        self.dirty = {}
        self.args = SimpleNamespace(env_root=self.env, index_root=self.index,
                                    sdk_url=None, sdk_revision=None,
                                    author_email="test@example.invalid",
                                    bsp=self.bsp, cmsis_source=self.cmsis)
        self.root_patch = patch.object(prepare, "ROOT", self.sdk)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.git_patch = patch.object(prepare, "git", side_effect=self.git)
        self.git_mock = self.git_patch.start()
        self.addCleanup(self.git_patch.stop)
        # Unexpected clones or junction creation must never reach the host.
        self.run_patch = patch.object(prepare.subprocess, "run",
                                      side_effect=AssertionError("Unexpected external mutation"))
        self.run_mock = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def git(self, path, *args):
        path = Path(path).resolve()
        if args == ("rev-parse", "HEAD"):
            return self.revisions[path]
        if args == ("status", "--porcelain"):
            return self.dirty.get(path, "")
        raise AssertionError(f"Unexpected Git read: {path}: {args}")

    def snapshot(self):
        return {path.relative_to(self.base).as_posix(): path.read_bytes()
                for path in self.base.rglob("*") if path.is_file()}

    def assert_local_rejected_without_writes(self, message):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, message):
            prepare.prepare_local(self.args)
        self.assertEqual(before, self.snapshot())
        self.run_mock.assert_not_called()

    def test_rejects_existing_foreign_sdk_package_without_overwriting(self):
        self.write(self.bsp.parent / "libraries/py32f403_sdk-candidate-123/owner.txt", "keep SDK")
        self.assert_local_rejected_without_writes("Refusing to replace existing package")

    def test_rejects_existing_foreign_sdk_bridge_before_linking(self):
        self.write(self.bsp / "packages/py32f403_sdk-candidate-123/SConscript", "custom bridge\n")
        with patch.object(prepare, "link_directory") as link:
            self.assert_local_rejected_without_writes("existing package bridge")
        link.assert_not_called()

    def test_rejects_existing_foreign_package_entry_before_linking(self):
        self.write(self.bsp / "packages/SConscript", "custom package entry\n")
        with patch.object(prepare, "link_directory") as link:
            self.assert_local_rejected_without_writes("Existing package entry differs")
        link.assert_not_called()

    def test_invalid_download_arguments_fail_before_any_git_or_file_changes(self):
        invalid_pairs = (
            ("https://example.invalid/sdk.git", None),
            ("https://example.invalid/sdk.git", "main"),
            ("https://example.invalid/sdk.git", "a" * 39),
            ("https://example.invalid/sdk.git", "z" * 40),
        )
        before = self.snapshot()
        for url, revision in invalid_pairs:
            with self.subTest(url=url, revision=revision):
                self.args.sdk_url, self.args.sdk_revision = url, revision
                with self.assertRaises(ValueError):
                    prepare.prepare_index(self.args)
                self.assertEqual(before, self.snapshot())
        self.git_mock.assert_not_called()
        self.run_mock.assert_not_called()

    def test_non_git_download_url_fails_before_any_git_or_file_changes(self):
        self.args.sdk_url = "https://example.invalid/account/sdk"
        self.args.sdk_revision = "a" * 40
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "must end in .git"):
            prepare.prepare_index(self.args)
        self.assertEqual(before, self.snapshot())
        self.git_mock.assert_not_called()
        self.run_mock.assert_not_called()

    def test_missing_author_email_fails_before_any_git_or_file_changes(self):
        self.args.sdk_url = "https://example.invalid/account/sdk.git"
        self.args.sdk_revision = "a" * 40
        before = self.snapshot()
        for email in (None, ""):
            with self.subTest(email=email):
                self.args.author_email = email
                with self.assertRaisesRegex(ValueError, "--author-email is required"):
                    prepare.prepare_index(self.args)
                self.assertEqual(before, self.snapshot())
        self.git_mock.assert_not_called()
        self.run_mock.assert_not_called()

    def test_dirty_pinned_dependencies_are_rejected_before_local_changes(self):
        for dependency in (self.scripts, self.cmsis):
            with self.subTest(dependency=dependency.name):
                self.dirty = {dependency: " M tracked-file"}
                self.assert_local_rejected_without_writes("Pinned dependency has local modifications")

    def test_wrong_dependency_revision_is_rejected_before_local_changes(self):
        self.revisions[self.cmsis] = "f" * 40
        self.assert_local_rejected_without_writes("expected .*found")

    def test_dirty_baseline_index_does_not_import_local_extensions(self):
        baseline = self.env / "packages/packages"
        local_menu = baseline / "peripherals/hal-sdk/Kconfig"
        local_package = baseline / "peripherals/hal-sdk/local/package.json"
        self.write(local_menu, 'menu "LOCAL WORKTREE ONLY"\nendmenu\n')
        self.write(local_package, '{"name": "local-only"}\n')
        self.dirty[baseline] = " M peripherals/hal-sdk/Kconfig\n?? peripherals/hal-sdk/local/"
        with redirect_stdout(io.StringIO()):
            prepare.prepare_index(self.args)
        self.assertIn('menu "HAL SDK"', self.parent_menu.read_text())
        self.assertNotIn("LOCAL WORKTREE ONLY", self.parent_menu.read_text())
        self.assertFalse((self.index / "packages/peripherals/hal-sdk/local").exists())
        self.assertEqual(local_menu.read_text(), 'menu "LOCAL WORKTREE ONLY"\nendmenu\n')
        self.assertEqual(local_package.read_text(), '{"name": "local-only"}\n')
        self.run_mock.assert_not_called()

    def test_wrong_baseline_index_revision_is_rejected_before_writes(self):
        self.revisions[self.env / "packages/packages"] = "f" * 40
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "expected .*found"):
            prepare.prepare_index(self.args)
        self.assertEqual(before, self.snapshot())
        self.run_mock.assert_not_called()

    def test_cli_resolves_relative_index_path_in_reported_environment(self):
        previous = Path.cwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, previous)
        arguments = ["prepare.py", "index", "--env-root", "env", "--index-root", "test-index"]
        output = io.StringIO()
        with patch("sys.argv", arguments), redirect_stdout(output):
            prepare.main()
        self.assertIn("PKGS_DIR=" + str(self.index), output.getvalue().splitlines())
        self.assertTrue((self.index / "Kconfig").exists())
        self.assertFalse((self.index / "packages" / prepare.SDK_INDEX / "package.json").exists())
        self.assertIn("Configuration-only SDK index", output.getvalue())
        self.run_mock.assert_not_called()

    def test_cli_defaults_to_confirmed_repository_and_email_but_requires_explicit_revision(self):
        arguments = ["prepare.py", "index", "--env-root", str(self.env),
                     "--index-root", str(self.index), "--sdk-revision", "c" * 40]
        with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
            prepare.main()
        package_json = self.index / "packages" / prepare.SDK_INDEX / "package.json"
        mapping = json.loads(package_json.read_bytes())
        self.assertEqual(mapping["repository"], "https://github.com/luchenxu73/py32f403-rtthread-sdk")
        self.assertEqual(mapping["homepage"], mapping["repository"] + "#readme")
        self.assertEqual(mapping["site"][0]["URL"], prepare.SDK_GIT_URL)
        self.assertEqual(mapping["site"][0]["VER_SHA"], "c" * 40)
        self.assertEqual(mapping["author"]["email"], "luchenxu73@gmail.com")

    def test_package_template_tracks_main_for_latest_downloads(self):
        template = json.loads((SCRIPT.parent / "package/package.json.in").read_text(encoding="utf-8"))
        self.assertEqual(template["author"]["email"], prepare.SDK_AUTHOR_EMAIL)
        self.assertEqual(template["site"][0]["URL"], prepare.SDK_GIT_URL)
        self.assertEqual(template["site"][0]["version"], "latest")
        self.assertEqual(template["site"][0]["VER_SHA"], "main")

    def test_fixed_download_mapping_is_created_and_repeat_preserves_it_explicitly(self):
        self.args.sdk_url = "https://example.invalid/account/sdk.git"
        self.args.sdk_revision = "c" * 40
        arguments = ["prepare.py", "index", "--env-root", str(self.env),
                     "--index-root", str(self.index), "--sdk-url", self.args.sdk_url,
                     "--sdk-revision", self.args.sdk_revision,
                     "--author-email", self.args.author_email]
        with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
            prepare.main()
        package_json = self.index / "packages" / prepare.SDK_INDEX / "package.json"
        before = package_json.read_bytes()
        mapping = json.loads(before)
        self.assertEqual(mapping["site"][0]["VER_SHA"], "c" * 40)
        self.assertEqual(mapping["site"][0]["URL"], self.args.sdk_url)
        self.assertEqual(mapping["author"]["email"], "test@example.invalid")
        self.args.sdk_url = self.args.sdk_revision = None
        output = io.StringIO()
        with redirect_stdout(output):
            prepare.prepare_index(self.args)
        self.assertEqual(package_json.read_bytes(), before)
        self.assertIn("Retained existing download mapping:", output.getvalue())
        self.assertEqual(self.parent_menu.read_text().count(str(prepare.SDK_INDEX).replace("\\", "/")), 1)

    def test_local_mapping_uses_configured_versions_and_records_sdk_dirty_state(self):
        self.dirty[self.sdk] = " M SConscript"
        with patch.object(prepare, "link_directory") as link, redirect_stdout(io.StringIO()):
            prepare.prepare_local(self.args)
        self.assertEqual(link.call_args_list[0].args,
                         (self.sdk, self.bsp.parent / "libraries/py32f403_sdk-candidate-123"))
        self.assertEqual(link.call_args_list[1].args,
                         (self.cmsis, self.bsp / "packages/CMSIS-Core-v1.0.0"))
        self.assertEqual((self.bsp / "packages/SConscript").read_text(), self.entry)
        manifest = json.loads((self.bsp / "packages/py32-local.json").read_text())
        self.assertTrue(manifest["sdk_dirty"])
        self.assertEqual(manifest["cmsis_core"], prepare.CMSIS_REVISION)
        self.assertEqual(manifest["configured_index_baseline"], prepare.INDEX_REVISION)
        self.assertNotIn("package_index_base", manifest)


if __name__ == "__main__":
    unittest.main()
