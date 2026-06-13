"""
text_anchor.py 单元测试 — chunk manifest、文本哈希、增量 diff。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_anchor import (
    compute_chunk_text_hash,
    build_chunk_hash_map,
    diff_chunk_manifests,
    save_chunk_manifest,
    load_chunk_manifest,
)


class TestComputeChunkTextHash:
    def test_stable(self):
        text = "这是一段测试文本"
        h1 = compute_chunk_text_hash(text)
        h2 = compute_chunk_text_hash(text)
        assert h1 == h2

    def test_different_text(self):
        h1 = compute_chunk_text_hash("文本A")
        h2 = compute_chunk_text_hash("文本B")
        assert h1 != h2

    def test_empty(self):
        h = compute_chunk_text_hash("")
        assert isinstance(h, str)
        assert len(h) > 0

    def test_whitespace_difference(self):
        h1 = compute_chunk_text_hash("hello world")
        h2 = compute_chunk_text_hash("hello  world")
        assert h1 != h2


class TestBuildChunkHashMap:
    def test_basic(self):
        manifest = {
            "chunks": [
                {"chunk_index": 1, "text": "text_a"},
                {"chunk_index": 2, "text": "text_b"},
                {"chunk_index": 3, "text": "text_c"},
            ]
        }
        hm = build_chunk_hash_map(manifest)
        assert isinstance(hm, dict)
        assert len(hm) == 3

    def test_duplicates(self):
        manifest = {
            "chunks": [
                {"chunk_index": 1, "text": "same"},
                {"chunk_index": 2, "text": "same"},
                {"chunk_index": 3, "text": "different"},
            ]
        }
        hm = build_chunk_hash_map(manifest)
        # hash 去重后只剩 2 个
        assert len(hm) == 2

    def test_empty_manifest(self):
        hm = build_chunk_hash_map({})
        assert len(hm) == 0

    def test_no_chunks_key(self):
        hm = build_chunk_hash_map({"other": "data"})
        assert len(hm) == 0


class TestDiffChunkManifests:
    def test_all_reuse(self):
        old = {"chunks": [{"chunk_index": 1, "text": "aaa"}, {"chunk_index": 2, "text": "bbb"}]}
        new = {"chunks": [{"chunk_index": 1, "text": "aaa"}, {"chunk_index": 2, "text": "bbb"}]}
        diff = diff_chunk_manifests(old, new)
        assert set(diff["reuse_chunks"]) == {1, 2}
        assert len(diff["rescan_chunks"]) == 0

    def test_all_changed(self):
        old = {"chunks": [{"chunk_index": 1, "text": "aaa"}, {"chunk_index": 2, "text": "bbb"}]}
        new = {"chunks": [{"chunk_index": 1, "text": "changed_a"}, {"chunk_index": 2, "text": "changed_b"}]}
        diff = diff_chunk_manifests(old, new)
        assert len(diff["reuse_chunks"]) == 0
        assert set(diff["rescan_chunks"]) == {1, 2}

    def test_partial_reuse(self):
        old = {"chunks": [
            {"chunk_index": 1, "text": "aaa"},
            {"chunk_index": 2, "text": "bbb"},
            {"chunk_index": 3, "text": "ccc"},
        ]}
        new = {"chunks": [
            {"chunk_index": 1, "text": "aaa"},
            {"chunk_index": 2, "text": "changed"},
            {"chunk_index": 3, "text": "ccc"},
        ]}
        diff = diff_chunk_manifests(old, new)
        assert set(diff["reuse_chunks"]) == {1, 3}
        assert set(diff["rescan_chunks"]) == {2}

    def test_empty_old(self):
        old = {}
        new = {"chunks": [{"chunk_index": 1, "text": "aaa"}]}
        diff = diff_chunk_manifests(old, new)
        assert len(diff["reuse_chunks"]) == 0

    def test_empty_new(self):
        old = {"chunks": [{"chunk_index": 1, "text": "aaa"}]}
        new = {}
        diff = diff_chunk_manifests(old, new)
        assert len(diff["reuse_chunks"]) == 0

    def test_reuse_rate(self):
        old = {"chunks": [{"chunk_index": 1, "text": "aaa"}, {"chunk_index": 2, "text": "bbb"}]}
        new = {"chunks": [{"chunk_index": 1, "text": "aaa"}, {"chunk_index": 2, "text": "bbb"}, {"chunk_index": 3, "text": "new"}]}
        diff = diff_chunk_manifests(old, new)
        assert abs(diff["reuse_rate"] - 2/3) < 0.001

    def test_removed_count(self):
        old = {"chunks": [{"chunk_index": 1, "text": "aaa"}, {"chunk_index": 2, "text": "bbb"}, {"chunk_index": 3, "text": "ccc"}]}
        new = {"chunks": [{"chunk_index": 1, "text": "aaa"}]}
        diff = diff_chunk_manifests(old, new)
        # old 有 3 个 chunk，new 只复用了 1 个，所以 removed = 3 - 1 = 2
        assert diff["removed_count"] == 2


class TestSaveLoadManifest:
    def test_roundtrip(self, tmp_path):
        manifest = {
            "chunks": [
                {"chunk_index": 1, "text_hash": "aaa", "window_start": 0, "window_end": 99},
            ],
            "text_signature": "sig_123",
        }
        path = str(tmp_path / "manifest.json")
        save_chunk_manifest(manifest, path)
        loaded = load_chunk_manifest(path)
        assert loaded["text_signature"] == "sig_123"
        assert loaded["chunks"][0]["text_hash"] == "aaa"

    def test_load_not_found(self, tmp_path):
        import pytest
        with pytest.raises(FileNotFoundError):
            load_chunk_manifest(str(tmp_path / "nonexistent.json"))
