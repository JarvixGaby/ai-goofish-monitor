"""
测试 vocabulary.py

所有测试使用临时目录，不依赖真实 vocab/ 文件，完全离线可运行。
"""

import pytest
from pathlib import Path
from vocabulary import Vocabulary, TermEntry, CATEGORY_FILES


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def vocab_dir(tmp_path: Path) -> Path:
    """返回一个空的临时词库目录。"""
    return tmp_path / "vocab"


@pytest.fixture
def vocab(vocab_dir: Path) -> Vocabulary:
    """返回指向临时目录的 Vocabulary 实例。"""
    return Vocabulary(vocab_dir)


@pytest.fixture
def seeded_vocab(vocab_dir: Path) -> Vocabulary:
    """预置了部分词条的 Vocabulary 实例。"""
    vocab_dir.mkdir(parents=True, exist_ok=True)
    (vocab_dir / "virtual_weak.txt").write_text(
        "# 注释行\n教程\n\n", encoding="utf-8"
    )
    (vocab_dir / "virtual_strong.txt").write_text(
        "百度云\n秒发\n", encoding="utf-8"
    )
    (vocab_dir / "demand_signal.txt").write_text(
        "求\n有没有\n蹲一个\n", encoding="utf-8"
    )
    (vocab_dir / "delivery_method.txt").write_text(
        "提取码\n链接发送\n", encoding="utf-8"
    )
    (vocab_dir / "blacklist.txt").write_text(
        "实体书\n", encoding="utf-8"
    )
    return Vocabulary(vocab_dir)


# ─────────────────────────────────────────────
# load() 测试
# ─────────────────────────────────────────────

class TestLoad:
    def test_load_nonexistent_returns_empty(self, vocab: Vocabulary):
        result = vocab.load("virtual_strong")
        assert result == set()

    def test_load_virtual_supply_union(self, seeded_vocab: Vocabulary):
        u = seeded_vocab.load("virtual_supply")
        assert "教程" in u and "百度云" in u and "秒发" in u

    def test_load_skips_comments_and_blank_lines(self, seeded_vocab: Vocabulary):
        terms = seeded_vocab.load("virtual_weak")
        assert "# 注释行" not in terms
        assert "" not in terms
        assert "教程" in terms

    def test_load_cached(self, seeded_vocab: Vocabulary):
        first = seeded_vocab.load("virtual_weak")
        second = seeded_vocab.load("virtual_weak")
        assert first is second  # 同一对象，来自缓存

    def test_reload_clears_cache(self, seeded_vocab: Vocabulary, vocab_dir: Path):
        seeded_vocab.load("virtual_weak")
        (vocab_dir / "virtual_weak.txt").write_text("新词\n", encoding="utf-8")
        seeded_vocab.reload()
        terms = seeded_vocab.load("virtual_weak")
        assert "新词" in terms
        assert "教程" not in terms


# ─────────────────────────────────────────────
# match() 测试
# ─────────────────────────────────────────────

class TestMatch:
    def test_empty_title_returns_unknown(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("")
        assert result.classification == "unknown"
        assert result.confidence == 0.0

    def test_strong_signal_virtual(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("Cursor教程 百度云 秒发")
        assert result.classification == "virtual"
        assert result.confidence == 1.0
        assert "百度云" in result.matched_terms

    def test_demand_signal_before_virtual(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("蹲一个AI工作流教程")
        assert result.classification == "demand"
        assert "蹲一个" in result.matched_terms

    def test_pure_demand_match(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("有没有好用的AI工具推荐")
        assert result.classification == "demand"
        assert "有没有" in result.matched_terms

    def test_blacklist_takes_priority(self, seeded_vocab: Vocabulary):
        """黑名单优先于虚拟信号。"""
        result = seeded_vocab.match("实体书教程推荐")
        assert result.classification == "blacklisted"
        assert "实体书" in result.matched_terms

    def test_unknown_when_no_match(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("二手iPhone 15 256G 成色9成新")
        assert result.classification == "unknown"
        assert result.matched_terms == []

    def test_delivery_method_match(self, seeded_vocab: Vocabulary):
        """delivery_method 单独命中（无强/弱虚拟词）。"""
        result = seeded_vocab.match("发送提取码给你")
        assert result.classification == "delivery"
        assert result.confidence == 0.7

    def test_weak_only_weak_virtual(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("可转债入门教程")
        assert result.classification == "weak_virtual"
        assert result.confidence == 0.4
        assert result.matched_terms == ["教程"]

    def test_two_weak_virtual(self, seeded_vocab: Vocabulary):
        (seeded_vocab.vocab_dir / "virtual_weak.txt").write_text(
            "教程\n资料\n", encoding="utf-8"
        )
        seeded_vocab.reload()
        result = seeded_vocab.match("教程资料合集")
        assert result.classification == "virtual"
        assert abs(result.confidence - 0.8) < 0.01

    def test_one_weak_plus_delivery(self, seeded_vocab: Vocabulary):
        result = seeded_vocab.match("可转债教程 提取码发货")
        assert result.classification == "virtual"
        assert abs(result.confidence - 0.7) < 0.01


# ─────────────────────────────────────────────
# add_terms() 测试
# ─────────────────────────────────────────────

class TestAddTerms:
    def test_add_new_terms(self, vocab: Vocabulary, vocab_dir: Path):
        vocab_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            TermEntry(term="新词A", confidence=0.9, source="ai", reason="测试"),
            TermEntry(term="新词B", confidence=0.88, source="ai", reason="测试"),
        ]
        added = vocab.add_terms("virtual_supply", entries)
        assert added == 2
        vocab.reload()
        terms = vocab.load("virtual_supply")
        assert "新词A" in terms
        assert "新词B" in terms

    def test_skip_duplicate_terms(self, seeded_vocab: Vocabulary):
        entries = [TermEntry(term="教程", confidence=0.9, source="ai")]
        added = seeded_vocab.add_terms("virtual_supply", entries)
        assert added == 0

    def test_add_to_empty_category(self, vocab: Vocabulary, vocab_dir: Path):
        vocab_dir.mkdir(parents=True, exist_ok=True)
        entries = [TermEntry(term="测试词", confidence=0.95, source="manual")]
        added = vocab.add_terms("virtual_supply", entries)
        assert added == 1
        vocab.reload()
        assert "测试词" in vocab.load("virtual_supply")

    def test_add_creates_file_if_missing(self, vocab: Vocabulary, vocab_dir: Path):
        vocab_dir.mkdir(parents=True, exist_ok=True)
        path = vocab_dir / "virtual_weak.txt"
        assert not path.exists()
        vocab.add_terms("virtual_supply", [TermEntry("新词", 0.9, "ai")])
        assert path.exists()


# ─────────────────────────────────────────────
# remove_terms() 测试
# ─────────────────────────────────────────────

class TestRemoveTerms:
    def test_remove_existing_term(self, seeded_vocab: Vocabulary, vocab_dir: Path):
        removed = seeded_vocab.remove_terms("virtual_supply", ["教程"])
        assert removed == 1
        seeded_vocab.reload()
        assert "教程" not in seeded_vocab.load("virtual_supply")

    def test_remove_nonexistent_term(self, seeded_vocab: Vocabulary):
        removed = seeded_vocab.remove_terms("virtual_supply", ["不存在的词"])
        assert removed == 0

    def test_remove_preserves_other_terms(self, seeded_vocab: Vocabulary):
        seeded_vocab.remove_terms("virtual_supply", ["教程"])
        seeded_vocab.reload()
        terms = seeded_vocab.load("virtual_supply")
        assert "百度云" in terms
        assert "秒发" in terms


# ─────────────────────────────────────────────
# pending_review 测试
# ─────────────────────────────────────────────

class TestPending:
    def test_add_to_pending(self, vocab: Vocabulary, vocab_dir: Path):
        vocab_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            TermEntry("候选词A", 0.72, "ai", "可能是暗语"),
            TermEntry("候选词B", 0.65, "ai", "需要确认"),
        ]
        vocab.add_to_pending(entries)
        loaded = vocab.load_pending()
        terms = [e.term for e in loaded]
        assert "候选词A" in terms
        assert "候选词B" in terms

    def test_load_pending_empty(self, vocab: Vocabulary, vocab_dir: Path):
        vocab_dir.mkdir(parents=True, exist_ok=True)
        result = vocab.load_pending()
        assert result == []


# ─────────────────────────────────────────────
# stats() 测试
# ─────────────────────────────────────────────

class TestStats:
    def test_stats_returns_counts(self, seeded_vocab: Vocabulary):
        stats = seeded_vocab.stats()
        assert stats["virtual_weak"] == 1
        assert stats["virtual_strong"] == 2
        assert stats["demand_signal"] == 3
        assert stats["blacklist"] == 1
        assert "virtual_supply" not in stats
        assert len(stats) == len(CATEGORY_FILES)

    def test_stats_empty_vocab(self, vocab: Vocabulary):
        stats = vocab.stats()
        for count in stats.values():
            assert count == 0
