#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real Env package installer in a fresh, isolated BSP copy.

By default a newly exported candidate ZIP is served on loopback HTTP. This
checks the transport, unpacking and HAL SDK bridge, not public availability or
upstream acceptance. --sdk-url and --sdk-sha256 instead check a published ZIP.
CMSIS-Core is always fetched by Env from its public Git URL at the pinned SHA.
The output directory must not exist. No source SDK, BSP or Env files are edited.
"""

import argparse
from contextlib import contextmanager
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit
from urllib.request import urlopen
import uuid
import zipfile

from export_package import ROOT, export_package, is_below, is_link
from prepare import (CMSIS_REVISION, ENV_SCRIPTS_REVISION, INDEX_REVISION,
                     SDK_INDEX, literal, require_revision, write)

CORE_INDEX = Path("system/CMSIS/CMSIS-Core")
CORE_VERSION = "pinned-" + CMSIS_REVISION[:12]
SDK_VERSION = "1.4.5-candidate"
SKIP_NAMES = {".git", "__pycache__", "build", "dist", "packages", "vendor"}
SKIP_SUFFIXES = {".o", ".obj", ".elf", ".bin", ".axf", ".map", ".pyc", ".pyo"}


def reject_links(path):
    """Do not traverse a symlink/junction, including in the supplied root."""
    absolute = Path(os.path.abspath(path))
    for parent in (*reversed(absolute.parents), absolute):
        if parent.exists() and is_link(parent):
            raise ValueError(f"Links/junctions are not allowed: {parent}")


def new_output(path, source):
    path = Path(os.path.abspath(path))
    reject_links(path)
    resolved = path.resolve()
    if path.exists():
        raise ValueError(f"Output must be a new directory: {path}")
    if is_below(resolved, source.resolve()):
        raise ValueError("Output must not be inside the source BSP")
    if is_below(resolved, ROOT) and not is_below(resolved, ROOT / ".cache"):
        raise ValueError("Output inside the SDK checkout is allowed only under .cache/")
    return path


def copy_clean_bsp(source, target):
    """Copy source BSP and series helpers while excluding all installed packages."""
    reject_links(source)
    if not (source / ".config").is_file() or not (source / "SConstruct").is_file():
        raise ValueError("--bsp-source must contain .config and SConstruct")

    def ignore(directory, names):
        skipped = set()
        for name in names:
            path = Path(directory) / name
            if (name in SKIP_NAMES or name.startswith(".sconsign")
                    or name.startswith(("py32f403_sdk-", "CMSIS-Core-"))
                    or path.suffix.lower() in SKIP_SUFFIXES):
                skipped.add(name)
            elif is_link(path):
                raise ValueError(f"Unexpected BSP source link/junction: {path}")
        return skipped

    shutil.copytree(source, target, ignore=ignore)
    for name in ("libraries", "tools"):
        if not (target / name).exists() and (source.parent / name).is_dir():
            reject_links(source.parent / name)
            shutil.copytree(source.parent / name, target / name, ignore=ignore)
    if not (target / "libraries/Kconfig").is_file():
        raise ValueError("A PY32 libraries/Kconfig is required in or next to the source BSP")
    return adapt_copy_kconfig(target)


def adapt_copy_kconfig(bsp):
    """Adapt known PY32 Kconfig paths to the validation copy's layout.

    The upstream board keeps ../libraries/Kconfig. The validation copy puts
    libraries inside its BSP directory, as an exported distribution does.
    building.py supplies RTT_DIR from RTT_ROOT; use that environment value
    instead of the source board's now-invalid relative preprocessor variable.
    PKGS_DIR already uses environment expansion and needs no conversion.
    Return explicit provenance for the download report; never modify source.
    """
    kconfig = bsp / "Kconfig"
    content = kconfig.read_text(encoding="utf-8")
    includes = re.findall(r'^\s*rsource\s+"([^"\r\n]*libraries/Kconfig)"\s*$',
                          content, re.MULTILINE)
    if len(includes) != 1 or includes[0] not in {"../libraries/Kconfig", "libraries/Kconfig"}:
        raise ValueError("Unsupported BSP Kconfig libraries layout; expected one PY32 relative include")
    if not (bsp / "libraries/Kconfig").is_file():
        raise ValueError("Copied BSP libraries/Kconfig is missing")
    changes = []
    if includes[0] == "../libraries/Kconfig":
        content = re.sub(r'(^\s*rsource\s+)"\.\./libraries/Kconfig"(\s*$)',
                         r'\1"libraries/Kconfig"\2', content, count=1, flags=re.MULTILINE)
        changes.append({"file": "Kconfig", "from": 'rsource "../libraries/Kconfig"',
                        "to": 'rsource "libraries/Kconfig"',
                        "reason": "Validation copy keeps series libraries inside its BSP directory"})
    rtt_source = 'source "$(RTT_DIR)/Kconfig"'
    if rtt_source in content:
        declarations = re.findall(r'^RTT_DIR\s*:=\s*([^\r\n]+)$', content, re.MULTILINE)
        if len(declarations) != 1 or declarations[0] not in {"../../..", "rt-thread"}:
            raise ValueError("Unsupported BSP Kconfig RTT_DIR layout")
        content = re.sub(r'^RTT_DIR\s*:=\s*[^\r\n]+$',
                         "# RTT_DIR comes from building.py for this validation copy.",
                         content, count=1, flags=re.MULTILINE)
        content = content.replace(rtt_source, 'source "$RTT_DIR/Kconfig"', 1)
        changes.append({"file": "Kconfig", "from": "RTT_DIR := " + declarations[0] + "; " + rtt_source,
                        "to": 'source "$RTT_DIR/Kconfig" (environment supplied by building.py)',
                        "reason": "The copied board no longer shares the source board's relative RT-Thread root"})
    if changes:
        write(kconfig, content)
    return changes


def configure_versions(bsp):
    config = bsp / ".config"
    content = config.read_text(encoding="utf-8")
    packages = set(re.findall(r'^CONFIG_PKG_(.+)_PATH=', content, re.MULTILINE))
    if packages != {"PY32F403_SDK", "CMSIS_CORE"}:
        raise ValueError("Download validation requires only PY32F403_SDK and CMSIS_CORE packages")
    for name, version in (("PY32F403_SDK", SDK_VERSION), ("CMSIS_CORE", CORE_VERSION)):
        content, count = re.subn(r'^CONFIG_PKG_' + name + r'_VER="[^"\r\n]+"$',
                                f'CONFIG_PKG_{name}_VER="{version}"', content,
                                flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Missing/duplicate package version: {name}")
    write(config, content)


def archive_manifest(archive):
    """Reject incompatible or unsafe archives before giving them to Env."""
    with zipfile.ZipFile(archive) as package:
        files = {}
        roots = set()
        for entry in package.infolist():
            path = PurePosixPath(entry.filename)
            if ("\\" in entry.filename or path.is_absolute() or ".." in path.parts
                    or any(":" in part for part in path.parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError(f"Unsafe ZIP entry: {entry.filename}")
            if entry.is_dir():
                continue
            if len(path.parts) < 2:
                raise ValueError("Env ZIP packages require one top-level directory")
            roots.add(path.parts[0])
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in files:
                raise ValueError(f"Duplicate ZIP entry: {relative}")
            files[relative] = package.read(entry)
        if len(roots) != 1:
            raise ValueError("Env ZIP packages require one top-level directory")
    if "package-manifest.json" not in files:
        raise ValueError("The SDK ZIP must contain package-manifest.json")
    manifest = json.loads(files.pop("package-manifest.json"))
    if manifest.get("format") != 1 or manifest.get("package") != "py32f403_sdk":
        raise ValueError("Unexpected SDK package manifest")
    listed = set()
    for entry in manifest["files"]:
        name = entry["path"]
        if name in listed:
            raise ValueError(f"Duplicate manifest entry: {name}")
        listed.add(name)
        data = files.get(name)
        if (data is None or len(data) != entry["size"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]):
            raise ValueError(f"Manifest mismatch: {name}")
    if listed != set(files):
        raise ValueError("Manifest does not describe the exact ZIP file set")
    return manifest


def prepare_index(env_root, target, sdk_url):
    baseline = env_root / "packages/packages"
    require_revision(baseline, INDEX_REVISION, clean=False)
    # git archive reads precisely the pinned tree and never modifies the source index.
    data = subprocess.check_output(["git", "-c", "safe.directory=" + baseline.as_posix(),
                                    "-C", str(baseline), "archive", "--format=zip", INDEX_REVISION])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(target / "packages")
    parent = target / "packages/peripherals/hal-sdk/Kconfig"
    include = 'source "$PKGS_DIR/packages/peripherals/hal-sdk/puya/py32f403_sdk/Kconfig"'
    content = parent.read_text(encoding="utf-8")
    if include not in content:
        write(parent, content.replace("endmenu", include + "\nendmenu", 1))
    sdk_kconfig = (ROOT / "tools/package/Kconfig").read_text(encoding="utf-8")
    sdk_kconfig = sdk_kconfig.replace('"latest"', '"' + SDK_VERSION + '"')
    sdk_kconfig = sdk_kconfig.replace("latest (fixed candidate commit in the test index)",
                                     SDK_VERSION + " (verified ZIP)")
    write(target / "packages" / SDK_INDEX / "Kconfig", sdk_kconfig)
    metadata = json.loads((ROOT / "tools/package/package.json.in").read_text(encoding="utf-8"))
    metadata["site"] = [{"version": SDK_VERSION, "URL": sdk_url, "filename": "py32f403_sdk-validation.zip"}]
    write(target / "packages" / SDK_INDEX / "package.json", json.dumps(metadata, indent=2) + "\n")
    core_dir = target / "packages" / CORE_INDEX
    core = json.loads((core_dir / "package.json").read_text(encoding="utf-8"))
    site = next(item for item in core["site"] if item["version"] == "latest")
    site.update(version=CORE_VERSION, VER_SHA=CMSIS_REVISION)
    core["site"] = [site]
    write(core_dir / "package.json", json.dumps(core, indent=2) + "\n")
    write(core_dir / "Kconfig", (core_dir / "Kconfig").read_text(encoding="utf-8").replace('"latest"', '"' + CORE_VERSION + '"'))
    write(target / "Kconfig", 'source "$PKGS_DIR/packages/Kconfig"\n')
    return site["URL"]


@contextmanager
def serve_archive(archive):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if urlsplit(self.path).path != "/" + archive.name:
                self.send_error(404)
                return
            super().do_GET()

    handler = partial(QuietHandler, directory=str(archive.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{archive.name}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def verify_installation(bsp, scripts, manifest):
    sdk = bsp / "libraries" / ("py32f403_sdk-" + SDK_VERSION)
    core = bsp / "packages" / ("CMSIS-Core-" + CORE_VERSION)
    for root in (sdk, core):
        reject_links(root)
        if not root.is_dir():
            raise ValueError(f"Env did not install the expected package directory: {root}")
        for parent, directories, files in os.walk(root, followlinks=False):
            for name in directories + files:
                if is_link(Path(parent) / name):
                    raise ValueError(f"Installed package contains a link/junction: {Path(parent) / name}")
    actual = {file.relative_to(sdk).as_posix() for file in sdk.rglob("*") if file.is_file()}
    expected = {item["path"] for item in manifest["files"]} | {"package-manifest.json"}
    if actual != expected:
        raise ValueError("Installed SDK file set differs from the archive manifest")
    for entry in manifest["files"]:
        data = (sdk / entry["path"]).read_bytes()
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"Installed SDK hash mismatch: {entry['path']}")
    if json.loads((sdk / "package-manifest.json").read_text(encoding="utf-8")) != manifest:
        raise ValueError("Installed SDK manifest differs from the verified archive")
    require_revision(core, CMSIS_REVISION)
    if not (core / "Include/core_cm4.h").is_file():
        raise ValueError("CMSIS-Core installation is incomplete")
    bridge = bsp / "packages" / sdk.name
    expected_bridge = literal(scripts / "cmds/cmd_package/cmd_package_update.py", "HAL_SDK_BRIDGE_SCONSCRIPT")
    if (bridge / "SConscript").read_text(encoding="utf-8") != expected_bridge:
        raise ValueError("Installed SDK bridge differs from the pinned Env bridge")
    if set(path.name for path in bridge.iterdir()) != {"SConscript"}:
        raise ValueError("Expected only the Env SConscript bridge in BSP packages")
    if json.loads((bsp / "packages/pkgs_error.json").read_text(encoding="utf-8")):
        raise ValueError("Env recorded package download failures")
    return sdk, core


def install(args, work, archive, manifest, sdk_url, mode):
    bsp = work / "bsp"
    scripts = args.env_root / "tools/scripts"
    copy_adaptations = copy_clean_bsp(args.bsp_source, bsp)
    configure_versions(bsp)
    index = work / "index"
    core_url = prepare_index(args.env_root, index, sdk_url)
    isolated_env = work / "env"
    (isolated_env / "local_pkgs").mkdir(parents=True)
    environment = dict(os.environ, ENV_ROOT=str(isolated_env), PKGS_ROOT=str(index),
                       PKGS_DIR=str(index), PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
    command = [str(args.python), str(scripts / "env.py"), "pkgs", "--update"]
    with (work / "pkgs-update.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=bsp, env=environment, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=args.timeout)
    # Env may exit 0 even after installation failure; independently audit all outputs.
    downloaded = isolated_env / "local_pkgs/py32f403_sdk-validation.zip"
    expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if hashlib.sha256(downloaded.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("Env downloaded SDK ZIP differs from the verified SHA-256")
    sdk, core = verify_installation(bsp, scripts, manifest)
    result = {"status": "passed", "scope": mode, "public_sdk_validated": mode == "public-sdk-zip",
              "upstream_index_validated": False, "env_scripts": ENV_SCRIPTS_REVISION,
              "index_baseline": INDEX_REVISION, "index": str(index),
              "sdk_url": sdk_url, "sdk_sha256": expected_digest,
              "sdk_manifest": manifest, "sdk_installation": str(sdk),
              "cmsis_url": core_url, "cmsis_revision": CMSIS_REVISION,
              "cmsis_installation": str(core), "package_links": False,
              "command": command, "source_bsp": str(args.bsp_source),
              "bsp_copy_adaptations": copy_adaptations,
              "bsp": str(bsp), "build_validated": False}
    write(work / "download-validation.json", json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-root", required=True, type=Path)
    parser.add_argument("--bsp-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="a new isolated directory")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Python with Env dependencies installed")
    parser.add_argument("--sdk-url", help="published HTTP(S) ZIP, with package-manifest.json")
    parser.add_argument("--sdk-sha256", help="required immutable digest with --sdk-url")
    parser.add_argument("--timeout", type=int, default=300, help="Env subprocess timeout in seconds")
    args = parser.parse_args()
    try:
        if bool(args.sdk_url) != bool(args.sdk_sha256):
            raise ValueError("--sdk-url and --sdk-sha256 must be provided together")
        if args.sdk_url and (urlsplit(args.sdk_url).scheme not in {"http", "https"}
                            or not re.fullmatch(r"[0-9a-fA-F]{64}", args.sdk_sha256)):
            raise ValueError("A HTTP(S) URL and full SHA-256 digest are required")
        reject_links(args.bsp_source)
        args.env_root, args.bsp_source = args.env_root.resolve(), args.bsp_source.resolve()
        require_revision(args.env_root / "tools/scripts", ENV_SCRIPTS_REVISION)
        work = new_output(args.output, args.bsp_source)
        work.mkdir(parents=True)
        archive = work / "sdk.zip"
        if args.sdk_url:
            with urlopen(args.sdk_url, timeout=60) as response, archive.open("wb") as target:
                shutil.copyfileobj(response, target)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != args.sdk_sha256.lower():
                raise ValueError("Published SDK SHA-256 mismatch")
            manifest = archive_manifest(archive)
            result = install(args, work, archive, manifest, args.sdk_url, "public-sdk-zip")
        else:
            # Exporter restricts checkout-local outputs to dist/, then serve only
            # the copied archive from the isolated workspace on loopback.
            exported = export_package(output=ROOT / "dist" / ("py32f403_sdk-download-" + uuid.uuid4().hex + ".zip"))
            shutil.copyfile(exported["archive"], archive)
            manifest = archive_manifest(archive)
            with serve_archive(archive) as sdk_url:
                result = install(args, work, archive, manifest, sdk_url, "local-http-fixture")
        print(json.dumps({key: value for key, value in result.items() if key != "sdk_manifest"}, indent=2))
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
