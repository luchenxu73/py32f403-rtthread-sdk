"""Exercise the package boundary, reproducibility and release safeguards."""
# SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "export_package.py"
SPEC = importlib.util.spec_from_file_location("export_package", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class ExportPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.expected = set(exporter.REQUIRED_FILES)
        self.expected.update((
            "sdk/RTThread/startup.S",
            "sdk/Templates/PY32F403xx_Templates/Src/system.c",
            "sdk/Drivers/PY32F403_HAL_Driver/Src/hal.c",
            "sdk/Drivers/CMSIS/Device/PUYA/PY32F403/Include/device.h",
            "sdk/Drivers/CMSIS/LICENSE.txt",
        ))
        for relative in self.expected:
            self.write(relative, relative + "\n")
        for directory in exporter.EXCLUDED_DIRECTORIES:
            self.write(directory + "/excluded.c", "never ship")
        self.write("docs/plan2.md", "development-only")
        self.write("tools/prepare.py", "development-only")
        self.write("sdk/Drivers/PY32F403_HAL_Driver/Src/hal.o", "build output")
        self.metadata = {"git_commit": "a" * 40, "dirty": True}
        self.mock = patch.object(exporter, "git_metadata", side_effect=self.get_metadata)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def get_metadata(self, root):
        return dict(self.metadata), set(self.expected)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_trimmed_layout_hashes_and_candidate_metadata(self):
        result = exporter.export_package(self.root)
        self.assertEqual(result["kind"], "candidate")
        self.assertTrue(result["source"]["dirty"])
        archive_path = Path(result["archive"])
        self.assertEqual(result["sha256"], hashlib.sha256(archive_path.read_bytes()).hexdigest())
        with zipfile.ZipFile(archive_path) as archive:
            prefix = "py32f403_sdk-1.4.5/"
            self.assertEqual(result["archive_prefix"], prefix)
            self.assertEqual(set(archive.namelist()), {
                prefix + name for name in self.expected | {"package-manifest.json"}
            })
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            manifest = json.loads(archive.read(prefix + "package-manifest.json"))
            self.assertEqual(manifest["archive_prefix"], prefix)
            self.assertEqual(manifest, json.loads(Path(result["manifest"]).read_bytes()))
            for entry in manifest["files"]:
                data = archive.read(prefix + entry["path"])
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(entry["size"], len(data))
            self.assertTrue(all(info.date_time == exporter.ZIP_TIME for info in archive.infolist()))
        self.assertEqual(Path(result["checksum"]).read_text().split()[0], result["sha256"])
        self.assertTrue((self.root / "sdk/Drivers/CMSIS/Include/excluded.c").exists())

    def test_env_single_root_extraction_contract(self):
        """Exercise Env's first-component extraction/rename contract on disk."""
        self.metadata["dirty"] = False
        for release in (False, True):
            with self.subTest(release=release):
                result = exporter.export_package(self.root, release=release)
                packages = Path(self.temporary.name) / ("release" if release else "candidate")
                staging = packages / "package_temp"
                with zipfile.ZipFile(result["archive"]) as archive:
                    folder_name = archive.namelist()[0].split("/")[0]
                    self.assertEqual({name.split("/")[0] for name in archive.namelist()},
                                     {folder_name})
                    archive.extractall(staging)
                extracted = staging / folder_name
                installed = packages / "py32f403_sdk-v1.4.5"
                self.assertTrue(extracted.is_dir())
                shutil.move(str(extracted), installed)
                self.assertEqual(
                    {path.relative_to(installed).as_posix() for path in installed.rglob("*")
                     if path.is_file()}, self.expected | {"package-manifest.json"})
                for relative in self.expected:
                    self.assertEqual((installed / relative).read_bytes(),
                                     (self.root / relative).read_bytes())

    def test_real_env_zip_installer_when_available(self):
        """Run the installed Env implementation, isolating only its package DB."""
        default_env = SCRIPT.parents[2] / "env-windows"
        env_root = Path(os.environ.get("ENV_ROOT", default_env))
        scripts = env_root / "tools/scripts"
        archive_script = scripts / "archive.py"
        if not archive_script.is_file():
            self.skipTest("Set ENV_ROOT to run the real Env ZIP installer integration test")
        spec = importlib.util.spec_from_file_location("env_archive_contract", archive_script)
        env_archive = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            spec.loader.exec_module(env_archive)
        result = exporter.export_package(self.root)
        packages = Path(self.temporary.name) / "env-packages"
        packages.mkdir()
        with patch.object(env_archive.pkgsdb, "save_to_database") as save_to_database:
            self.assertTrue(env_archive.handle_zip_package(
                result["archive"], str(packages), "py32f403_sdk", {"ver": "v1.4.5"}))
        installed = packages / "py32f403_sdk-v1.4.5"
        self.assertEqual(save_to_database.call_count, len(self.expected) + 1)
        self.assertFalse((packages / "package_temp").exists())
        self.assertTrue((installed / "SConscript").is_file())
        for relative in self.expected:
            self.assertEqual((installed / relative).read_bytes(),
                             (self.root / relative).read_bytes())

    def test_repeated_exports_are_byte_identical(self):
        first = exporter.export_package(self.root, self.root / "dist/one.zip")
        second = exporter.export_package(self.root, self.root / "dist/two.zip")
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        self.assertEqual(Path(first["manifest"]).read_bytes(), Path(second["manifest"]).read_bytes())

    def test_dirty_or_unknown_source_cannot_be_a_release(self):
        for source in ({"git_commit": "a" * 40, "dirty": True},
                       {"git_commit": None, "dirty": None}):
            self.metadata = source
            with self.assertRaisesRegex(ValueError, "clean Git"):
                exporter.export_package(self.root, release=True)
        self.assertFalse((self.root / "dist").exists())

    def test_clean_source_still_defaults_to_candidate_and_explicit_release_works(self):
        self.metadata["dirty"] = False
        candidate = exporter.export_package(self.root)
        release = exporter.export_package(self.root, release=True)
        self.assertEqual(candidate["kind"], "candidate")
        self.assertEqual(release["kind"], "release")

    def test_release_rejects_ignored_source_file(self):
        self.metadata["dirty"] = False
        self.write("sdk/Drivers/ignored.h", "not versioned")
        with self.assertRaisesRegex(ValueError, "untracked/ignored"):
            exporter.export_package(self.root, release=True)

    def test_refuses_source_output_and_existing_artifacts(self):
        with self.assertRaisesRegex(ValueError, "only under dist"):
            exporter.export_package(self.root, self.root / "sdk/archive.zip", force=True)
        first = exporter.export_package(self.root)
        with self.assertRaisesRegex(ValueError, "already exists"):
            exporter.export_package(self.root)
        replaced = exporter.export_package(self.root, force=True)
        self.assertEqual(first["sha256"], replaced["sha256"])

    def test_source_link_is_rejected(self):
        target = self.root / "sdk/Drivers/link.h"
        try:
            target.symlink_to(self.root / "README.md")
        except OSError as error:
            self.skipTest("Symlink creation unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "links/junctions"):
            exporter.export_package(self.root)

    def test_windows_junction_attribute_is_detected_without_symlink_privileges(self):
        attributes = SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=attributes):
            self.assertTrue(exporter.is_link(self.root / "sdk"))

    def test_state_change_while_exporting_is_rejected(self):
        self.mock.stop()
        states = [({"git_commit": "a" * 40, "dirty": False}, set(self.expected)),
                  ({"git_commit": "a" * 40, "dirty": True}, set(self.expected))]
        with patch.object(exporter, "git_metadata", side_effect=states):
            with self.assertRaisesRegex(ValueError, "Git state changed"):
                exporter.export_package(self.root, release=True)
        self.assertFalse((self.root / "dist").exists())

    def test_missing_required_file_fails_before_output(self):
        (self.root / "LICENSE").unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            exporter.export_package(self.root)
        self.assertFalse((self.root / "dist").exists())


if __name__ == "__main__":
    unittest.main()
