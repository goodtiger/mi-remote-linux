#!/usr/bin/env python3
"""Install a verified MiRemote Linux release into a versioned user venv."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import NamedTuple

REPOSITORY_URL = "https://github.com/goodtiger/mi-remote-linux"
TAG_PATTERN = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseAssets(NamedTuple):
    tag: str
    version: str
    wheel_name: str
    wheel_url: str
    checksums_url: str


def release_assets(tag: str) -> ReleaseAssets:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("版本必须使用 vMAJOR.MINOR.PATCH 格式")
    version = tag[1:]
    wheel_name = f"mi_remote_linux-{version}-py3-none-any.whl"
    base_url = f"{REPOSITORY_URL}/releases/download/{tag}"
    return ReleaseAssets(
        tag,
        version,
        wheel_name,
        f"{base_url}/{wheel_name}",
        f"{base_url}/SHA256SUMS",
    )


def parse_checksums(content: str) -> dict[str, str]:
    checksums = {}
    for line in content.splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not SHA256_PATTERN.fullmatch(parts[0]):
            raise ValueError("SHA256SUMS 格式无效")
        name = parts[1].lstrip("*")
        if Path(name).name != name:
            raise ValueError(f"SHA256SUMS 包含不安全的文件名：{name}")
        if name in checksums:
            raise ValueError(f"SHA256SUMS 包含重复文件名：{name}")
        checksums[name] = parts[0]
    return checksums


def verify_file(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise ValueError(f"{path.name} SHA-256 校验失败：{actual} != {expected}")


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "mi-remote-linux-installer"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def default_paths(environment: dict[str, str]) -> tuple[Path, Path]:
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    data_home = Path(environment.get("XDG_DATA_HOME", home / ".local/share")).expanduser()
    bin_home = Path(environment.get("XDG_BIN_HOME", home / ".local/bin")).expanduser()
    return data_home / "mi-remote-linux", bin_home


def _link_is_managed(command_path: Path, install_root: Path) -> bool:
    if not command_path.exists() and not command_path.is_symlink():
        return True
    if not command_path.is_symlink():
        return False
    try:
        command_path.resolve(strict=False).relative_to(install_root.resolve())
    except ValueError:
        return False
    return True


def install(
    assets: ReleaseAssets,
    *,
    python: str,
    with_voice: bool,
    install_root: Path,
    bin_home: Path,
) -> Path:
    versions_dir = install_root / "versions"
    target = versions_dir / assets.version
    command_path = bin_home / "mi-remote"
    if target.exists():
        raise RuntimeError(f"版本目录已存在，未覆盖：{target}")
    if not _link_is_managed(command_path, install_root):
        raise RuntimeError(f"拒绝覆盖非 MiRemote 管理的命令：{command_path}")

    with tempfile.TemporaryDirectory(prefix="mi-remote-download-") as download_dir:
        download_path = Path(download_dir)
        checksums_path = download_path / "SHA256SUMS"
        wheel_path = download_path / assets.wheel_name
        print(f"下载校验清单：{assets.checksums_url}")
        download(assets.checksums_url, checksums_path)
        checksums = parse_checksums(checksums_path.read_text(encoding="utf-8"))
        expected = checksums.get(assets.wheel_name)
        if expected is None:
            raise RuntimeError(f"SHA256SUMS 未包含 {assets.wheel_name}")
        print(f"下载 wheel：{assets.wheel_url}")
        download(assets.wheel_url, wheel_path)
        verify_file(wheel_path, expected)
        print("SHA-256 校验通过")

        versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{assets.version}.install-", dir=versions_dir))
        try:
            subprocess.run([python, "-m", "venv", str(temporary)], check=True)
            pip = temporary / "bin/pip"
            requirement = wheel_path.resolve().as_uri()
            if with_voice:
                requirement = f"mi-remote-linux[voice] @ {requirement}"
            subprocess.run([str(pip), "install", "--upgrade", requirement], check=True)
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    bin_home.mkdir(parents=True, exist_ok=True)
    temporary_link = bin_home / f".mi-remote.{os.getpid()}.tmp"
    temporary_link.unlink(missing_ok=True)
    try:
        temporary_link.symlink_to(target / "bin/mi-remote")
        os.replace(temporary_link, command_path)
    finally:
        temporary_link.unlink(missing_ok=True)
    return command_path


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并安装 MiRemote Linux GitHub Release")
    parser.add_argument("--version", required=True, help="发行标签，格式为 vMAJOR.MINOR.PATCH")
    parser.add_argument("--python", default=sys.executable, help="用于创建 venv 的 Python 3")
    parser.add_argument("--no-voice", action="store_true", help="不安装本地语音识别依赖")
    parser.add_argument("--dry-run", action="store_true", help="只显示路径和下载地址")
    args = parser.parse_args()

    try:
        assets = release_assets(args.version)
        install_root, bin_home = default_paths(dict(os.environ))
        target = install_root / "versions" / assets.version
        print(f"版本：{assets.tag}")
        print(f"安装目录：{target}")
        print(f"命令入口：{bin_home / 'mi-remote'}")
        print(f"wheel：{assets.wheel_url}")
        if args.dry_run:
            print("预览完成，不会修改系统。")
            return 0
        command_path = install(
            assets,
            python=args.python,
            with_voice=not args.no_voice,
            install_root=install_root,
            bin_home=bin_home,
        )
        print(f"安装完成：{command_path}")
        print("请运行 mi-remote doctor 检查桌面依赖和设备权限。")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"安装失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
