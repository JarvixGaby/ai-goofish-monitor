"""
AI 兜底分类模块。

对词库未命中（classification == "unknown"）的商品标题，
批量调用 LLM 进行语义分类。

分类结果：
    virtual   — 虚拟商品/服务（教程、资料、代做等数字内容）
    demand    — 求购/需求帖（买家在找某东西）
    physical  — 实物商品（二手手机、衣服等）
    other     — 无法判断或与闲鱼虚拟商品无关

此模块不直接修改词库；新发现的信号词通过 signal_terms 字段
回传给调用方，由 vocab_learner 决定是否写入词库。

Fallback 策略：AI 不可用时，所有未命中项保持 "unknown" 分类，
不影响已通过词库匹配的结果。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ai_client import AIClient, AIClientError
from vocabulary import Vocabulary

logger = logging.getLogger(__name__)

# 每次发送给 AI 的最大标题数（避免 prompt 过长）
BATCH_SIZE = 40

_SYSTEM_PROMPT = """\
你是一位专注于中国二手交易平台「闲鱼」的虚拟商品市场分析专家。

你的任务：对给定的商品标题列表进行分类。

分类规则：
- virtual：虚拟商品或服务，包括：
    * 教程/课程/资料/电子书/PDF（无论是否用暗语描述）
    * 代做/代操作/辅导/带练等服务
    * 网盘资源、链接发送的数字内容
    * 使用暗语的虚拟商品，如"秒发"、"百度云"、"保姆级"、"手把手"等

- demand：求购/需求帖，买家在找某个东西，包括：
    * 含"求"、"有没有"、"蹲一个"、"dd"、"急需"等词
    * 明显是在寻找而非出售

- physical：实物商品（二手手机、衣服、书籍实体、电子设备等）

- other：与以上分类无关，或无法判断

关键提示：
1. 优先识别虚拟商品的「闲鱼黑话」，不要被表面措辞迷惑
2. 同时从标题中提取能区分「虚拟商品」和「需求帖」的信号词

请以 JSON 格式返回，结构如下：
{
  "results": [
    {
      "index": 0,
      "classification": "virtual",
      "signal_terms": ["秒发", "百度云"],
      "confidence": 0.92
    },
    ...
  ]
}

confidence 范围 0.0-1.0，表示你对该分类的把握程度。
"""


@dataclass
class ClassifyResult:
    """单条标题的 AI 分类结果。"""
    index: int                   # 原始 items 列表中的索引
    title: str
    classification: str          # "virtual" | "demand" | "physical" | "other" | "unknown"
    signal_terms: list[str]      # AI 发现的信号词（供词库学习使用）
    confidence: float


async def classify_batch(
    items: list[dict],
    client: AIClient,
    vocabulary: Vocabulary | None = None,
) -> list[ClassifyResult]:
    """
    对商品列表中 classification == "unknown" 的项目进行 AI 分类。

    参数：
        items:      完整商品列表（包含已分类和未分类的）
        client:     AIClient 实例
        vocabulary: 可选，用于 fallback 的词库（当前未使用，预留接口）

    返回：仅含未命中项的分类结果列表（已分类项不再重复处理）

    副作用：直接修改 items 中 "unknown" 项的 "classification" 字段
    """
    unknowns = [
        (idx, item)
        for idx, item in enumerate(items)
        if item.get("classification", "unknown") == "unknown"
    ]

    if not unknowns:
        return []

    all_results: list[ClassifyResult] = []

    # 分批处理
    for batch_start in range(0, len(unknowns), BATCH_SIZE):
        batch = unknowns[batch_start: batch_start + BATCH_SIZE]
        batch_results = await _classify_single_batch(batch, client)
        all_results.extend(batch_results)

        # 回写分类结果到原始 items
        for result in batch_results:
            items[result.index]["classification"] = result.classification
            items[result.index]["matched_terms"] = result.signal_terms
            # 同步兼容字段
            items[result.index]["is_virtual"] = result.classification == "virtual"
            items[result.index]["is_demand"] = result.classification == "demand"

    return all_results


async def _classify_single_batch(
    batch: list[tuple[int, dict]],
    client: AIClient,
) -> list[ClassifyResult]:
    """处理单个批次，失败时返回全部 unknown。"""
    # 构建用户消息
    titles_block = "\n".join(
        f"{i}. \"{item['title']}\"（¥{item.get('price', 0):.0f}）"
        for i, (_, item) in enumerate(batch)
    )
    user_msg = f"请分类以下 {len(batch)} 条闲鱼商品标题：\n\n{titles_block}"

    try:
        response = await client.chat(_SYSTEM_PROMPT, user_msg)
        return _parse_response(response, batch)

    except AIClientError as e:
        logger.warning(f"[classifier] AI 分类失败，回退到 unknown：{e}")
        return _fallback_results(batch)
    except Exception as e:
        logger.warning(f"[classifier] 意外错误，回退到 unknown：{e}")
        return _fallback_results(batch)


def _parse_response(
    response: dict,
    batch: list[tuple[int, dict]],
) -> list[ClassifyResult]:
    """解析 AI 返回的 JSON 结果，解析失败时 fallback。"""
    try:
        ai_results = response.get("results", [])
        if not isinstance(ai_results, list):
            raise ValueError("results 字段不是列表")

        parsed: list[ClassifyResult] = []
        for ai_item in ai_results:
            seq_idx = ai_item.get("index", -1)
            if seq_idx < 0 or seq_idx >= len(batch):
                continue

            original_idx, item = batch[seq_idx]
            classification = str(ai_item.get("classification", "other"))
            if classification not in ("virtual", "demand", "physical", "other"):
                classification = "other"

            parsed.append(ClassifyResult(
                index=original_idx,
                title=item.get("title", ""),
                classification=classification,
                signal_terms=ai_item.get("signal_terms", []),
                confidence=float(ai_item.get("confidence", 0.7)),
            ))

        # 补全 AI 没有返回的条目（保守处理为 other）
        returned_seqs = {r.index for r in parsed}
        for seq_idx, (original_idx, item) in enumerate(batch):
            if original_idx not in returned_seqs:
                parsed.append(ClassifyResult(
                    index=original_idx,
                    title=item.get("title", ""),
                    classification="other",
                    signal_terms=[],
                    confidence=0.5,
                ))

        return parsed

    except Exception as e:
        logger.warning(f"[classifier] 解析 AI 响应失败：{e}，回退到 unknown")
        return _fallback_results(batch)


def _fallback_results(batch: list[tuple[int, dict]]) -> list[ClassifyResult]:
    """AI 不可用时的 fallback：全部保持 unknown。"""
    return [
        ClassifyResult(
            index=original_idx,
            title=item.get("title", ""),
            classification="unknown",
            signal_terms=[],
            confidence=0.0,
        )
        for original_idx, item in batch
    ]
