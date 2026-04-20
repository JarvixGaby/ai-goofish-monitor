"""测试 scanner.calculate_gap 与 weak_virtual 统计。"""

import pytest
from pathlib import Path

from scanner import calculate_gap
from vocabulary import Vocabulary


@pytest.fixture
def vocab(tmp_path: Path) -> Vocabulary:
    d = tmp_path / "vocab"
    d.mkdir()
    (d / "virtual_strong.txt").write_text("秒发\n", encoding="utf-8")
    (d / "virtual_weak.txt").write_text("教程\n资料\n", encoding="utf-8")
    (d / "delivery_method.txt").write_text("提取码\n", encoding="utf-8")
    (d / "demand_signal.txt").write_text("", encoding="utf-8")
    (d / "blacklist.txt").write_text("", encoding="utf-8")
    return Vocabulary(d)


def make_item(title: str) -> dict:
    return {"title": title, "price": 10.0, "want_num": 1}


def test_weak_virtual_not_in_supply_count(vocab: Vocabulary):
    items = [make_item("仅含教程一词的实体书标题")]
    gap = calculate_gap("kw", items, 5, vocabulary=vocab)
    assert gap["virtual_supply"] == 0
    assert gap["weak_virtual_count"] == 1


def test_two_weak_counts_as_virtual_supply(vocab: Vocabulary):
    items = [make_item("教程资料打包")]
    gap = calculate_gap("kw", items, 5, vocabulary=vocab)
    assert gap["virtual_supply"] == 1
    assert gap["weak_virtual_count"] == 0


def test_preserves_preclassified_weak_virtual():
    items = [{"title": "x", "price": 1, "want_num": 0, "classification": "weak_virtual"}]
    gap = calculate_gap("kw", items, 3, vocabulary=None)
    assert gap["virtual_supply"] == 0
    assert gap["weak_virtual_count"] == 1
