from __future__ import annotations

import csv
import unicodedata
from functools import lru_cache
from pathlib import Path

from src.i18n.locale import current_language, normalize_language

_DATA_PATH = Path(__file__).with_name("data") / "tag_names.csv"
_TARGET_COLUMNS = {"zh-CN": "zh_cn", "en": "en"}
_SOURCE_COLUMNS = ("zh_cn", "zh_tw", "en")


def _normalize(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


@lru_cache(maxsize=1)
def _load_rows() -> tuple[dict[str, str], ...]:
    with _DATA_PATH.open(encoding="utf-8", newline="") as handle:
        return tuple(csv.DictReader(handle))


@lru_cache(maxsize=2)
def _build_lookup(target_language: str) -> dict[str, str]:
    target_column = _TARGET_COLUMNS[target_language]
    lookup: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in _load_rows():
        target_name = (row.get(target_column) or "").strip()
        if not target_name:
            continue
        for source_column in _SOURCE_COLUMNS:
            source_name = _normalize(row.get(source_column))
            if not source_name:
                continue
            existing = lookup.get(source_name)
            if existing is None:
                lookup[source_name] = target_name
            elif existing != target_name:
                ambiguous.add(source_name)
    for source_name in ambiguous:
        lookup.pop(source_name, None)
    return lookup


def resolve_tag_name(name: str, language: str | None = None) -> str:
    target_language = normalize_language(language) or current_language()
    target_language = normalize_language(target_language) or "zh-CN"
    return _build_lookup(target_language).get(_normalize(name), name)
