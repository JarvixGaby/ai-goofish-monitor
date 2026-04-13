"""
报告生成模块。

将缺口分析结果输出为 Markdown 日报，保存到 reports/YYYY-MM-DD.md。
"""

from pathlib import Path

REPORTS_DIR = Path("reports")


def generate(gaps: list[dict], date_str: str) -> str:
    """
    从缺口分析结果列表生成 Markdown 报告文本。

    gaps 中每条记录来自 scanner.calculate_gap()。
    """
    sorted_gaps = sorted(gaps, key=lambda x: x["gap_score"], reverse=True)
    top = sorted_gaps[:10]

    lines = [
        f"# 闲鱼虚拟商品缺口日报 {date_str}",
        "",
        f"> 共扫描 {len(gaps)} 个关键词。缺口分 = 需求帖 ÷ 虚拟供给数，分数越高越值得今天创作。",
        "",
        "## 缺口 Top 10",
        "",
        "| 排名 | 关键词 | 需求帖 | 虚拟供给 | 缺口分 | 现有均价 | 建议定价 |",
        "|:----:|--------|:------:|:--------:|:------:|:--------:|:--------:|",
    ]

    for rank, g in enumerate(top, 1):
        avg_price_str = f"¥{g['avg_price']}" if g["avg_price"] else "—"
        lines.append(
            f"| {rank} | **{g['keyword']}** | {g['demand_posts']} "
            f"| {g['virtual_supply']} | **{g['gap_score']}** "
            f"| {avg_price_str} | {g['suggested_price']} |"
        )

    lines += ["", "---", "", "## 详细分析"]

    for g in top:
        avg_price_str = f"¥{g['avg_price']}" if g["avg_price"] else "无数据"
        lines += [
            "",
            f"### {g['keyword']}",
            f"- 缺口分：**{g['gap_score']}**（需求帖 {g['demand_posts']} / 虚拟供给 {g['virtual_supply']}）",
            f"- 总挂牌数：{g['total_listings']}条",
            f"- 现有虚拟商品均价：{avg_price_str}，平均想要数：{g['avg_want']}",
            f"- 建议定价：{g['suggested_price']}",
        ]
        if g["top_titles"]:
            lines.append("- 现有竞品样本：")
            for t in g["top_titles"]:
                lines.append(f"  - {t}")

    lines += [
        "",
        "---",
        "",
        "## 其余关键词（缺口分从高到低）",
        "",
        "| 关键词 | 缺口分 | 需求帖 | 虚拟供给 | 建议定价 |",
        "|--------|:------:|:------:|:--------:|:--------:|",
    ]
    for g in sorted_gaps[10:]:
        lines.append(
            f"| {g['keyword']} | {g['gap_score']} | {g['demand_posts']} "
            f"| {g['virtual_supply']} | {g['suggested_price']} |"
        )

    return "\n".join(lines) + "\n"


def save(content: str, date_str: str) -> Path:
    """将报告写入 reports/YYYY-MM-DD.md，返回文件路径。"""
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{date_str}.md"
    path.write_text(content, encoding="utf-8")
    print(f"\n报告已保存：{path}")
    return path
