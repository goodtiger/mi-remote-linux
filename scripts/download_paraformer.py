#!/usr/bin/env python3
"""下载项目默认使用的 Sherpa-ONNX Paraformer 中英文模型。"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

MODEL_REPOSITORY = "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14"
BASE_URL = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/main"
MODEL_FILES = ("model.int8.onnx", "tokens.txt")


def default_target() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "mi-remote-linux/models/paraformer-zh"


def download_file(name: str, target: Path, force: bool) -> None:
    destination = target / name
    if destination.is_file() and destination.stat().st_size > 0 and not force:
        print(f"已存在，跳过: {destination}")
        return

    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        f"{BASE_URL}/{name}", headers={"User-Agent": "mi-remote-linux"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length", 0))
            received = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                if total:
                    print(f"\r{name}: {received / total:6.1%}", end="", flush=True)
        partial.replace(destination)
        print(f"\r已下载: {destination} ({received / 1024 / 1024:.1f} MiB)")
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 MiRemote 默认 Paraformer 模型")
    parser.add_argument("--target", type=Path, default=default_target())
    parser.add_argument("--force", action="store_true", help="覆盖已经下载的文件")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(f"模型来源: https://huggingface.co/{MODEL_REPOSITORY} (Apache-2.0)")
    for name in MODEL_FILES:
        download_file(name, target, args.force)
    print(f"完成。模型目录: {target}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130) from None
