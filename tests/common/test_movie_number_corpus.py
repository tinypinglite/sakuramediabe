"""跨项目番号解析回归语料。

语料来自 JavSP / JavBoss / metatube-sdk-go / mdcz / javinizer-go 的公开测试用例，
收录范围：双方期望在大小写归一后一致的条目；本项目有意偏离的约定（零填充形态、
FC2 折叠、z 后缀等）见 ``test_movie_numbers.py`` 里的定向断言。
"""

from pathlib import Path

import pytest

from src.common.movie_numbers import parse_movie_number_from_text

CORPUS_PATH = Path(__file__).parent / "data" / "movie_number_corpus.txt"


def _load_corpus() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        filename, expected = line.split("\t")
        cases.append((filename, expected))
    return cases


@pytest.mark.parametrize(("filename", "expected"), _load_corpus())
def test_movie_number_corpus(filename: str, expected: str):
    assert parse_movie_number_from_text(filename) == expected
