"""
AI 词库学习模块。

每次扫描结束后，从本次爬取的所有标题中提取新的「闲鱼黑话」，
自动更新词库文件（或写入 pending_review.txt 待人工确认）。

学习流程：
    1. 筛出词库未命中的标题（classification == "unknown" 或来自 AI 分类器的 signal_terms）
    2. 将标题发给 AI，提取新信号词
    3. 按置信度分流：
       - >= auto_threshold  → 自动写入词库
       - >= review_threshold → 写入 pending_review.txt 待审核
       - < review_threshold  → 忽略
    4. 返回 LearnResult 供调用方打印摘要

AI 建议删除的过时词：写入 vocab/prune_suggestions.txt，不自动执行删除。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ai_client import AIClient, AIClientError
from config import Settings
from vocabulary import Vocabulary, TermEntry

logger = logging.getLogger(__name__)

# 每批发送给 AI 的最大标题数
LEARN_BATCH_SIZE = 80

_SYSTEM_PROMPT = """\
你是一位专注于中国二手交易平台「闲鱼」的虚拟商品语言专家。

你的任务：从一批闲鱼商品标题中，挖掘出能区分「虚拟商品/服务」和「需求帖」的新信号词。

背景：
- 闲鱼卖家经常使用「黑话」规避平台审核或精准传达信息
- 这些黑话随时间演化，需要持续更新识别词库
- 你需要帮助发现词库中还没有的新信号词

当前词库已有词（请勿重复推荐这些）：
{existing_vocab}

待分析标题（这些标题当前词库无法识别）：
{titles}

请从这些标题中提取：
1. 表示「虚拟商品/服务」的新信号词（暗示可数字交付）
2. 表示「求购/需求」的新信号词（暗示买家在找东西）
3. 表示「交付方式」的新词（网盘、私信等交付暗语）
4. 当前词库中你认为过时或误判率高的词（可选）

注意事项：
- 只推荐通用性强的词，不推荐太具体的商品名称
- 置信度反映这个词作为信号词的可靠程度（0.0-1.0）
- 同一含义不要推荐多个重复词

请以 JSON 格式返回：
{{
  "new_terms": [
    {{
      "term": "词条",
      "category": "virtual_supply",
      "confidence": 0.9,
      "reason": "简短说明（中文，不超过30字）"
    }}
  ],
  "prune_suggestions": [
    {{
      "term": "过时词",
      "reason": "为什么建议删除"
    }}
  ]
}}

category 只能是：virtual_supply | demand_signal | delivery_method
"""


@dataclass
class LearnResult:
    """学习结果摘要。"""
    auto_added: list[TermEntry] = field(default_factory=list)       # 自动写入词库的词
    pending_review: list[TermEntry] = field(default_factory=list)   # 待人工审核的词
    prune_suggestions: list[str] = field(default_factory=list)      # 建议删除的词
    titles_analyzed: int = 0


async def learn_from_scan(
    all_items: list[dict],
    vocabulary: Vocabulary,
    client: AIClient,
    settings: Settings,
) -> LearnResult:
    """
    从本次扫描的所有商品中学习新信号词。

    参数：
        all_items:  本次扫描的全部商品（含各关键词结果）
        vocabulary: 词库实例（用于去重和写入）
        client:     AIClient 实例
        settings:   配置（含置信度阈值）

    返回：LearnResult 摘要
    """
    result = LearnResult()

    # 1. 收集值得学习的标题：
    #    - 词库未命中（unknown）的
    #    - AI 分类器发现了 signal_terms 的
    candidate_titles: list[str] = []
    extra_signal_terms: list[tuple[str, str]] = []  # (term, category)

    seen_titles: set[str] = set()
    for item in all_items:
        title = item.get("title", "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)

        # 词库未命中的标题都值得分析
        if item.get("classification") in ("unknown", "other"):
            candidate_titles.append(title)

        # 从 AI 分类器收集到的 signal_terms（已发现但还没入库）
        for term in item.get("matched_terms", []):
            if term and len(term) >= 2:
                extra_signal_terms.append((term, "virtual_supply"))

    result.titles_analyzed = len(candidate_titles)

    if not candidate_titles and not extra_signal_terms:
        logger.info("[learner] 没有需要学习的新标题")
        return result

    # 2. 预处理 AI 分类器已发现的 signal_terms（批量入库，无需再次 AI）
    if extra_signal_terms:
        _process_signal_terms(extra_signal_terms, vocabulary, settings, result)

    # 3. 对未命中标题调用 AI 提取新词
    if candidate_titles:
        await _learn_from_titles(candidate_titles, vocabulary, client, settings, result)

    return result


def _process_signal_terms(
    terms: list[tuple[str, str]],
    vocabulary: Vocabulary,
    settings: Settings,
    result: LearnResult,
) -> None:
    """
    处理 AI 分类器已发现的 signal_terms。
    这些词置信度默认 0.80（来自分类器，已有一定可信度）。
    """
    existing = set()
    for category in ("virtual_supply", "demand_signal", "delivery_method"):
        existing.update(vocabulary.load(category))

    entries_by_category: dict[str, list[TermEntry]] = {}
    for term, category in terms:
        if term in existing:
            continue
        entry = TermEntry(term=term, confidence=0.80, source="ai",
                          reason="来自 AI 分类器 signal_terms", category=category)
        entries_by_category.setdefault(category, []).append(entry)

    for category, entries in entries_by_category.items():
        if entries[0].confidence >= settings.vocab_auto_threshold:
            added = vocabulary.add_terms(category, entries)
            result.auto_added.extend(entries[:added])
        else:
            vocabulary.add_to_pending(entries)
            result.pending_review.extend(entries)


async def _learn_from_titles(
    titles: list[str],
    vocabulary: Vocabulary,
    client: AIClient,
    settings: Settings,
    result: LearnResult,
) -> None:
    """分批调用 AI，从未命中标题中提取新信号词。"""
    # 构建现有词库摘要（避免 AI 重复推荐）
    existing_summary = _build_existing_vocab_summary(vocabulary)

    for batch_start in range(0, len(titles), LEARN_BATCH_SIZE):
        batch = titles[batch_start: batch_start + LEARN_BATCH_SIZE]
        await _process_title_batch(batch, existing_summary, vocabulary, client, settings, result)


def _build_existing_vocab_summary(vocabulary: Vocabulary) -> str:
    """构建现有词库的简洁摘要，避免 AI 重复推荐已有词。"""
    parts = []
    for category in ("virtual_supply", "demand_signal", "delivery_method"):
        terms = vocabulary.load(category)
        if terms:
            sample = list(terms)[:20]  # 只展示前20个，节省 token
            parts.append(f"{category}: {', '.join(sample)}")
    return "\n".join(parts) if parts else "（词库为空）"


async def _process_title_batch(
    titles: list[str],
    existing_vocab_summary: str,
    vocabulary: Vocabulary,
    client: AIClient,
    settings: Settings,
    result: LearnResult,
) -> None:
    """处理单批标题。"""
    titles_block = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = _SYSTEM_PROMPT.format(
        existing_vocab=existing_vocab_summary,
        titles=titles_block,
    )

    try:
        response = await client.chat(
            system="你是闲鱼虚拟商品语言专家，帮助维护信号词词库。",
            user=prompt,
        )
        _process_ai_response(response, vocabulary, settings, result)

    except AIClientError as e:
        logger.warning(f"[learner] AI 学习调用失败：{e}")
    except Exception as e:
        logger.warning(f"[learner] 意外错误：{e}")


def _process_ai_response(
    response: dict,
    vocabulary: Vocabulary,
    settings: Settings,
    result: LearnResult,
) -> None:
    """解析 AI 返回的词条，按置信度写入词库或 pending。"""
    new_terms = response.get("new_terms", [])
    prune_suggestions = response.get("prune_suggestions", [])

    # 收集现有词（去重用）
    existing = set()
    for category in ("virtual_supply", "demand_signal", "delivery_method"):
        existing.update(vocabulary.load(category))
    existing.update(e.term for e in result.auto_added)
    existing.update(e.term for e in result.pending_review)

    # 按类别整理新词
    to_add: dict[str, list[TermEntry]] = {}
    to_pending: list[TermEntry] = []

    for item in new_terms:
        term = str(item.get("term", "")).strip()
        category = str(item.get("category", "virtual_supply"))
        confidence = float(item.get("confidence", 0.7))
        reason = str(item.get("reason", ""))[:60]

        if not term or len(term) < 2 or term in existing:
            continue
        if category not in ("virtual_supply", "demand_signal", "delivery_method"):
            category = "virtual_supply"

        entry = TermEntry(term=term, confidence=confidence, source="ai",
                          reason=reason, category=category)

        if confidence >= settings.vocab_auto_threshold:
            to_add.setdefault(category, []).append(entry)
        elif confidence >= settings.vocab_review_threshold:
            to_pending.append(entry)
        # 低于 review_threshold 的忽略

    # 写入词库
    for category, entries in to_add.items():
        added = vocabulary.add_terms(category, entries)
        result.auto_added.extend(entries[:added])

    # 写入 pending
    if to_pending:
        vocabulary.add_to_pending(to_pending)
        result.pending_review.extend(to_pending)

    # 记录删除建议（不执行）
    if prune_suggestions:
        today = date.today().isoformat()
        prune_path = vocabulary.vocab_dir / "prune_suggestions.txt"
        with open(prune_path, "a", encoding="utf-8") as f:
            f.write(f"\n# --- {today} ---\n")
            for item in prune_suggestions:
                t = str(item.get("term", "")).strip()
                r = str(item.get("reason", "")).strip()
                if t:
                    f.write(f"{t}  # {r}\n")
                    result.prune_suggestions.append(t)
