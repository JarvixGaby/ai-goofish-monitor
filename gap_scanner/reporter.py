"""
Markdown 报告生成模块（纯逻辑，无 AI 依赖）。

generate() 将缺口分析结果和可选的 AI 建议渲染为 Markdown 报告。
save() 将报告写入 reports/{date}.md。

Phase 2 扩展：若传入 advice_map，在 Top 5 缺口下方追加
「AI 卖家建议」区块，包含竞品分析、差异化角度、推荐标题等。
"""

from __future__ import annotations

from pathlib import Path

REPORTS_DIR = Path("reports")


def generate(
    gaps: list[dict],
    date_str: str,
    advice_map: "dict | None" = None,
) -> str:
    """
    生成 Markdown 报告文本。

    参数：
        gaps:       calculate_gap() 返回的缺口数据列表
        date_str:   日期字符串（YYYY-MM-DD）
        advice_map: {keyword: OpportunityAdvice} 字典（Phase 2，可选）

    返回：完整的 Markdown 字符串
    """
    sorted_gaps = sorted(gaps, key=lambda x: x["gap_score"], reverse=True)

    lines = [
        f"# 闲鱼虚拟商品缺口扫描报告",
        f"",
        f"日期：{date_str}",
        f"",
        f"---",
        f"",
        f"## 缺口分排行",
        f"",
        "| 排名 | 关键词 | 缺口分 | 需求帖 | 虚拟供给 | 总挂牌 | 均价 | 建议定价 |",
        "|------|--------|--------|--------|----------|--------|------|----------|",
    ]

    for rank, gap in enumerate(sorted_gaps, 1):
        lines.append(
            f"| {rank} | {gap['keyword']} | {gap['gap_score']} "
            f"| {gap['demand_posts']} | {gap['virtual_supply']} "
            f"| {gap['total_listings']} | ¥{gap['avg_price']:.0f} "
            f"| {gap['suggested_price']} |"
        )

    # 详情区（Top 10）
    lines += [
        "",
        "---",
        "",
        "## Top 10 详情",
        "",
    ]

    for rank, gap in enumerate(sorted_gaps[:10], 1):
        lines += [
            f"### {rank}. {gap['keyword']}",
            "",
            f"- **缺口分**：{gap['gap_score']}",
            f"- **需求帖 / 虚拟供给**：{gap['demand_posts']} / {gap['virtual_supply']}（总挂牌 {gap['total_listings']}）",
            f"- **竞品均价**：¥{gap['avg_price']:.0f}（平均 {gap['avg_want']:.0f} 人想要）",
            f"- **建议定价**：{gap['suggested_price']}",
            "",
        ]

        # 展示代表性竞品标题
        if gap.get("top_titles"):
            lines.append("**竞品标题参考**：")
            for t in gap["top_titles"][:3]:
                lines.append(f"> {t}")
            lines.append("")

        # Phase 2：AI 卖家建议
        if advice_map:
            advice = advice_map.get(gap["keyword"])
            if advice and advice.has_content:
                lines += _render_advice(advice)

    # 页脚
    lines += [
        "---",
        "",
        f"*报告生成时间：{date_str}*",
        "",
        "> 缺口分公式：需求帖数 ÷ max(虚拟供给数, 1)。分值越高，供需缺口越大。",
        "",
    ]

    return "\n".join(lines)


def _render_advice(advice) -> list[str]:
    """渲染单个关键词的 AI 建议区块。"""
    lines = [
        f"#### AI 卖家建议 — {advice.keyword}",
        "",
    ]

    if advice.competitor_analysis:
        lines += [
            f"- **竞品分析**：{advice.competitor_analysis}",
        ]

    if advice.differentiation:
        lines += [
            f"- **差异化机会**：{advice.differentiation}",
        ]

    if advice.recommended_titles:
        lines += ["- **推荐标题**："]
        for i, title in enumerate(advice.recommended_titles, 1):
            lines.append(f"  {i}. {title}")

    if advice.recommended_description:
        lines += [
            f"- **商品描述**：",
            f"  > {advice.recommended_description}",
        ]

    if advice.pricing_rationale:
        lines += [
            f"- **定价策略**：{advice.pricing_rationale}",
        ]

    lines.append("")
    return lines


def save(content: str, date_str: str) -> Path:
    """将报告内容写入 reports/{date_str}.md，返回文件路径。"""
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{date_str}.md"
    path.write_text(content, encoding="utf-8")
    print(f"\n报告已保存：{path}")
    return path
