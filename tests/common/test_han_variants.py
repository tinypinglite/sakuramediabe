from src.common.han_variants import han_script_variants


def test_han_script_variants_cover_simplified_japanese_and_traditional():
    assert han_script_variants("亚美") == ("亚美", "亜美", "亞美")
    assert han_script_variants("亜美") == ("亜美", "亚美", "亞美")
    assert han_script_variants("亞美") == ("亞美", "亜美", "亚美")


def test_han_script_variants_keep_non_han_text_unchanged():
    assert han_script_variants("Mikami Yua") == ("Mikami Yua",)
    assert han_script_variants("深田えいみ") == ("深田えいみ",)
    assert han_script_variants("") == ()


def test_han_script_variants_are_unique_and_ordered():
    variants = han_script_variants("亚美")
    assert len(variants) == len(set(variants))
    assert variants[0] == "亚美"
