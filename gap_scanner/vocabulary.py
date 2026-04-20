"""
词库引擎模块（纯逻辑，无 AI 依赖）。

负责从 vocab/ 目录加载、匹配、增删词条。
所有文件读写均为文本格式，人类可读可编辑。

词库分类：
    virtual_strong  — 强信号：单独命中即可判为虚拟供给
    virtual_weak    — 弱信号：需多个命中或与 delivery 组合
    demand_signal   — 求购/需求帖信号词
    delivery_method — 交付方式辅助词
    blacklist       — 误判排除词

匹配优先级：blacklist > demand_signal > 强/弱虚拟规则 > delivery_method > unknown

兼容：`virtual_supply` 作为读写别名，加载时为 strong∪weak；写入默认落到 virtual_weak。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

# 词库文件名映射（真实文件）
CATEGORY_FILES: dict[str, str] = {
    "virtual_strong": "virtual_strong.txt",
    "virtual_weak": "virtual_weak.txt",
    "demand_signal": "demand_signal.txt",
    "delivery_method": "delivery_method.txt",
    "blacklist": "blacklist.txt",
}

# add_terms / remove_terms 中旧分类名 → 实际写入的文件（保守：默认弱信号文件）
_WRITE_CATEGORY_ALIASES: dict[str, str] = {
    "virtual_supply": "virtual_weak",
}

ClassificationType = Literal[
    "virtual",
    "weak_virtual",
    "demand",
    "delivery",
    "blacklisted",
    "unknown",
]


@dataclass
class MatchResult:
    """单条标题的匹配结果。"""
    classification: ClassificationType
    matched_terms: list[str]
    confidence: float   # 1.0 = 词库明确命中；0.0 = 无匹配


@dataclass
class TermEntry:
    """待写入词库的词条。"""
    term: str
    confidence: float
    source: Literal["manual", "ai"] = "ai"
    reason: str = ""
    category: str = "virtual_supply"   # 默认写入 virtual_weak（经别名解析）


class Vocabulary:
    """
    词库 CRUD + 匹配引擎。

    用法：
        vocab = Vocabulary(Path("vocab"))
        result = vocab.match("Cursor教程 百度云 秒发")
        print(result.classification)  # "virtual"
    """

    def __init__(self, vocab_dir: Path) -> None:
        self.vocab_dir = vocab_dir
        vocab_dir.mkdir(parents=True, exist_ok=True)
        # 缓存：{category: set[str]}，首次 load 时懒加载
        self._cache: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load(self, category: str) -> set[str]:
        """
        加载指定词库，返回词条集合（已去除注释和空行）。
        结果会被缓存直到调用 invalidate_cache()。

        特殊：category == 'virtual_supply' 时返回 virtual_strong ∪ virtual_weak（兼容旧代码）。
        """
        if category == "virtual_supply":
            return self.load("virtual_strong") | self.load("virtual_weak")

        if category in self._cache:
            return self._cache[category]

        path = self._path(category)
        if not path.exists():
            self._cache[category] = set()
            return self._cache[category]

        terms: set[str] = set()
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            # 去除行内注释（# 后面的部分）并 strip
            line = re.sub(r"\s*#.*$", "", raw_line).strip()
            if line:
                terms.add(line)

        self._cache[category] = terms
        return terms

    def reload(self) -> None:
        """清空缓存，下次 match/load 时重新读取文件。"""
        self._cache.clear()

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def match(self, title: str) -> MatchResult:
        """
        对一条商品标题做多词库匹配。

        优先级：
            blacklisted > demand > 强虚拟 / 弱虚拟规则 > delivery > unknown
        """
        if not title:
            return MatchResult("unknown", [], 0.0)

        bl_hits = self._find_hits(title, "blacklist")
        if bl_hits:
            return MatchResult("blacklisted", bl_hits, 1.0)

        ds_hits = self._find_hits(title, "demand_signal")
        if ds_hits:
            return MatchResult("demand", ds_hits, 1.0)

        strong_hits = self._find_hits(title, "virtual_strong")
        weak_hits = self._find_hits(title, "virtual_weak")
        dm_hits = self._find_hits(title, "delivery_method")

        if strong_hits:
            merged = self._merge_hit_lists(strong_hits, weak_hits, dm_hits)
            return MatchResult("virtual", merged, 1.0)

        wc = len(weak_hits)
        dc = len(dm_hits)

        if wc >= 2:
            return MatchResult("virtual", list(weak_hits), 0.8)
        if wc == 1 and dc >= 1:
            merged = self._merge_hit_lists(weak_hits, dm_hits)
            return MatchResult("virtual", merged, 0.7)
        if wc == 1:
            return MatchResult("weak_virtual", list(weak_hits), 0.4)

        if dm_hits:
            return MatchResult("delivery", dm_hits, 0.7)

        return MatchResult("unknown", [], 0.0)

    @staticmethod
    def _merge_hit_lists(*lists: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for lst in lists:
            for t in lst:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out

    def _find_hits(self, title: str, category: str) -> list[str]:
        """返回标题中命中该词库的词条列表（按词库文件内字符串匹配）。"""
        terms = self.load(category)
        return [t for t in terms if t in title]

    # ------------------------------------------------------------------
    # 增删
    # ------------------------------------------------------------------

    def _resolve_write_category(self, category: str) -> str:
        return _WRITE_CATEGORY_ALIASES.get(category, category)

    def add_terms(self, category: str, entries: list[TermEntry]) -> int:
        """
        将新词条追加写入词库文件，跳过已存在的词。
        返回实际新增数量。
        """
        write_cat = self._resolve_write_category(category)
        path = self._path(write_cat)
        existing = self.load(write_cat)

        new_entries = [e for e in entries if e.term not in existing]
        if not new_entries:
            return 0

        today = date.today().isoformat()
        lines: list[str] = []

        # 确保文件末尾有换行
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if content and not content.endswith("\n"):
                lines.append("")  # 补一个空行

        lines.append(f"\n# --- AI learned {today} ---")
        for e in new_entries:
            comment_parts = [f"source={e.source}"]
            if e.confidence:
                comment_parts.append(f"confidence={e.confidence:.2f}")
            if e.reason:
                comment_parts.append(e.reason[:60])
            comment = "  # " + ", ".join(comment_parts)
            lines.append(f"{e.term}{comment}")

        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # 使缓存失效
        self._cache.pop(write_cat, None)
        self._cache.pop("virtual_supply", None)
        return len(new_entries)

    def remove_terms(self, category: str, terms: list[str]) -> int:
        """
        从词库文件中删除指定词条（保留注释行和其他词）。
        返回实际删除数量。
        """
        write_cat = self._resolve_write_category(category)
        targets = frozenset(terms)
        removed = 0

        if category == "virtual_supply":
            # 旧别名：两个文件都尝试删除
            removed += self._remove_terms_from_file("virtual_strong", targets)
            removed += self._remove_terms_from_file("virtual_weak", targets)
            self._cache.pop("virtual_strong", None)
            self._cache.pop("virtual_weak", None)
            self._cache.pop("virtual_supply", None)
            return removed

        removed = self._remove_terms_from_file(write_cat, targets)
        self._cache.pop(write_cat, None)
        self._cache.pop("virtual_supply", None)
        return removed

    def _remove_terms_from_file(self, category: str, to_remove: frozenset[str]) -> int:
        path = self._path(category)
        if not path.exists():
            return 0
        removed = 0
        new_lines: list[str] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            term = re.sub(r"\s*#.*$", "", raw_line).strip()
            if term and term in to_remove:
                removed += 1
                continue
            new_lines.append(raw_line)
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return removed

    def add_to_pending(self, entries: list[TermEntry]) -> None:
        """
        将低置信度候选词写入 pending_review.txt，供人工审核。
        格式：term | category | confidence | reason | date
        """
        path = self.vocab_dir / "pending_review.txt"
        today = date.today().isoformat()

        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                reason = e.reason.replace("|", "｜")[:80]  # 防止分隔符冲突
                f.write(f"{e.term} | {e.category} | {e.confidence:.2f} | {reason} | {today}\n")

    def load_pending(self) -> list[TermEntry]:
        """加载 pending_review.txt 中的待审核词条。"""
        path = self.vocab_dir / "pending_review.txt"
        if not path.exists():
            return []

        entries: list[TermEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            try:
                entries.append(TermEntry(
                    term=parts[0],
                    category=parts[1] if len(parts) > 1 else "virtual_supply",
                    confidence=float(parts[2]) if len(parts) > 2 else 0.7,
                    reason=parts[3] if len(parts) > 3 else "",
                    source="ai",
                ))
            except (ValueError, IndexError):
                continue
        return entries

    def clear_pending(self) -> None:
        """清空 pending_review.txt（审核完成后调用）。"""
        path = self.vocab_dir / "pending_review.txt"
        if path.exists():
            # 保留文件头注释
            header = "# AI 词库学习 — 待人工审核\n# 格式：term | category | confidence | reason | date\n"
            path.write_text(header, encoding="utf-8")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _path(self, category: str) -> Path:
        filename = CATEGORY_FILES.get(category, f"{category}.txt")
        return self.vocab_dir / filename

    def stats(self) -> dict[str, int]:
        """返回各词库词条数量统计。"""
        return {cat: len(self.load(cat)) for cat in CATEGORY_FILES}
