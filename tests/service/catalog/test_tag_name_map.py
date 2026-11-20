from src.service.catalog.tag_name_map import resolve_tag_name


def test_resolve_tag_name_to_simplified_chinese():
    assert resolve_tag_name("顏射", "zh-CN") == "颜射"
    assert resolve_tag_name("Big tits", "zh-CN") == "巨乳"
    assert resolve_tag_name("4K", "zh-CN") == "4K"


def test_resolve_tag_name_to_english():
    assert resolve_tag_name("巨乳", "en") == "Big Tits"


def test_resolve_tag_name_keeps_unknown_or_ambiguous_name():
    assert resolve_tag_name("不存在的标签", "zh-CN") == "不存在的标签"
    assert resolve_tag_name("颜射", "en") == "颜射"
