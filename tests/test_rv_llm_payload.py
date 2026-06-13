"""
rv_llm_payload.py 单元测试 — 拆包提取的零依赖常量。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rv_llm_payload import (
    REVIEW_LLM_SECTION_MAX_CHARS,
    REVIEW_LLM_FIELD_MAX_CHARS,
    REVIEW_LLM_LIST_MAX_ITEMS,
)


class TestConstants:
    def test_section_max_chars(self):
        assert isinstance(REVIEW_LLM_SECTION_MAX_CHARS, int)
        assert REVIEW_LLM_SECTION_MAX_CHARS >= 2000

    def test_field_max_chars(self):
        assert isinstance(REVIEW_LLM_FIELD_MAX_CHARS, int)
        assert REVIEW_LLM_FIELD_MAX_CHARS >= 40

    def test_list_max_items(self):
        assert isinstance(REVIEW_LLM_LIST_MAX_ITEMS, int)
        assert REVIEW_LLM_LIST_MAX_ITEMS >= 5
