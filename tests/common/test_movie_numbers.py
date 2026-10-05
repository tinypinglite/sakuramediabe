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

    def test_letter_series_not_truncated_after_punctuation(self):
        # 水印/域名残片后紧跟番号时，不能从第二个字母起截出假番号。
        assert (
            parse_movie_number_from_text(
                "[4096社区 www.0621dz.cn[.NGOD-253 保健室のひと妻 徹夜の看病後に学生たちの逞しい身体に翻弄される 波多野結衣.mp4"
            )
            == "NGOD-253"
        )
        assert (
            parse_movie_number_from_text(
                "[4096社区 www.0621dz.cn].AOZ-314 一人暮らしの美人OLだけを狙った尾行押し込み鬼畜レ●プ映像.mp4"
            )
            == "AOZ-314"
        )

    def test_short_n_series_requires_left_boundary(self):
        # N\d{4} 规则不能从更长字母串里截尾（adn00277 -> N0027）。
        assert parse_movie_number_from_text("adn00277.mp4") == "ADN-277"
        assert parse_movie_number_from_text("118chn00037hhb_000.mp4") == "CHN-037"
        assert parse_movie_number_from_text("n0646.mp4") == "N0646"

    def test_numeric_pair_requires_digit_boundaries(self):
        # 下载目录名 FC2-1743979-463fb7 里的 1743979-463 是长数字串的尾段，不是素人番号。
        assert (
            parse_movie_number_from_text("FC2-1743979-463fb7/FC2-PPV-1743979 我和清纯的她约会了")
            == "FC2-1743979"
        )

    def test_watermark_tag_does_not_become_number(self):
        # hhb/hhb1/hhb2 是压制组水印，不能把 "_60fps" 前面的 hhb 当番号。
        assert parse_movie_number_from_text("hhd800.com@midv00574hhb_60fps") == "MIDV-574"

    def test_z_suffix_is_part_of_number(self):
        # JavDB 里 IBW-478 与 IBW-478z 是两条记录，z 后缀属于番号本体。
        assert parse_movie_number_from_text("SSIS-001Z.mp4") == "SSIS-001Z"
        assert parse_movie_number_from_text("AOZ-313Z.mp4") == "AOZ-313Z"
        assert parse_movie_number_from_text("IBW-478z.mp4") == "IBW-478Z"

    def test_numeric_pair_allows_two_digit_part(self):
        # 10musume 等系列的第二段可以是两位（010116_01），分隔符保持原样。
        assert parse_movie_number_from_text("010116_01.mp4") == "010116_01"
        assert parse_movie_number_from_text("020317-01-10musume-1080p.mp4") == "020317-01"

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
