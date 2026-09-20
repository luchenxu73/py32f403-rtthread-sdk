#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Prepare an isolated Env index and editable SDK package for a PY32 BSP."""

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CMSIS_REVISION = "39d8e01f0be84b83a8f11d33756e82ce1ef07a84"
INDEX_REVISION = "16ed99eba0a7897311972254bafe0667afb145ee"
ENV_SCRIPTS_REVISION = "0fd51234e31a6bc8f901aadd7b7dbd633b774527"
SDK_INDEX = Path("peripherals/hal-sdk/puya/py32f403_sdk")
SDK_GIT_URL = "https://github.com/luchenxu73/py32-rtthread-sdk.git"
SDK_AUTHOR_EMAIL = "luchenxu73@gmail.com"


def git(path, *args):
    repository = path.resolve()
    while not (repository / ".git").exists() and repository.parent != repository:
        repository = repository.parent
    return subprocess.check_output(
        ["git", "-c", "safe.directory=" + repository.as_posix(), "-C", str(path), *args],
        text=True,
    ).strip()


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def literal(path, name):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"{name} missing from {path}")


def require_revision(path, revision, clean=True):
    actual = git(path, "rev-parse", "HEAD")
    if actual != revision:
        raise ValueError(f"{path}: expected {revision}, found {actual}")
    if clean and git(path, "status", "--porcelain"):
        raise ValueError(f"Pinned dependency has local modifications: {path}")


def prepare_index(args):
    sdk_url = args.sdk_url or SDK_GIT_URL
    if sdk_url != SDK_GIT_URL and not args.sdk_revision:
        raise ValueError("A custom --sdk-url requires --sdk-revision")
    if args.sdk_revision and not re.fullmatch(r"[0-9a-fA-F]{40}", args.sdk_revision):
        raise ValueError("--sdk-revision must be a full, immutable Git SHA")
    if not sdk_url.endswith('.git'):
        raise ValueError("--sdk-url must end in .git for Env's Git package installer")
    if args.sdk_revision and not getattr(args, 'author_email', None):
        raise ValueError("--author-email is required for a downloadable package index")
    source = args.env_root / "packages/packages"
    require_revision(source, INDEX_REVISION)
    target = args.index_root / "packages"
    if target.exists():
        require_revision(target, INDEX_REVISION, clean=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "https://github.com/RT-Thread/packages.git",
                        str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "--detach", INDEX_REVISION], check=True)
    parent = target / "peripherals/hal-sdk/Kconfig"
    content = parent.read_text(encoding="utf-8")
    entry = 'source "$PKGS_DIR/packages/peripherals/hal-sdk/puya/py32f403_sdk/Kconfig"'
    if entry not in content:
        write(parent, content.replace("endmenu", entry + "\nendmenu", 1))
    write(target / SDK_INDEX / "Kconfig", (ROOT / "tools/package/Kconfig").read_text(encoding="utf-8"))
    write(args.index_root / "Kconfig", 'source "$PKGS_DIR/packages/Kconfig"\n')
    if args.sdk_revision:
        data = json.loads((ROOT / "tools/package/package.json.in").read_text(encoding="utf-8"))
        data['author']['email'] = args.author_email
        repository = sdk_url.removesuffix(".git")
        data.update(repository=repository, homepage=repository + "#readme", doc=repository + "#readme")
        data["site"][0].update(URL=sdk_url, VER_SHA=args.sdk_revision)
        write(target / SDK_INDEX / "package.json", json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"PKGS_DIR={args.index_root}")
    if not args.sdk_revision:
        metadata = target / SDK_INDEX / "package.json"
        if metadata.exists():
            print("Retained existing download mapping: " + str(metadata))
        else:
            print("Configuration-only SDK index: no downloadable SDK revision supplied.")


def link_directory(source, target):
    source = source.resolve()
    if target.exists():
        if target.resolve() != source:
            raise ValueError(f"Refusing to replace existing package: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Literal quoted arguments; do not invoke cmd.exe or concatenate shell commands.
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = "New-Item -ItemType Junction -Path " + quote(target) + " -Target " + quote(source)
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], check=True)
    else:
        target.symlink_to(source, target_is_directory=True)


def config_value(bsp, name):
    text = (bsp / ".config").read_text(encoding="utf-8")
    match = re.search(r'^CONFIG_' + re.escape(name) + r'="([^"/\\]+)"$', text, re.MULTILINE)
    if not match:
        raise ValueError(f"{name} missing from {bsp / '.config'}")
    return match.group(1)


def prepare_local(args):
    scripts = args.env_root / "tools/scripts"
    require_revision(scripts, ENV_SCRIPTS_REVISION)
    bsp = args.bsp.resolve()
    libraries = bsp.parent / "libraries"
    if not (libraries / "Kconfig").is_file():
        raise ValueError(f"Expected PY32 series libraries/Kconfig next to {bsp}")
    if not (ROOT / "SConscript").is_file():
        raise ValueError("SDK root SConscript is missing")
    cmsis = args.cmsis_source.resolve()
    if not cmsis.exists():
        cmsis.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "https://github.com/RT-Thread-packages/CMSIS-Core.git", str(cmsis)], check=True)
        subprocess.run(["git", "-C", str(cmsis), "checkout", "--detach", CMSIS_REVISION], check=True)
    require_revision(cmsis, CMSIS_REVISION)
    if not (cmsis / "Include/core_cm4.h").is_file():
        raise ValueError("CMSIS-Core package is incomplete")
    sdk_name = "py32f403_sdk-" + config_value(bsp, "PKG_PY32F403_SDK_VER")
    core_name = "CMSIS-Core-" + config_value(bsp, "PKG_CMSIS_CORE_VER")
    bridge = literal(scripts / "cmds/cmd_package/cmd_package_update.py", "HAL_SDK_BRIDGE_SCONSCRIPT")
    package_entry = literal(scripts / "package.py", "Bridge_SConscript")
    bridge_file = bsp / "packages" / sdk_name / "SConscript"
    if bridge_file.exists() and bridge_file.read_text(encoding="utf-8") != bridge:
        raise ValueError(f"Refusing to replace existing package bridge: {bridge_file}")
    entry_file = bsp / "packages/SConscript"
    if entry_file.exists() and entry_file.read_text(encoding="utf-8") != package_entry:
        raise ValueError(f"Existing package entry differs from pinned Env: {entry_file}")
    link_directory(ROOT, libraries / sdk_name)
    link_directory(cmsis, bsp / "packages" / core_name)
    write(bridge_file, bridge)
    if not entry_file.exists():
        write(entry_file, package_entry)
    manifest = {
        "mode": "editable-local-sdk",
        "rt_thread": git(bsp, "rev-parse", "HEAD"),
        "sdk_base": git(ROOT, "rev-parse", "HEAD"),
        "sdk_dirty": bool(git(ROOT, "status", "--porcelain")),
        "sdk_source": str(ROOT),
        "cmsis_core": CMSIS_REVISION,
        "env_scripts": ENV_SCRIPTS_REVISION,
        "configured_index_baseline": INDEX_REVISION,
    }
    write(bsp / "packages/py32-local.json", json.dumps(manifest, indent=2) + "\n")
    print("Editable SDK connected. Do not run pkgs --update in this development directory.")
    print("Use a separate clean BSP to validate the real package download workflow.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["index", "local"])
    parser.add_argument("--env-root", required=True, type=Path)
    parser.add_argument("--index-root", type=Path, default=ROOT / ".cache/package-index")
    parser.add_argument("--sdk-url", default=SDK_GIT_URL, help="SDK Git repository; defaults to the maintainer repository")
    parser.add_argument("--sdk-revision", help="Full Git SHA already published to the SDK repository; omit for a configuration-only index")
    parser.add_argument("--author-email", default=SDK_AUTHOR_EMAIL,
                        help="Public maintainer email for the packages index")
    parser.add_argument("--bsp", type=Path)
    parser.add_argument("--cmsis-source", type=Path, default=ROOT / ".cache/CMSIS-Core")
    args = parser.parse_args()
    args.env_root = args.env_root.resolve()
    args.index_root = args.index_root.resolve()
    if args.command == "local" and not args.bsp:
        parser.error("local requires --bsp")
    try:
        (prepare_index if args.command == "index" else prepare_local)(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
