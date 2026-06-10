"""
scan_memory.py - 结构化扫描记忆体

替代纯文本摘要（CHUNK_SUMMARIES），维护随扫描增长的结构化记忆。
类似阅读大师的"脑中笔记"——不是逐字记住全文，而是记住：
- 哪些叙事线索还没闭合（active_threads）
- 角色当前状态（character_states）
- 关键事件时间线（timeline）
- 已确认事实的指纹（用于增量去重）

设计原则：
- 记忆体是"提示"而非"事实"，注入 prompt 时标注仅供参考
- 优先压缩，控制注入 token 在 800 字以内
- 与 CHUNK_SUMMARIES 兼容，可渐进式替换
- 记忆衰减：长期未引用的线索降权
"""

import json
import hashlib
import os
import logging
import threading
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ===================== 配置 =====================
SCAN_MEMORY_ENABLED = os.environ.get("SCAN_MEMORY_ENABLED", "1").strip() == "1"
SCAN_MEMORY_MAX_THREADS = int(os.environ.get("SCAN_MEMORY_MAX_THREADS", "30") or "30")
SCAN_MEMORY_MAX_TIMELINE = int(os.environ.get("SCAN_MEMORY_MAX_TIMELINE", "50") or "50")
SCAN_MEMORY_MAX_CHARS = int(os.environ.get("SCAN_MEMORY_MAX_CHARS", "800") or "800")
SCAN_MEMORY_DECAY_CHUNKS = int(os.environ.get("SCAN_MEMORY_DECAY_CHUNKS", "50") or "50")
SCAN_MEMORY_SCHEMA_VERSION = 1


class ScanMemory:
    """
    结构化扫描记忆体。

    核心数据结构：
    - active_threads: 未闭合的叙事线索
    - character_states: 角色当前状态快照
    - timeline: 关键事件时间线
    - confirmed_fact_hashes: 已确认事实的指纹（用于去重）
    """

    def __init__(self):
        self.active_threads: List[Dict[str, Any]] = []
        self.character_states: Dict[str, Dict[str, Any]] = {}
        self.timeline: List[Dict[str, Any]] = []
        self.confirmed_fact_hashes: Set[str] = set()
        self._current_chunk: int = 0
        self._lock = threading.Lock()

    def update_from_chunk_result(
        self,
        chunk_index: int,
        *,
        heroine_facts: List[Dict[str, Any]] = None,
        issues: List[Dict[str, Any]] = None,
        summary_text: str = "",
    ) -> None:
        """
        从 chunk 扫描结果更新记忆体。

        这是核心更新入口，在每个 chunk 扫描完成后调用。
        """
        if not SCAN_MEMORY_ENABLED:
            return

        with self._lock:
            self._current_chunk = chunk_index

            # 1. 从 heroine_facts 中提取角色状态更新
            for fact in (heroine_facts or []):
                heroine = fact.get("heroine", "").strip()
                if not heroine:
                    continue
                dimension = fact.get("dimension", "")
                detail = fact.get("detail", "")
                evidence_level = fact.get("evidence_level", "")

                # 记录事实指纹
                fact_hash = self._fact_fingerprint(heroine, dimension, detail)
                self.confirmed_fact_hashes.add(fact_hash)

                # 更新角色状态
                if heroine not in self.character_states:
                    self.character_states[heroine] = {
                        "first_seen_chunk": chunk_index,
                        "last_updated_chunk": chunk_index,
                        "key_facts": [],
                    }
                state = self.character_states[heroine]
                state["last_updated_chunk"] = chunk_index

                # 只保留关键事实（性关系、孩子、重要关系变化）
                if dimension in ("sexual_relations", "children_info") and detail:
                    state["key_facts"].append({
                        "dimension": dimension,
                        "detail": detail[:60],
                        "chunk": chunk_index,
                        "evidence_level": evidence_level,
                    })
                    # 保留最近 5 条关键事实
                    state["key_facts"] = state["key_facts"][-5:]

            # 2. 从 issues 中提取叙事线索
            for issue in (issues or []):
                issue_type = issue.get("type", "")
                detail = issue.get("detail", issue.get("reason", ""))

                # 伏笔和悬念作为 active_thread
                if issue_type in ("foreshadowing", "悬念", "伏笔") and detail:
                    self._add_or_update_thread(
                        description=detail[:80],
                        thread_type="foreshadowing",
                        chunk_index=chunk_index,
                    )

                # 重大事件加入 timeline
                if issue_type in ("major_event", "重大事件", "转折") and detail:
                    self.timeline.append({
                        "chunk": chunk_index,
                        "event": detail[:60],
                    })

            # 3. 基于 summary_text 提取更精确的线索（如有）
            if summary_text and len(summary_text) > 20:
                # 检测重大状态变化
                change_markers = ["突破", "觉醒", "死亡", "背叛", "成亲", "分手", "怀孕", "生子"]
                for marker in change_markers:
                    if marker in summary_text:
                        self.timeline.append({
                            "chunk": chunk_index,
                            "event": f"【{marker}】{summary_text[:40]}",
                        })
                        break

            # 4. 衰减处理
            self._decay()

    def _add_or_update_thread(self, description: str, thread_type: str, chunk_index: int) -> None:
        """添加或更新叙事线索。"""
        # 检查是否已有相似线索
        for thread in self.active_threads:
            if self._similar_description(thread.get("description", ""), description):
                thread["last_referenced_chunk"] = chunk_index
                return

        self.active_threads.append({
            "description": description,
            "type": thread_type,
            "since_chunk": chunk_index,
            "last_referenced_chunk": chunk_index,
            "status": "open",
        })

    def _similar_description(self, desc1: str, desc2: str) -> bool:
        """简单判断两个描述是否相似（避免重复线索）。"""
        if not desc1 or not desc2:
            return False
        # 取前 20 字符比较
        return desc1[:20] == desc2[:20]

    def _fact_fingerprint(self, heroine: str, dimension: str, detail: str) -> str:
        """生成事实指纹（用于去重）。"""
        key = f"{heroine}|{dimension}|{detail[:40]}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def has_fact(self, heroine: str, dimension: str, detail: str) -> bool:
        """检查是否已有相似事实。"""
        fp = self._fact_fingerprint(heroine, dimension, detail)
        return fp in self.confirmed_fact_hashes

    def _decay(self) -> None:
        """记忆衰减：长期未引用的线索降权。"""
        current = self._current_chunk

        # 线索衰减：超过 N 个 chunk 未引用的标记为 stale
        for thread in self.active_threads:
            if current - thread.get("last_referenced_chunk", 0) > SCAN_MEMORY_DECAY_CHUNKS:
                thread["status"] = "stale"

        # 移除过期的 stale 线索（保留最近 MAX_THREADS 条）
        active = [t for t in self.active_threads if t["status"] == "open"]
        stale = [t for t in self.active_threads if t["status"] == "stale"]
        if len(active) > SCAN_MEMORY_MAX_THREADS:
            active = active[-SCAN_MEMORY_MAX_THREADS:]
        # 只保留最近 10 条 stale
        stale = stale[-10:]
        self.active_threads = active + stale

        # timeline 保留最近 MAX_TIMELINE 条
        if len(self.timeline) > SCAN_MEMORY_MAX_TIMELINE:
            self.timeline = self.timeline[-SCAN_MEMORY_MAX_TIMELINE:]

        # 角色状态：保留最近更新的角色（最多 20 个）
        if len(self.character_states) > 20:
            sorted_chars = sorted(
                self.character_states.items(),
                key=lambda x: x[1].get("last_updated_chunk", 0),
                reverse=True,
            )
            self.character_states = dict(sorted_chars[:20])

        # 事实指纹集合控制大小
        if len(self.confirmed_fact_hashes) > 500:
            # 保留最近的（无法精确排序，直接截断）
            self.confirmed_fact_hashes = set(list(self.confirmed_fact_hashes)[-300:])

    def to_prompt_text(self, max_chars: int = None) -> str:
        """
        将记忆体压缩为可注入 prompt 的文本。

        格式紧凑，控制在 max_chars 以内。
        """
        if not SCAN_MEMORY_ENABLED:
            return ""

        max_chars = max_chars or SCAN_MEMORY_MAX_CHARS
        if max_chars <= 0:
            return ""

        with self._lock:
            lines = ["【扫描记忆体（仅供参考，不能替代原文证据）】"]

            # 活跃线索
            open_threads = [t for t in self.active_threads if t["status"] == "open"]
            if open_threads:
                lines.append(f"未闭合线索({len(open_threads)}):")
                for t in open_threads[-8:]:
                    lines.append(f"  · {t['description'][:50]}")

            # 角色关键状态
            active_chars = {
                k: v for k, v in self.character_states.items()
                if v.get("key_facts")
            }
            if active_chars:
                lines.append(f"角色关键状态({len(active_chars)}):")
                for name, state in list(active_chars.items())[-6:]:
                    facts_str = "; ".join(
                        f["detail"][:30] for f in state["key_facts"][-2:]
                    )
                    lines.append(f"  · {name}: {facts_str}")

            # 最近时间线
            if self.timeline:
                lines.append(f"近期事件({len(self.timeline)}):")
                for ev in self.timeline[-5:]:
                    lines.append(f"  · [ch{ev['chunk']}] {ev['event'][:40]}")

            text = "\n".join(lines)
            if len(text) > max_chars:
                text = text[:max_chars - 3] + "..."
            return text

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于持久化）。"""
        return {
            "schema_version": SCAN_MEMORY_SCHEMA_VERSION,
            "active_threads": self.active_threads,
            "character_states": self.character_states,
            "timeline": self.timeline,
            "confirmed_fact_hashes": list(self.confirmed_fact_hashes),
            "current_chunk": self._current_chunk,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScanMemory":
        """从字典反序列化。"""
        mem = cls()
        if not data:
            return mem
        mem.active_threads = data.get("active_threads", [])
        mem.character_states = data.get("character_states", {})
        mem.timeline = data.get("timeline", [])
        mem.confirmed_fact_hashes = set(data.get("confirmed_fact_hashes", []))
        mem._current_chunk = data.get("current_chunk", 0)
        return mem

    def save(self, path: str) -> None:
        """保存到 JSON 文件。"""
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> Optional["ScanMemory"]:
        """从 JSON 文件加载。"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception:
            return None
