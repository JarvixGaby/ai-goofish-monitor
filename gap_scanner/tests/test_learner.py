"""
测试 vocab_learner.py

使用 Mock AIClient，不发起真实 API 调用。
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from vocab_learner import learn_from_scan, LearnResult, _process_ai_response
from vocabulary import Vocabulary, TermEntry
from config import Settings


def make_settings(auto_threshold=0.85, review_threshold=0.60, vocab_dir=None) -> Settings:
    return Settings(
        ai_base_url="https://test.api.example.com",
        ai_api_key="sk-test",
        ai_model="test-model",
        vocab_auto_threshold=auto_threshold,
        vocab_review_threshold=review_threshold,
        vocab_dir=vocab_dir or Path("/tmp/vocab"),
        ai_timeout=5,
        ai_max_retries=0,
    )


def make_item(title: str, classification: str = "unknown", terms: list = None) -> dict:
    return {
        "title": title,
        "price": 9.9,
        "classification": classification,
        "matched_terms": terms or [],
        "is_virtual": classification == "virtual",
        "is_demand": classification == "demand",
    }


def make_ai_learn_response(new_terms: list, prune: list = None) -> dict:
    return {
        "new_terms": new_terms,
        "prune_suggestions": prune or [],
    }


# ──────────────────────────────────────────────────────────
# _process_ai_response 单元测试
# ──────────────────────────────────────────────────────────

class TestProcessAiResponse:
    def test_high_confidence_auto_added(self, tmp_path):
        vocab = Vocabulary(tmp_path / "vocab")
        settings = make_settings(auto_threshold=0.85)
        result = LearnResult()

        response = make_ai_learn_response([
            {"term": "永久更新", "category": "virtual_supply", "confidence": 0.92, "reason": "测试"},
            {"term": "保姆级", "category": "virtual_supply", "confidence": 0.90, "reason": "测试"},
        ])
        _process_ai_response(response, vocab, settings, result)

        assert len(result.auto_added) == 2
        assert len(result.pending_review) == 0
        vocab.reload()
        assert "永久更新" in vocab.load("virtual_supply")
        assert "保姆级" in vocab.load("virtual_supply")

    def test_medium_confidence_goes_to_pending(self, tmp_path):
        vocab = Vocabulary(tmp_path / "vocab")
        settings = make_settings(auto_threshold=0.85, review_threshold=0.60)
        result = LearnResult()

        response = make_ai_learn_response([
            {"term": "候选词", "category": "virtual_supply", "confidence": 0.72, "reason": "可能"},
        ])
        _process_ai_response(response, vocab, settings, result)

        assert len(result.auto_added) == 0
        assert len(result.pending_review) == 1
        # pending_review.txt 已写入
        pending = vocab.load_pending()
        assert any(e.term == "候选词" for e in pending)

    def test_low_confidence_ignored(self, tmp_path):
        vocab = Vocabulary(tmp_path / "vocab")
        settings = make_settings(review_threshold=0.60)
        result = LearnResult()

        response = make_ai_learn_response([
            {"term": "低置信词", "category": "virtual_supply", "confidence": 0.45, "reason": "不确定"},
        ])
        _process_ai_response(response, vocab, settings, result)

        assert len(result.auto_added) == 0
        assert len(result.pending_review) == 0

    def test_deduplication_skips_existing(self, tmp_path):
        """已在词库中的词不重复添加。"""
        vdir = tmp_path / "vocab"
        vdir.mkdir()
        (vdir / "virtual_supply.txt").write_text("教程\n", encoding="utf-8")
        vocab = Vocabulary(vdir)
        settings = make_settings()
        result = LearnResult()

        response = make_ai_learn_response([
            {"term": "教程", "category": "virtual_supply", "confidence": 0.95, "reason": "已有"},
        ])
        _process_ai_response(response, vocab, settings, result)

        assert len(result.auto_added) == 0

    def test_prune_suggestions_written_to_file(self, tmp_path):
        vocab = Vocabulary(tmp_path / "vocab")
        settings = make_settings()
        result = LearnResult()

        response = make_ai_learn_response(
            new_terms=[],
            prune=[{"term": "过时词", "reason": "现在不用了"}],
        )
        _process_ai_response(response, vocab, settings, result)

        assert "过时词" in result.prune_suggestions
        prune_file = vocab.vocab_dir / "prune_suggestions.txt"
        assert prune_file.exists()
        assert "过时词" in prune_file.read_text(encoding="utf-8")

    def test_invalid_category_defaults_to_virtual(self, tmp_path):
        vocab = Vocabulary(tmp_path / "vocab")
        settings = make_settings()
        result = LearnResult()

        response = make_ai_learn_response([
            {"term": "新词", "category": "invalid_cat", "confidence": 0.90, "reason": "测试"},
        ])
        _process_ai_response(response, vocab, settings, result)

        assert len(result.auto_added) == 1
        assert result.auto_added[0].category == "virtual_supply"


# ──────────────────────────────────────────────────────────
# learn_from_scan 集成测试
# ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_learn_from_scan_basic(tmp_path):
    vocab = Vocabulary(tmp_path / "vocab")
    settings = make_settings(vocab_dir=tmp_path / "vocab")
    client = MagicMock()
    client.chat = AsyncMock(return_value=make_ai_learn_response([
        {"term": "秒发", "category": "virtual_supply", "confidence": 0.93, "reason": "交付方式"},
    ]))

    items = [
        make_item("Cursor教程 秒发百度云", classification="unknown"),
        make_item("iPhone 15 二手", classification="physical"),
    ]
    result = await learn_from_scan(items, vocab, client, settings)

    assert result.titles_analyzed >= 1
    assert any(e.term == "秒发" for e in result.auto_added)


@pytest.mark.asyncio
async def test_learn_skips_when_no_unknown(tmp_path):
    """所有 item 已分类时，无需调用 AI。"""
    vocab = Vocabulary(tmp_path / "vocab")
    settings = make_settings(vocab_dir=tmp_path / "vocab")
    client = MagicMock()
    client.chat = AsyncMock()

    items = [
        make_item("某商品", classification="virtual"),
        make_item("另一个", classification="physical"),
    ]
    result = await learn_from_scan(items, vocab, client, settings)

    assert result.titles_analyzed == 0


@pytest.mark.asyncio
async def test_learn_signal_terms_from_classifier(tmp_path):
    """AI 分类器发现的 signal_terms 也能被学习。"""
    vdir = tmp_path / "vocab"
    vocab = Vocabulary(vdir)
    settings = make_settings(auto_threshold=0.80, vocab_dir=vdir)
    client = MagicMock()
    client.chat = AsyncMock(return_value=make_ai_learn_response([]))

    # 模拟分类器已经标注了 signal_terms
    items = [
        make_item("AI课程永久更新", classification="virtual",
                  terms=["永久更新", "从0到1"]),
    ]
    result = await learn_from_scan(items, vocab, client, settings)

    vocab.reload()
    terms = vocab.load("virtual_supply")
    # signal_terms 置信度 0.80 >= auto_threshold 0.80，应自动加入
    assert "永久更新" in terms or "从0到1" in terms


@pytest.mark.asyncio
async def test_learn_ai_failure_graceful(tmp_path):
    """AI 失败时不抛出异常，返回空结果。"""
    from ai_client import AIClientError
    vocab = Vocabulary(tmp_path / "vocab")
    settings = make_settings(vocab_dir=tmp_path / "vocab")
    client = MagicMock()
    client.chat = AsyncMock(side_effect=AIClientError("服务不可用"))

    items = [make_item("某个未知商品", classification="unknown")]
    result = await learn_from_scan(items, vocab, client, settings)

    assert result.auto_added == []
    assert result.pending_review == []
