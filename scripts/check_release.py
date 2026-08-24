#!/usr/bin/env python3
"""Fail a release unless its stable SemVer tag matches the package version."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mi_remote_linux import __version__

TAG_PATTERN = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def check_release(tag: str) -> str:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("发行标签必须使用 vMAJOR.MINOR.PATCH 格式")
    expected = f"v{__version__}"
    if tag != expected:
        raise ValueError(f"发行标签 {tag} 与包版本 {__version__} 不匹配（应为 {expected}）")
    return f"发行版本校验通过：{tag}"


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) == 2 else os.environ.get("GITHUB_REF_NAME", "")
    if len(sys.argv) > 2:
        print("用法：check_release.py vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    try:
        print(check_release(tag))
    except ValueError as exc:
        print(f"发行校验失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
