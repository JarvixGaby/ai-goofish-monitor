"""
测试 reporter.py

纯逻辑测试，不依赖 AI 和网络。
"""

import pytest
from pathlib import Path
from reporter import generate, save
from ai_advisor import OpportunityAdvice


def make_gap(keyword, gap_score=5.0, demand=10, supply=2, total=20, avg_price=30.0,
             avg_want=5.0, suggested="¥25-30", titles=None):
    return {
        "keyword": keyword,
        "gap_score": gap_score,
        "demand_posts": demand,
        "virtual_supply": supply,
        "total_listings": total,
        "avg_price": avg_price,
        "avg_want": avg_want,
        "suggested_price": suggested,
        "top_titles": titles or [f"{keyword}教程 秒发", f"{keyword}资料 百度云"],
        "top_items": [],
        "price_median": avg_price,
        "price_p25": avg_price * 0.9,
        "price_p75": avg_price * 1.1,
        "price_distribution": "¥0-10: 0个, ¥10-50: 2个, ¥50+: 0个",
        "newest_pub_date": "2026-04-10",
        "oldest_pub_date": "2026-03-01",
        "recent_7d_count": 3,
        "recent_30d_count": 15,
        "classification_dist": {"virtual": supply, "unknown": max(0, total - supply)},
        "top_want_items": [
            {"title": f"{keyword} 热门", "price": 19.0, "want_num": 12},
        ],
        "want_positive_count": 5,
    }


def make_advice(keyword, has_content=True):
    if not has_content:
        return OpportunityAdvice(keyword=keyword, competitor_analysis="",
                                 differentiation="", error="测试错误")
    return OpportunityAdvice(
        keyword=keyword,
        competitor_analysis="当前竞品主要打基础入门角度，同质化严重",
        differentiation="实战项目角度空缺，需求帖里有人求项目实战",
        recommended_titles=[
            f"{keyword}实战项目 手把手带做 秒发",
            f"保姆级{keyword}教程 从0到1全套 永久更新",
            f"{keyword}进阶教程 独家整合 附送售后群",
        ],
        recommended_description="超详细教程，手把手带你从零开始，永久更新，秒发百度云，购买后附赠专属答疑群。",
        pricing_rationale="竞品均价¥30，建议首发¥19.9 抢排名，稳定后调整至¥29",
    )


# ─────────────────────────────────────────────
# 基础报告生成
# ─────────────────────────────────────────────

def test_generate_basic_structure():
    gaps = [
        make_gap("Cursor教程", gap_score=8.0),
        make_gap("Python教程", gap_score=3.0),
    ]
    report = generate(gaps, "2026-04-15")

    assert "# 闲鱼虚拟商品缺口扫描报告" in report
    assert "2026-04-15" in report
    assert "Cursor教程" in report
    assert "Python教程" in report
    assert "## 数据质量" in report
    assert "总扫描商品数" in report
    assert "中位价" in report
    assert "近7天新品" in report


def test_generate_sorts_by_gap_score():
    gaps = [
        make_gap("低分关键词", gap_score=1.0),
        make_gap("高分关键词", gap_score=10.0),
    ]
    report = generate(gaps, "2026-04-15")

    # 高分应在低分前面出现
    high_pos = report.index("高分关键词")
    low_pos = report.index("低分关键词")
    assert high_pos < low_pos


def test_generate_with_advice():
    gaps = [make_gap("Cursor教程")]
    advice_map = {"Cursor教程": make_advice("Cursor教程")}
    report = generate(gaps, "2026-04-15", advice_map=advice_map)

    assert "AI 卖家建议" in report
    assert "竞品分析" in report
    assert "差异化机会" in report
    assert "推荐标题" in report
    assert "定价策略" in report


def test_generate_without_advice():
    gaps = [make_gap("Python教程")]
    report = generate(gaps, "2026-04-15")

    assert "AI 卖家建议" not in report


def test_generate_skips_failed_advice():
    """AI 建议生成失败（has_content=False）时不渲染该区块。"""
    gaps = [make_gap("某关键词")]
    advice_map = {"某关键词": make_advice("某关键词", has_content=False)}
    report = generate(gaps, "2026-04-15", advice_map=advice_map)

    assert "AI 卖家建议" not in report


def test_generate_table_has_all_keywords():
    keywords = ["A", "B", "C", "D", "E"]
    gaps = [make_gap(k, gap_score=float(i)) for i, k in enumerate(keywords, 1)]
    report = generate(gaps, "2026-04-15")

    for k in keywords:
        assert k in report


# ─────────────────────────────────────────────
# save() 测试
# ─────────────────────────────────────────────

def test_save_creates_file(tmp_path, monkeypatch):
    """save() 应在 reports/ 目录创建文件。"""
    import reporter
    monkeypatch.setattr(reporter, "REPORTS_DIR", tmp_path / "reports")

    content = "# Test Report\n"
    path = save(content, "2026-04-15")

    assert path.exists()
    assert path.read_text(encoding="utf-8") == content
