#!/usr/bin/env python3
"""Compatibility wrapper for the built-in model manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mi_remote_linux.model_manager import MODEL_REPOSITORY, ModelManager, default_model_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 MiRemote 默认 Paraformer 模型")
    parser.add_argument("--target", type=Path, default=default_model_dir())
    parser.add_argument("--force", action="store_true", help="覆盖已经下载的文件")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    print(f"模型来源: https://huggingface.co/{MODEL_REPOSITORY} (Apache-2.0)")
    result = ModelManager(target=target).download(force=args.force)
    for name in result.skipped:
        print(f"已校验，跳过: {name}")
    for name in result.downloaded:
        print(f"已下载并校验: {name}")
    print(f"完成。模型目录: {target}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130) from None
