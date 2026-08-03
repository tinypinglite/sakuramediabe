"""番号处理函数的回归护栏。

这些边界全部在真实数据上踩过坑：
- 库里的规范番号来自 provider 原样，一本道用下划线（072625_001）、加勒比用横杠（072625-001），
  是两部不同影片；东热（n0646）与欧美（Vixen.2026.05.07）是小写。任何"写入侧归一化"都会
  损坏它们，normalize_movie_number 因此只允许当匹配键用，绝不落库。
- 人工输入点查走 movie_number_lookup_values 候选集：先精确后互换，两种分隔符影片同时存在时
  必须命中用户输入的那一部。
"""

from src.common.movie_numbers import (
    movie_number_lookup_values,
    normalize_movie_number,
    parse_movie_number_from_text,
)
from src.service.transfers.imports.source_scanner import parse_movie_number_from_scan_path


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


class TestParseMovieNumberFromScanPath:
    def test_number_in_parent_directory(self):
        # 截断到最后两级：番号在父目录名、文件名是 movie.mp4 的常见布局必须命中。
        assert parse_movie_number_from_scan_path("/vol/media/ABC-123/movie.mp4") == "ABC-123"

    def test_upper_directories_do_not_leak(self):
        # 上层目录的数字串不进入扫描范围，不会被误判成番号。
        assert parse_movie_number_from_scan_path("/downloads/temp1234/sub/movie.mp4") == ""

    def test_plain_filename(self):
        assert parse_movie_number_from_scan_path("122124_001.mp4") == "122124_001"


class TestNormalizeMovieNumber:
    def test_folds_case_space_and_separator(self):
        assert normalize_movie_number("  abc-123 ") == "ABC-123"
        assert normalize_movie_number("ABC 123") == "ABC123"
        assert normalize_movie_number("072625_001") == "072625-001"

    def test_strips_ppv_prefix(self):
        # 有损折叠：仅用于两侧同时折叠后的比较（字幕配对、provider 一致性校验）。
        assert normalize_movie_number("FC2-PPV-1234567") == "FC2-1234567"

    def test_empty_input(self):
        assert normalize_movie_number("") == ""
        assert normalize_movie_number(None) == ""


class TestMovieNumberLookupValues:
    def test_exact_candidate_comes_first(self):
        # 先精确后互换：一本道/加勒比同日番号同时在库时，必须先命中用户输入的形态。
        assert movie_number_lookup_values("072625_001") == ["072625_001", "072625-001"]
        assert movie_number_lookup_values("072625-001") == ["072625-001", "072625_001"]

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
