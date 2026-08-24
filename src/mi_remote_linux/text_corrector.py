"""可配置的识别文本术语纠正。"""

from __future__ import annotations

import json
import re
from pathlib import Path


class TermsFileError(ValueError):
    """术语表不存在或格式无效。"""


class TextCorrector:
    """对一次识别结果执行不连锁的最长优先字面替换。"""

    def __init__(self, replacements: dict[str, str] | None = None):
        replacements = replacements or {}
        normalized: dict[str, str] = {}
        originals: dict[str, str] = {}
        for source, target in replacements.items():
            if not isinstance(source, str) or not source:
                raise TermsFileError("术语表的待替换文本必须是非空字符串")
            if not isinstance(target, str) or not target:
                raise TermsFileError(f"术语 {source!r} 的目标文本必须是非空字符串")
            key = source.casefold()
            if key in normalized:
                raise TermsFileError(
                    f"术语 {source!r} 与 {originals[key]!r} 仅大小写不同，会产生歧义"
                )
            normalized[key] = target
            originals[key] = source

        self._replacements = normalized
        ordered = sorted(originals.values(), key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(source) for source in ordered), re.IGNORECASE)
            if ordered
            else None
        )

    @classmethod
    def from_file(cls, path: str | Path) -> TextCorrector:
        terms_path = Path(path).expanduser()
        try:
            payload = json.loads(terms_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TermsFileError(f"无法读取术语表 {terms_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TermsFileError(f"术语表不是有效 JSON: {exc}") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("replacements"), dict):
            raise TermsFileError('术语表必须包含 JSON 对象字段 "replacements"')
        return cls(payload["replacements"])

    def apply(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(
            lambda match: self._replacements[match.group(0).casefold()],
            text,
        )
