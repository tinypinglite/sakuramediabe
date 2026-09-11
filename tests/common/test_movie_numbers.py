"""番号匹配保留纯数字分隔符，其他番号保持已有宽松规则。"""

from src.common.movie_numbers import (
    movie_number_lookup_values,
    normalize_movie_number,
    parse_movie_number_from_text,
    subtitle_matches_movie_number,
)


class TestParseMovieNumberFromText:
    def test_letter_number_forms_join_with_dash(self):
        assert parse_movie_number_from_text("abc-123.mp4") == "ABC-123"
        assert parse_movie_number_from_text("SSIS00123") == "SSIS-123"
        assert parse_movie_number_from_text("dvmm206") == "DVMM-206"

    def test_numeric_pair_preserves_source_separator(self):
        # 分隔符是片商标识：一本道 _ / 加勒比 -，同日番号是两部不同影片，绝不转写。
        assert parse_movie_number_from_text("122124_001-1080p.mp4") == "122124_001"
        assert parse_movie_number_from_text("Caribbeancom 122124-001") == "122124-001"

    def test_fc2_variants_collapse_to_canonical(self):
        assert parse_movie_number_from_text("FC2PPV-1234567") == "FC2-1234567"
        assert parse_movie_number_from_text("FC2-PPV-1234567") == "FC2-1234567"
        assert parse_movie_number_from_text("FC2PPV_1234567") == "FC2-1234567"

    def test_domain_noise_removed_before_matching(self):
        # remove_disturb 先剥域名，站点水印不会被误识别成番号。
        assert parse_movie_number_from_text("hjd2048.com-0602meyd424") == "MEYD-424"

    def test_unparseable_returns_empty(self):
        assert parse_movie_number_from_text("random words") == ""
        assert parse_movie_number_from_text("") == ""
        assert parse_movie_number_from_text(None) == ""


class TestNormalizeMovieNumber:
    def test_folds_case_space_and_separator(self):
        assert normalize_movie_number("  abc-123 ") == "ABC-123"
        assert normalize_movie_number("ABC 123") == "ABC123"
        assert normalize_movie_number("ABC_123") == "ABC-123"
        for number in ("072625_001", "072625-001", "1_22", "1-22"):
            assert normalize_movie_number(number) == number

    def test_strips_ppv_prefix(self):
        # 有损折叠：仅用于两侧同时折叠后的比较（字幕配对、provider 一致性校验）。
        assert normalize_movie_number("FC2-PPV-1234567") == "FC2-1234567"

    def test_empty_input(self):
        assert normalize_movie_number("") == ""
        assert normalize_movie_number(None) == ""


class TestMovieNumberLookupValues:
    def test_exact_candidate_comes_first(self):
        # 纯数字番号只能命中自身。
        assert movie_number_lookup_values("072625_001") == ["072625_001"]
        assert movie_number_lookup_values("072625-001") == ["072625-001"]

    def test_case_folded_but_shape_preserved(self):
        # 大小写交给 UPPER(movie_number) 抹平，候选集只负责分隔符形态。
        assert movie_number_lookup_values(" n0646 ") == ["N0646"]
        assert movie_number_lookup_values("heydouga-4030-1717") == [
            "HEYDOUGA-4030-1717",
            "HEYDOUGA_4030_1717",
        ]

    def test_no_separator_yields_single_candidate(self):
        assert movie_number_lookup_values("ABC123") == ["ABC123"]

    def test_empty_input(self):
        assert movie_number_lookup_values("") == []
        assert movie_number_lookup_values("   ") == []
        assert movie_number_lookup_values(None) == []


def test_subtitle_pairing_preserves_numeric_separator():
    for number, other in (("072625_001", "072625-001"), ("072625-001", "072625_001")):
        assert subtitle_matches_movie_number(number + ".srt", number)
        assert not subtitle_matches_movie_number(other + ".srt", number)
    assert subtitle_matches_movie_number("ABC-123.srt", "ABC_123")
