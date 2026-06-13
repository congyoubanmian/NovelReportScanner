"""
name_normalizer.py 单元测试 — 名字归一化、拆分、编辑距离、相似度。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from name_normalizer import (
    normalize_person_name,
    split_multi_names,
    split_top_level_name_parts,
    is_group_or_title_enumeration_name,
    levenshtein_distance,
    quick_text_similarity,
)


class TestNormalizePersonName:
    def test_strip_whitespace(self):
        assert normalize_person_name("  张三  ") == "张三"

    def test_fullwidth_space(self):
        assert normalize_person_name("\u3000李四\u3000") == "李四"

    def test_strip_quotes(self):
        assert normalize_person_name('"王五"') == "王五"
        assert normalize_person_name("'赵六'") == "赵六"

    def test_none(self):
        assert normalize_person_name(None) == ""

    def test_empty(self):
        assert normalize_person_name("") == ""

    def test_compress_spaces(self):
        assert normalize_person_name("Jean   Pierre") == "Jean Pierre"

    def test_fullwidth_comma(self):
        assert normalize_person_name("张，三") == "张,三"

    def test_preserve_english_name(self):
        assert normalize_person_name("Smith, John") == "Smith, John"


class TestSplitMultiNames:
    def test_split_dunhao(self):
        result = split_multi_names("张三、李四、王五")
        assert result == ["张三", "李四", "王五"]

    def test_split_comma_cjk(self):
        result = split_multi_names("张三,李四")
        assert result == ["张三", "李四"]

    def test_no_split_english(self):
        result = split_multi_names("Smith, John")
        assert result == ["Smith, John"]

    def test_empty(self):
        assert split_multi_names("") == []

    def test_none(self):
        assert split_multi_names(None) == []

    def test_single_name(self):
        assert split_multi_names("张三") == ["张三"]

    def test_group_name_not_split(self):
        result = split_multi_names("群臣、百官")
        assert result == []

    def test_filter_empty_parts(self):
        result = split_multi_names("张三、、李四")
        assert result == ["张三", "李四"]


class TestSplitTopLevelNameParts:
    def test_basic_split(self):
        assert split_top_level_name_parts("A、B、C", {"、"}) == ["A", "B", "C"]

    def test_paren_depth(self):
        # 括号内的分隔符不应该被拆分
        result = split_top_level_name_parts("张三（又名、李四）、王五", {"、"})
        assert result == ["张三（又名、李四）", "王五"]

    def test_no_separator(self):
        assert split_top_level_name_parts("张三", {"、"}) == ["张三"]


class TestIsGroupOrTitleEnumerationName:
    def test_group_terms(self):
        assert is_group_or_title_enumeration_name("群臣、百官") is True
        assert is_group_or_title_enumeration_name("将领、士兵等人") is True

    def test_normal_name(self):
        assert is_group_or_title_enumeration_name("张三") is False

    def test_empty(self):
        assert is_group_or_title_enumeration_name("") is True

    def test_generic_person_name(self):
        # is_generic_person_name 会匹配一些泛称
        assert is_group_or_title_enumeration_name("某人") is True


class TestLevenshteinDistance:
    def test_identical(self):
        assert levenshtein_distance("张三", "张三") == 0

    def test_one_diff(self):
        assert levenshtein_distance("张三", "李三") == 1

    def test_two_diff(self):
        assert levenshtein_distance("张三", "李四") == 2

    def test_empty_a(self):
        assert levenshtein_distance("", "abc") == 3

    def test_empty_b(self):
        assert levenshtein_distance("abc", "") == 3

    def test_both_empty(self):
        assert levenshtein_distance("", "") == 0

    def test_length_cutoff(self):
        # 长度差>max_dist 时直接返回 max_dist+1
        assert levenshtein_distance("ab", "abcdefgh", max_dist=2) == 3


class TestQuickTextSimilarity:
    def test_identical(self):
        assert quick_text_similarity(["hello"], ["hello"]) == 1.0

    def test_empty(self):
        assert quick_text_similarity([], ["hello"]) == 0.0
        assert quick_text_similarity(["hello"], []) == 0.0
        assert quick_text_similarity(None, None) == 0.0

    def test_different(self):
        sim = quick_text_similarity(["abc"], ["xyz"])
        assert 0.0 <= sim < 0.5

    def test_partial(self):
        sim = quick_text_similarity(["hello world"], ["hello there"])
        assert 0.5 < sim < 1.0

    def test_multiple_texts(self):
        sim = quick_text_similarity(["a", "b", "c"], ["a", "b", "c"])
        assert sim == 1.0
