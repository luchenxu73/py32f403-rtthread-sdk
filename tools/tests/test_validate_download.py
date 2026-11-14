"""Check archive authenticity and isolation before the real installer can run."""
# SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("validate_download", TOOLS / "validate_download.py")
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


class DownloadValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def archive(self, extra=None, wrong_digest=False, flat=False):
        data = b"SDK entry\n"
        manifest = {"format": 1, "package": "py32f403_sdk", "files": [{
            "path": "SConscript", "size": len(data),
            "sha256": "0" * 64 if wrong_digest else hashlib.sha256(data).hexdigest(),
        }]}
        prefix = "" if flat else "py32f403_sdk-1.4.5/"
        filename = self.base / "sdk.zip"
        with zipfile.ZipFile(filename, "w") as archive:
            archive.writestr(prefix + "SConscript", data)
            archive.writestr(prefix + "package-manifest.json", json.dumps(manifest))
            if extra:
                archive.writestr(*extra)
        return filename, manifest

    def test_accepts_env_layout_and_exact_manifest(self):
        filename, expected = self.archive()
        self.assertEqual(download.archive_manifest(filename), expected)

    def test_rejects_flat_zip_before_env(self):
        filename, _ = self.archive(flat=True)
        with self.assertRaisesRegex(ValueError, "top-level directory"):
            download.archive_manifest(filename)

    def test_rejects_digest_mismatch_before_env(self):
        filename, _ = self.archive(wrong_digest=True)
        with self.assertRaisesRegex(ValueError, "Manifest mismatch"):
            download.archive_manifest(filename)

    def test_rejects_unlisted_file(self):
        filename, _ = self.archive(extra=("py32f403_sdk-1.4.5/unlisted.c", "unexpected"))
        with self.assertRaisesRegex(ValueError, "exact ZIP file set"):
            download.archive_manifest(filename)

    def test_rejects_path_traversal_and_extra_root(self):
        for name in ("../escape.c", "/absolute/file.c", "C:/absolute.c"):
            with self.subTest(name=name):
                filename, _ = self.archive(extra=(name, "unexpected"))
                with self.assertRaisesRegex(ValueError, "Unsafe ZIP entry"):
                    download.archive_manifest(filename)
        filename, _ = self.archive(extra=("other/file.c", "unexpected"))
        with self.assertRaisesRegex(ValueError, "top-level directory"):
            download.archive_manifest(filename)

    def test_refuses_existing_output_or_source_nested_output(self):
        with self.assertRaisesRegex(ValueError, "new directory"):
            download.new_output(self.source, self.source)
        with self.assertRaisesRegex(ValueError, "inside the source BSP"):
            download.new_output(self.source / "child", self.source)

    def test_refuses_sdk_output_outside_cache(self):
        with patch.object(download, "ROOT", self.base):
            with self.assertRaisesRegex(ValueError, "only under .cache"):
                download.new_output(self.base / "output/new", self.source)
            self.assertEqual(download.new_output(self.base / ".cache/new", self.source),
                             self.base / ".cache/new")

    def test_index_archive_uses_committed_tree_despite_local_extensions(self):
        """Use a real Git tree to keep staged and untracked index edits isolated."""
        env_root = self.base / "env"
        baseline = env_root / "packages/packages"
        baseline.mkdir(parents=True)
        command = ["git", "-c", "safe.directory=" + baseline.as_posix(),
                   "-c", "user.name=Package test", "-c", "user.email=test@example.invalid",
                   "-c", "core.autocrlf=false", "-C", str(baseline)]

        def git(*args):
            return subprocess.check_output(command + list(args), stderr=subprocess.PIPE,
                                           text=True).strip()

        git("init", "--quiet")
        menu = baseline / "peripherals/hal-sdk/Kconfig"
        self.write(menu, 'menu "COMMITTED BASELINE"\nendmenu\n')
        core = baseline / download.CORE_INDEX
        original_url = "https://example.invalid/committed-core.git"
        self.write(core / "package.json", json.dumps({"site": [{"version": "latest", "URL": original_url}]}))
        self.write(core / "Kconfig", 'config CORE_VER\n    string\n    default "latest"\n')
        git("add", ".")
        git("commit", "--quiet", "-m", "Pinned test index")
        revision = git("rev-parse", "HEAD")
        self.write(menu, 'menu "LOCAL WORKTREE ONLY"\nendmenu\n')
        self.write(core / "package.json", json.dumps({"site": [{"version": "latest", "URL": "https://example.invalid/local-only.git"}]}))
        git("add", ".")
        local_package = baseline / "peripherals/hal-sdk/local/package.json"
        self.write(local_package, '{"name": "local-only"}\n')
        before_status = git("status", "--porcelain")
        self.assertTrue(before_status)
        target = self.base / "isolated-index"
        with patch.object(download, "INDEX_REVISION", revision):
            actual_url = download.prepare_index(env_root, target, "https://example.invalid/sdk.zip")
        self.assertEqual(actual_url, original_url)
        self.assertIn("COMMITTED BASELINE", (target / "packages/peripherals/hal-sdk/Kconfig").read_text())
        self.assertNotIn("LOCAL WORKTREE ONLY", (target / "packages/peripherals/hal-sdk/Kconfig").read_text())
        self.assertFalse((target / "packages/peripherals/hal-sdk/local").exists())
        self.assertEqual(git("status", "--porcelain"), before_status)
        self.assertEqual(menu.read_text(), 'menu "LOCAL WORKTREE ONLY"\nendmenu\n')
        wrong_target = self.base / "wrong-revision-index"
        with patch.object(download, "INDEX_REVISION", "0" * 40):
            with self.assertRaisesRegex(ValueError, "expected .*found"):
                download.prepare_index(env_root, wrong_target, "https://example.invalid/sdk.zip")
        self.assertFalse(wrong_target.exists())

    def test_clean_copy_skips_all_packages_and_does_not_follow_known_sdk_links(self):
        for filename in (".config", "SConstruct", "board/board.c", "libraries/Kconfig"):
            self.write(self.source / filename, "source\n")
        self.write(self.source / "Kconfig", 'rsource "libraries/Kconfig"\n')
        for filename in ("packages/SDK/file", "libraries/py32f403_sdk-latest/file",
                         "libraries/vendor/file", "build/main.o", "rtthread.elf",
                         "dist/project/board/board.c", "dist/project/packages/SDK/file"):
            self.write(self.source / filename, "installed or generated\n")
        link = self.source / "libraries/py32f403_sdk-latest"
        original_is_link = download.is_link
        with patch.object(download, "is_link", side_effect=lambda path: path == link or original_is_link(path)):
            download.copy_clean_bsp(self.source, self.base / "bsp")
        result = {file.relative_to(self.base / "bsp").as_posix()
                  for file in (self.base / "bsp").rglob("*") if file.is_file()}
        self.assertEqual(result, {".config", "SConstruct", "Kconfig", "board/board.c", "libraries/Kconfig"})
        self.assertTrue((link / "file").exists())

    def test_clean_copy_adapts_series_include_and_preserves_original_board(self):
        self.write(self.source / ".config", "")
        self.write(self.source / "SConstruct", "")
        original = 'mainmenu "Test board"\nrsource "../libraries/Kconfig"\n'
        self.write(self.source / "Kconfig", original)
        self.write(self.source.parent / "libraries/Kconfig", 'config TEST_SERIES\n    bool\n')
        target = self.base / "isolated/bsp"
        changes = download.copy_clean_bsp(self.source, target)
        copied = (target / "Kconfig").read_text()
        self.assertEqual(copied, original.replace("../libraries/", "libraries/"))
        included = copied.split('rsource "', 1)[1].split('"', 1)[0]
        self.assertTrue((target / included).is_file())
        self.assertFalse((target.parent / "libraries/Kconfig").exists())
        self.assertEqual((self.source / "Kconfig").read_text(), original)
        self.assertEqual(changes[0]["file"], "Kconfig")
        before = (target / "Kconfig").read_bytes()
        self.assertEqual(download.adapt_copy_kconfig(target), [])
        self.assertEqual((target / "Kconfig").read_bytes(), before)

    def test_copy_adapter_rejects_unknown_layout_without_editing_kconfig(self):
        self.write(self.source / "libraries/Kconfig", "")
        for include in ('rsource "../../libraries/Kconfig"\n',
                        'rsource "other/Kconfig"\n',
                        'rsource "../libraries/Kconfig"\nrsource "libraries/Kconfig"\n'):
            with self.subTest(include=include):
                self.write(self.source / "Kconfig", include)
                before = (self.source / "Kconfig").read_bytes()
                with self.assertRaisesRegex(ValueError, "Unsupported BSP Kconfig libraries layout"):
                    download.adapt_copy_kconfig(self.source)
                self.assertEqual((self.source / "Kconfig").read_bytes(), before)

    def test_real_copied_bsp_pyconfig_when_dependencies_are_available(self):
        """Exercise real SCons/Kconfig parsing after relocation, without downloads."""
        rtt_root = Path(os.environ.get("PY32_TEST_RTT_ROOT", TOOLS.parent.parent / "rt-thread"))
        index = Path(os.environ.get("PY32_TEST_INDEX_ROOT", TOOLS.parent / ".cache/package-index"))
        source = rtt_root / "bsp/py32/py32f403_start_kit"
        if (not (source / "SConstruct").is_file() or not (index / "Kconfig").is_file()
                or importlib.util.find_spec("SCons") is None
                or importlib.util.find_spec("kconfiglib") is None):
            self.skipTest("Requires local PY32 BSP, prepared package index, SCons and kconfiglib")
        source_config = (source / ".config").read_bytes()
        source_kconfig = (source / "Kconfig").read_bytes()
        target = self.base / "relocated/bsp"
        changes = download.copy_clean_bsp(source, target)
        self.assertTrue(any("RTT_DIR" in change["from"] for change in changes))
        environment = dict(os.environ, RTT_ROOT=str(rtt_root.resolve()), RTT_CC="gcc",
                           PKGS_DIR=str(index.resolve()), PKGS_ROOT=str(index.resolve()),
                           ENV_ROOT=str(self.base / "isolated-env"), PYTHONUTF8="1")
        result = subprocess.run([sys.executable, "-m", "SCons", "--pyconfig-silent"],
                                cwd=target, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout)
        configured = (target / ".config").read_text(encoding="utf-8")
        for selected in ("CONFIG_PKG_USING_PY32F403_SDK=y", "CONFIG_PKG_USING_CMSIS_CORE=y"):
            self.assertIn(selected, configured)
        self.assertIn("#define PKG_USING_PY32F403_SDK", (target / "rtconfig.h").read_text())
        self.assertFalse((target / "packages").exists())
        self.assertEqual((source / ".config").read_bytes(), source_config)
        self.assertEqual((source / "Kconfig").read_bytes(), source_kconfig)

    def test_rejects_unknown_source_link(self):
        self.write(self.source / ".config", "")
        self.write(self.source / "SConstruct", "")
        self.write(self.source / "suspicious", "")
        original_is_link = download.is_link
        with patch.object(download, "is_link", side_effect=lambda path: path.name == "suspicious" or original_is_link(path)):
            with self.assertRaisesRegex(ValueError, "Unexpected BSP source link"):
                download.copy_clean_bsp(self.source, self.base / "bsp")

    def test_versions_change_only_isolated_config_and_reject_extra_package(self):
        content = ('CONFIG_PKG_PY32F403_SDK_PATH="/packages/sdk"\n'
                   'CONFIG_PKG_PY32F403_SDK_VER="latest"\n'
                   'CONFIG_PKG_CMSIS_CORE_PATH="/packages/core"\n'
                   'CONFIG_PKG_CMSIS_CORE_VER="latest"\n')
        self.write(self.source / ".config", content)
        download.configure_versions(self.source)
        actual = (self.source / ".config").read_text()
        self.assertIn(f'CONFIG_PKG_CMSIS_CORE_VER="{download.CORE_VERSION}"', actual)
        self.assertIn(f'CONFIG_PKG_PY32F403_SDK_VER="{download.SDK_VERSION}"', actual)
        self.write(self.source / ".config", content + 'CONFIG_PKG_OTHER_PATH="/packages/other"\n')
        before = (self.source / ".config").read_bytes()
        with self.assertRaisesRegex(ValueError, "only PY32F403_SDK and CMSIS_CORE"):
            download.configure_versions(self.source)
        self.assertEqual((self.source / ".config").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
