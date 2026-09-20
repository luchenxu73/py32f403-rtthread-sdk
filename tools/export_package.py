#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Export the PY32F403 SDK as a deterministic, trimmed package ZIP.

An export is a candidate unless --release is explicitly requested. Release
exports require a clean Git checkout. No network access is performed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SDK = "sdk/py32f403"
REQUIRED_FILES = (
    "SConscript", "LICENSE", "README.md",
    SDK + "/SConscript", SDK + "/README.md",
)
INCLUDED_DIRECTORIES = tuple(SDK + "/" + name for name in (
    "RTThread", "Templates", "Drivers",
))
EXCLUDED_DIRECTORIES = tuple(SDK + "/Drivers/" + name for name in (
    "BSP", "CMSIS/Include", "CMSIS/RTOS", "CMSIS/RTOS2",
))
EXCLUDED_FILE_SUFFIXES = (".o", ".obj", ".d", ".dep", ".elf", ".axf", ".map", ".lst", ".su", ".pyc", ".pyo")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def is_below(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_link(path):
    """Include Windows junctions on Python versions without Path.is_junction."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def check_source_path(path, root):
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if is_link(current):
            raise ValueError(f"Source links/junctions are not allowed: {current}")


def collect_files(root):
    files = {}
    for relative in REQUIRED_FILES:
        path = root / relative
        check_source_path(path, root)
        if not path.is_file():
            raise ValueError(f"Required package file is missing: {path}")
        files[relative] = path

    for relative in INCLUDED_DIRECTORIES:
        directory = root / relative
        check_source_path(directory, root)
        if not directory.is_dir():
            raise ValueError(f"Required package directory is missing: {directory}")
        for parent, directories, names in os.walk(directory, followlinks=False):
            parent = Path(parent)
            retained = []
            for name in sorted(directories):
                child = parent / name
                child_relative = child.relative_to(root).as_posix()
                if child_relative in EXCLUDED_DIRECTORIES:
                    continue
                check_source_path(child, root)
                retained.append(name)
            directories[:] = retained
            for name in sorted(names):
                child = parent / name
                if child.suffix.lower() in EXCLUDED_FILE_SUFFIXES or name.startswith(".sconsign"):
                    continue
                check_source_path(child, root)
                if not child.is_file():
                    raise ValueError(f"Package source is not a regular file: {child}")
                files[child.relative_to(root).as_posix()] = child
    return dict(sorted(files.items()))


def git_metadata(root):
    def run(*args):
        return subprocess.check_output(
            ["git", "-c", "safe.directory=" + root.as_posix(), "-C", str(root), *args],
            stderr=subprocess.DEVNULL,
        )

    try:
        top = Path(os.fsdecode(run("rev-parse", "--show-toplevel").strip())).resolve()
        if top != root:
            raise ValueError("Package source root must be the Git repository root")
        commit = run("rev-parse", "HEAD").decode("ascii").strip()
        status = run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        tracked = {os.fsdecode(name) for name in run("ls-files", "-z").split(b"\0") if name}
        return {"git_commit": commit, "dirty": bool(status)}, tracked
    except (OSError, subprocess.CalledProcessError):
        # A source archive can still produce a candidate, but never a release.
        return {"git_commit": None, "dirty": None}, set()


def validate_outputs(root, output, force):
    output = Path(os.path.abspath(output))
    if output.suffix.lower() != ".zip":
        raise ValueError("Package output must have a .zip suffix")
    outputs = (output, Path(str(output) + ".manifest.json"), Path(str(output) + ".sha256"))
    for path in outputs:
        resolved = path.resolve()
        if is_below(resolved, root) and not is_below(resolved, root / "dist"):
            raise ValueError("Output inside the source checkout is allowed only under dist/")
        if path.exists() or path.is_symlink():
            if is_link(path) or not path.is_file():
                raise ValueError(f"Refusing to replace output link/directory: {path}")
            if not force:
                raise ValueError(f"Output already exists (use --force): {path}")
    return outputs


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def export_package(root=ROOT, output=None, version="1.4.5", release=False, force=False):
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("Version must contain only letters, digits, dots, underscores, or hyphens")
    kind = "release" if release else "candidate"
    # Env's ZIP installer takes the first path component as the package
    # directory, then renames that directory to <package>-<selected version>.
    # A flat archive would make it treat the first file as that directory.
    archive_prefix = f"py32f403_sdk-{version}/"
    if output is None:
        output = root / "dist" / f"py32f403_sdk-{version}-{kind}.zip"
    outputs = validate_outputs(root, output, force)
    source_files = collect_files(root)
    source, tracked = git_metadata(root)
    if release:
        if source["git_commit"] is None or source["dirty"] is not False:
            raise ValueError("Release export requires a known, clean Git checkout; use a candidate export")
        untracked = set(source_files) - tracked
        if untracked:
            raise ValueError("Release export contains untracked/ignored sources: " + ", ".join(sorted(untracked)))

    # Snapshot source bytes once; hashes and the archive always describe the
    # same bytes even if a developer edits a file while the ZIP is written.
    contents = {name: path.read_bytes() for name, path in source_files.items()}
    after, tracked_after = git_metadata(root)
    if after != source or tracked_after != tracked:
        raise ValueError("Git state changed while collecting package sources; retry the export")
    manifest = {
        "format": 1,
        "package": "py32f403_sdk",
        "vendor_firmware": "1.4.5",
        "version": version,
        "kind": kind,
        "archive_prefix": archive_prefix,
        "source": source,
        "excluded_directories": list(EXCLUDED_DIRECTORIES),
        "excluded_file_suffixes": list(EXCLUDED_FILE_SUFFIXES),
        "files": [
            {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in contents.items()
        ],
    }
    metadata = json_bytes(manifest)
    contents["package-manifest.json"] = metadata
    outputs[0].parent.mkdir(parents=True, exist_ok=True)
    temporary_paths = []
    try:
        for path in outputs:
            descriptor, temporary = tempfile.mkstemp(prefix=".py32-export-", dir=path.parent)
            os.close(descriptor)
            temporary_paths.append(Path(temporary))
        with zipfile.ZipFile(temporary_paths[0], "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for name in sorted(contents):
                info = zipfile.ZipInfo(archive_prefix + name, ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, contents[name], compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=9)
        digest = hashlib.sha256(temporary_paths[0].read_bytes()).hexdigest()
        temporary_paths[1].write_bytes(metadata)
        temporary_paths[2].write_text(f"{digest}  {outputs[0].name}\n", encoding="utf-8", newline="\n")
        for temporary, destination in zip(temporary_paths, outputs):
            os.replace(temporary, destination)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
    return {"archive": str(outputs[0]), "manifest": str(outputs[1]),
            "checksum": str(outputs[2]), "sha256": digest,
            "files": len(source_files), "kind": kind, "source": source,
            "archive_prefix": archive_prefix}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, help="ZIP filename; defaults to source-root/dist/")
    parser.add_argument("--version", default="1.4.5")
    parser.add_argument("--release", action="store_true", help="require a clean, tracked Git source")
    parser.add_argument("--force", action="store_true", help="replace existing export artifacts")
    args = parser.parse_args()
    try:
        result = export_package(args.source_root, args.output, args.version, args.release, args.force)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
