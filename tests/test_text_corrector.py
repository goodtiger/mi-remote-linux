"""识别文本术语纠正测试。"""

import json

import pytest

from mi_remote_linux.text_corrector import TermsFileError, TextCorrector


def test_replacements_are_longest_first_case_insensitive_and_non_cascading():
    corrector = TextCorrector(
        {
            "git": "版本库",
            "github": "GitHub",
            "python": "Python",
            "版本库": "不应连锁",
        }
    )

    assert (
        corrector.apply("提交到 GITHUB，运行 python 和 git")
        == "提交到 GitHub，运行 Python 和 版本库"
    )


def test_terms_file_is_loaded(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text(
        json.dumps({"replacements": {"电脑号本": "GitHub"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert TextCorrector.from_file(path).apply("提交到电脑号本") == "提交到GitHub"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"replacements": []},
        {"replacements": {"": "空键"}},
        {"replacements": {"正常": ""}},
    ],
)
def test_invalid_terms_file_is_rejected(tmp_path, payload):
    path = tmp_path / "terms.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TermsFileError):
        TextCorrector.from_file(path)
