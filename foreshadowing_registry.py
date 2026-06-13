"""
伏笔全局注册表 — 事后全量匹配引擎

不依赖 LLM 的上下文记忆，在所有 chunk 扫描完成后收集全部伏笔数据，
用确定性算法（关键词重合度 + TF-IDF）做全量双向匹配。

输出每个伏笔的完整生命周期：
  planted_chunk → resolved_chunk (或 orphaned/false_lead)
  resolution_distance (跨度)
  status: resolved / orphaned / false_lead

使用方式：
  from foreshadowing_registry import build_foreshadowing_registry
  registry = build_foreshadowing_registry(chunk_results)
  report = registry.generate_report()
"""

from __future__ import annotations

import math
import re
import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 匹配阈值 ──
MIN_KEYWORD_OVERLAP = 0.15       # 关键词重合度下限
MIN_TFIDF_SIMILARITY = 0.08      # TF-IDF 余弦相似度下限
HIGH_CONFIDENCE_THRESHOLD = 0.35 # 高置信匹配阈值
MAX_RESOLUTION_SEARCH_WINDOW = 0  # 0 = 不限制搜索窗口（伏笔可在任意后续chunk回收）


def _tokenize(text: str) -> List[str]:
    """中文文本分词：按2-4字滑窗提取关键词。"""
    text = re.sub(r"[^\u4e00-\u9fff\w]", "", text.lower())
    if len(text) < 2:
        return []
    tokens = []
    # 2-gram 和 3-gram
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            token = text[i:i + n]
            if not token.isspace():
                tokens.append(token)
    return tokens


def _keyword_set(text: str) -> set:
    """提取关键词集合（2-gram去重）。"""
    return set(_tokenize(text))


def _tfidf_vector(text: str, idf: Dict[str, float]) -> Dict[str, float]:
    """构建 TF-IDF 向量。"""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    tf = Counter(tokens)
    total = sum(tf.values())
    return {term: (count / total) * idf.get(term, 1.0) for term, count in tf.items()}


def _cosine_similarity(v1: Dict[str, float], v2: Dict[str, float]) -> float:
    """计算两个稀疏向量的余弦相似度。"""
    if not v1 or not v2:
        return 0.0
    dot = sum(v1.get(k, 0) * v2.get(k, 0) for k in v1.keys() & v2.keys())
    norm1 = math.sqrt(sum(v * v for v in v1.values()))
    norm2 = math.sqrt(sum(v * v for v in v2.values()))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def _keyword_overlap(set_a: set, set_b: set) -> float:
    """关键词重合度（Jaccard系数）。"""
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _compute_idf(all_texts: List[str]) -> Dict[str, float]:
    """从所有文本计算 IDF。"""
    doc_count = len(all_texts)
    if doc_count == 0:
        return {}
    df = Counter()
    for text in all_texts:
        seen = set(_tokenize(text))
        for token in seen:
            df[token] += 1
    return {
        token: math.log((doc_count + 1) / (freq + 1)) + 1
        for token, freq in df.items()
    }


class ForeshadowingEntry:
    """单个伏笔条目。"""

    def __init__(
        self,
        chunk_index: int,
        description: str,
        entry_type: str = "",
        importance: str = "",
        evidence: str = "",
    ):
        self.planted_chunk = chunk_index
        self.description = description
        self.entry_type = entry_type
        self.importance = importance
        self.evidence = evidence
        self.resolved_chunk: Optional[int] = None
        self.resolution_description: str = ""
        self.resolution_evidence: str = ""
        self.resolution_satisfaction: str = ""
        self.match_confidence: float = 0.0
        self.status: str = "orphaned"  # resolved / orphaned / false_lead
        # 预计算
        self._keywords = _keyword_set(description)
        self._tfidf: Dict[str, float] = {}

    def set_tfidf(self, idf: Dict[str, float]) -> None:
        self._tfidf = _tfidf_vector(self.description, idf)

    @property
    def keywords(self) -> set:
        return self._keywords

    @property
    def tfidf(self) -> Dict[str, float]:
        return self._tfidf

    @property
    def resolution_distance(self) -> Optional[int]:
        if self.resolved_chunk is not None:
            return self.resolved_chunk - self.planted_chunk
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planted_chunk": self.planted_chunk,
            "description": self.description,
            "type": self.entry_type,
            "importance": self.importance,
            "evidence": self.evidence,
            "resolved_chunk": self.resolved_chunk,
            "resolution_distance": self.resolution_distance,
            "resolution_description": self.resolution_description,
            "resolution_evidence": self.resolution_evidence,
            "resolution_satisfaction": self.resolution_satisfaction,
            "match_confidence": round(self.match_confidence, 3),
            "status": self.status,
        }


class ForeshadowingRegistry:
    """伏笔全局注册表。"""

    def __init__(self):
        self.entries: List[ForeshadowingEntry] = []
        self.false_leads: List[Dict[str, Any]] = []
        self.unmatched_resolutions: List[Dict[str, Any]] = []
        self._matched: bool = False

    def add_planted(
        self,
        chunk_index: int,
        description: str,
        entry_type: str = "",
        importance: str = "",
        evidence: str = "",
    ) -> None:
        """注册一个新伏笔。"""
        if not description or not description.strip():
            return
        # 去重：同一 chunk 内描述完全相同的伏笔
        for existing in self.entries:
            if existing.planted_chunk == chunk_index and existing.description == description:
                return
        self.entries.append(ForeshadowingEntry(
            chunk_index=chunk_index,
            description=description.strip(),
            entry_type=entry_type,
            importance=importance,
            evidence=evidence,
        ))

    def add_resolution(
        self,
        chunk_index: int,
        resolved_item: str,
        resolution_description: str = "",
        evidence: str = "",
        satisfaction: str = "",
    ) -> None:
        """注册一个回收（待匹配）。"""
        if not resolved_item and not resolution_description:
            return
        self.unmatched_resolutions.append({
            "chunk_index": chunk_index,
            "resolved_item": (resolved_item or "").strip(),
            "resolution_description": (resolution_description or "").strip(),
            "evidence": (evidence or "").strip(),
            "satisfaction": (satisfaction or "").strip(),
        })

    def add_false_lead(self, chunk_index: int, description: str) -> None:
        """注册一个伪伏笔。"""
        if not description or not description.strip():
            return
        self.false_leads.append({
            "chunk_index": chunk_index,
            "description": description.strip(),
        })

    def match_resolutions(self) -> None:
        """
        对所有未匹配的 resolution 做全量匹配。
        每个 resolution 尝试匹配最佳伏笔（planted_chunk < resolution chunk）。
        """
        if self._matched:
            return
        self._matched = True

        # 构建 IDF
        all_texts = [e.description for e in self.entries]
        all_texts.extend(r["resolved_item"] for r in self.unmatched_resolutions if r["resolved_item"])
        all_texts.extend(r["resolution_description"] for r in self.unmatched_resolutions if r["resolution_description"])
        idf = _compute_idf(all_texts)

        # 为每个伏笔计算 TF-IDF
        for entry in self.entries:
            entry.set_tfidf(idf)

        # 匹配
        for resolution in self.unmatched_resolutions:
            res_text = resolution["resolved_item"] or resolution["resolution_description"]
            if not res_text:
                continue
            res_keywords = _keyword_set(res_text)
            res_tfidf = _tfidf_vector(res_text, idf)

            best_entry: Optional[ForeshadowingEntry] = None
            best_score: float = 0.0

            for entry in self.entries:
                # 回收必须发生在种植之后
                if entry.resolved_chunk is not None:
                    continue  # 已被匹配
                if resolution["chunk_index"] <= entry.planted_chunk:
                    continue  # 不能在种植之前回收

                # 搜索窗口限制
                if MAX_RESOLUTION_SEARCH_WINDOW > 0:
                    if resolution["chunk_index"] - entry.planted_chunk > MAX_RESOLUTION_SEARCH_WINDOW:
                        continue

                kw_score = _keyword_overlap(entry.keywords, res_keywords)
                tfidf_score = _cosine_similarity(entry.tfidf, res_tfidf)
                combined = kw_score * 0.4 + tfidf_score * 0.6

                if combined > best_score:
                    best_score = combined
                    best_entry = entry

            if best_entry and best_score >= MIN_TFIDF_SIMILARITY:
                best_entry.resolved_chunk = resolution["chunk_index"]
                best_entry.resolution_description = resolution["resolution_description"]
                best_entry.resolution_evidence = resolution["evidence"]
                best_entry.resolution_satisfaction = resolution["satisfaction"]
                best_entry.match_confidence = best_score
                best_entry.status = "resolved"
            # 未匹配的 resolution 保留在 unmatched_resolutions 中

        # 未被匹配的伏笔保持 orphaned 状态
        for entry in self.entries:
            if entry.status == "orphaned" and entry.resolved_chunk is None:
                entry.status = "orphaned"

    def get_statistics(self) -> Dict[str, Any]:
        """返回伏笔工程统计。"""
        if not self._matched:
            self.match_resolutions()

        total = len(self.entries)
        resolved = sum(1 for e in self.entries if e.status == "resolved")
        orphaned = sum(1 for e in self.entries if e.status == "orphaned")
        false_leads = len(self.false_leads)
        unmatched_resolutions = len([
            r for r in self.unmatched_resolutions
            if not any(
                e.resolved_chunk == r["chunk_index"]
                and r["resolved_item"] in (e.description, e.resolution_description)
                for e in self.entries
                if e.status == "resolved"
            )
        ])

        # 回收距离统计
        distances = [e.resolution_distance for e in self.entries if e.resolution_distance is not None]
        avg_distance = sum(distances) / len(distances) if distances else 0
        max_distance = max(distances) if distances else 0
        min_distance = min(distances) if distances else 0

        # 高置信匹配
        high_conf = sum(1 for e in self.entries if e.match_confidence >= HIGH_CONFIDENCE_THRESHOLD)

        # 回收率
        resolution_rate = resolved / total if total > 0 else 0.0

        return {
            "total_planted": total,
            "resolved": resolved,
            "orphaned": orphaned,
            "false_leads": false_leads,
            "unmatched_resolutions": unmatched_resolutions,
            "resolution_rate": round(resolution_rate, 3),
            "avg_resolution_distance": round(avg_distance, 1),
            "max_resolution_distance": max_distance,
            "min_resolution_distance": min_distance,
            "high_confidence_matches": high_conf,
            "low_confidence_matches": resolved - high_conf,
        }

    def get_entries(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回伏笔条目列表。"""
        if not self._matched:
            self.match_resolutions()
        if status:
            return [e.to_dict() for e in self.entries if e.status == status]
        return [e.to_dict() for e in self.entries]

    def get_orphaned(self) -> List[Dict[str, Any]]:
        """返回未回收的伏笔（最值得关注）。"""
        return self.get_entries(status="orphaned")

    def get_false_leads(self) -> List[Dict[str, Any]]:
        """返回伪伏笔/红鲱鱼。"""
        return list(self.false_leads)

    def generate_report(self) -> Dict[str, Any]:
        """生成完整伏笔工程报告。"""
        if not self._matched:
            self.match_resolutions()
        return {
            "statistics": self.get_statistics(),
            "resolved_entries": sorted(
                self.get_entries(status="resolved"),
                key=lambda x: x.get("resolution_distance", 0),
                reverse=True,
            ),
            "orphaned_entries": self.get_orphaned(),
            "false_leads": self.get_false_leads(),
        }


def build_foreshadowing_registry(chunk_results: List[Dict[str, Any]]) -> ForeshadowingRegistry:
    """
    从所有 chunk_results 构建伏笔全局注册表。

    每个 chunk_result 应包含 foreshadowing_engineering 字段：
    {
        "new_foreshadowing": [{"description": "...", "type": "...", ...}],
        "foreshadowing_resolutions": [{"resolved_item": "...", ...}],
        "false_foreshadowing": ["..."],
    }
    """
    registry = ForeshadowingRegistry()

    for chunk in chunk_results or []:
        if not isinstance(chunk, dict):
            continue
        chunk_index = int(chunk.get("chunk_index", 0) or 0)

        # 兼容两种字段名
        engineering = chunk.get("foreshadowing_engineering") or chunk.get("foreshadowing") or {}
        if isinstance(engineering, str):
            import json
            try:
                engineering = json.loads(engineering)
            except Exception:
                engineering = {}
        if not isinstance(engineering, dict):
            engineering = {}

        # 新伏笔
        for item in engineering.get("new_foreshadowing") or engineering.get("new_threads") or []:
            if isinstance(item, dict):
                registry.add_planted(
                    chunk_index=chunk_index,
                    description=str(item.get("description") or item.get("desc") or item.get("item") or ""),
                    entry_type=str(item.get("type") or item.get("kind") or ""),
                    importance=str(item.get("estimated_importance") or item.get("importance") or ""),
                    evidence=str(item.get("evidence") or item.get("quote") or ""),
                )
            elif isinstance(item, str) and item.strip():
                registry.add_planted(chunk_index=chunk_index, description=item)

        # 回收
        for item in engineering.get("foreshadowing_resolutions") or engineering.get("resolutions") or engineering.get("resolved_foreshadowing") or []:
            if isinstance(item, dict):
                registry.add_resolution(
                    chunk_index=chunk_index,
                    resolved_item=str(item.get("resolved_item") or item.get("item") or item.get("description") or ""),
                    resolution_description=str(item.get("resolution_description") or item.get("resolution") or item.get("payoff") or ""),
                    evidence=str(item.get("evidence") or item.get("quote") or ""),
                    satisfaction=str(item.get("satisfaction") or item.get("payoff_quality") or ""),
                )
            elif isinstance(item, str) and item.strip():
                registry.add_resolution(chunk_index=chunk_index, resolved_item=item)

        # 伪伏笔
        for item in engineering.get("false_foreshadowing") or engineering.get("red_herrings") or []:
            if isinstance(item, dict):
                text = str(item.get("description") or item.get("item") or "")
            else:
                text = str(item or "")
            if text.strip():
                registry.add_false_lead(chunk_index, text)

    # 执行全量匹配
    registry.match_resolutions()

    stats = registry.get_statistics()
    logger.info(
        "foreshadowing_registry: %d planted, %d resolved, %d orphaned, %d false_leads "
        "(resolution_rate=%.1f%%, avg_distance=%.0f chunks)",
        stats["total_planted"],
        stats["resolved"],
        stats["orphaned"],
        stats["false_leads"],
        stats["resolution_rate"] * 100,
        stats["avg_resolution_distance"],
    )

    return registry
