"""
测试 ai_classifier.py

使用 Mock AIClient，不发起真实 API 调用。
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ai_classifier import classify_batch, ClassifyResult, _parse_response, _fallback_results
from ai_client import AIClientError
from vocabulary import Vocabulary


def make_item(title: str, price: float = 0.0, classification: str = "unknown") -> dict:
    return {
        "title": title,
        "price": price,
        "classification": classification,
        "is_virtual": False,
        "is_demand": False,
        "matched_terms": [],
    }


def make_ai_response(results: list[dict]) -> dict:
    return {"results": results}


# ─────────────────────────────────────────────
# 正常流程测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_virtual_item():
    client = MagicMock()
    client.chat = AsyncMock(return_value=make_ai_response([
        {"index": 0, "classification": "virtual", "signal_terms": ["秒发", "百度云"], "confidence": 0.95}
    ]))

    items = [make_item("Cursor教程 百度云 秒发")]
    results = await classify_batch(items, client)

    assert len(results) == 1
    assert results[0].classification == "virtual"
    assert "秒发" in results[0].signal_terms
    assert items[0]["classification"] == "virtual"   # 回写验证
    assert items[0]["is_virtual"] is True


@pytest.mark.asyncio
async def test_classify_demand_item():
    client = MagicMock()
    client.chat = AsyncMock(return_value=make_ai_response([
        {"index": 0, "classification": "demand", "signal_terms": ["求"], "confidence": 0.88}
    ]))

    items = [make_item("蹲一个AI工作流资源")]
    results = await classify_batch(items, client)

    assert results[0].classification == "demand"
    assert items[0]["is_demand"] is True


@pytest.mark.asyncio
async def test_skip_already_classified_items():
    """已分类的 item（非 unknown）不应发送给 AI。"""
    client = MagicMock()
    client.chat = AsyncMock()

    items = [
        make_item("已分类虚拟商品", classification="virtual"),
        make_item("已分类实物", classification="physical"),
    ]
    results = await classify_batch(items, client)

    assert results == []
    client.chat.assert_not_called()  # AI 没被调用


@pytest.mark.asyncio
async def test_mixed_classified_and_unknown():
    """已分类和未分类混合时，只处理 unknown。"""
    client = MagicMock()
    client.chat = AsyncMock(return_value=make_ai_response([
        {"index": 0, "classification": "physical", "signal_terms": [], "confidence": 0.9}
    ]))

    items = [
        make_item("已分类虚拟", classification="virtual"),
        make_item("未知商品 iPhone"),              # unknown，索引 1
    ]
    results = await classify_batch(items, client)

    assert len(results) == 1
    assert results[0].classification == "physical"
    assert items[1]["classification"] == "physical"
    assert items[0]["classification"] == "virtual"  # 不变


# ─────────────────────────────────────────────
# Fallback 测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_on_ai_error():
    """AI 抛出 AIClientError 时，结果全部为 unknown。"""
    client = MagicMock()
    client.chat = AsyncMock(side_effect=AIClientError("API 不可用"))

    items = [make_item("某商品"), make_item("另一个商品")]
    results = await classify_batch(items, client)

    assert all(r.classification == "unknown" for r in results)
    assert all(r.confidence == 0.0 for r in results)
    # items 中的 classification 保持 unknown（不被修改为 unknown 以外）
    assert items[0]["classification"] == "unknown"


@pytest.mark.asyncio
async def test_fallback_on_malformed_json():
    """AI 返回结构不符合预期（缺少 results 字段）时，item 补全为 'other'。
    注：'other' 与 'unknown' 的区别：
        - unknown = AI 不可用（APIError）
        - other   = AI 可用但无法分类
    """
    client = MagicMock()
    client.chat = AsyncMock(return_value={"unexpected_key": "garbage"})

    items = [make_item("某商品")]
    results = await classify_batch(items, client)

    assert results[0].classification == "other"


# ─────────────────────────────────────────────
# _parse_response 单元测试
# ─────────────────────────────────────────────

def test_parse_response_handles_invalid_classification():
    """AI 返回不在白名单的分类时，应被转为 'other'。"""
    batch = [(0, make_item("某商品"))]
    response = make_ai_response([
        {"index": 0, "classification": "unknown_type", "signal_terms": [], "confidence": 0.5}
    ])
    results = _parse_response(response, batch)
    assert results[0].classification == "other"


def test_parse_response_fills_missing_items():
    """AI 没有返回某些条目时，应补全为 other。"""
    batch = [(0, make_item("商品A")), (1, make_item("商品B"))]
    response = make_ai_response([
        {"index": 0, "classification": "virtual", "signal_terms": [], "confidence": 0.9}
        # index=1 缺失
    ])
    results = _parse_response(response, batch)
    indices = {r.index for r in results}
    assert 0 in indices
    assert 1 in indices
    missing = next(r for r in results if r.index == 1)
    assert missing.classification == "other"


# ─────────────────────────────────────────────
# signal_terms 回传测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_terms_written_to_items():
    """signal_terms 应被写回 items 的 matched_terms 字段。"""
    client = MagicMock()
    client.chat = AsyncMock(return_value=make_ai_response([
        {"index": 0, "classification": "virtual", "signal_terms": ["永久更新", "售后群"], "confidence": 0.9}
    ]))

    items = [make_item("AI课程 永久更新 附送售后群")]
    await classify_batch(items, client)

    assert "永久更新" in items[0]["matched_terms"]
    assert "售后群" in items[0]["matched_terms"]
