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


def _pct(n: float, d: float) -> float:
    if d <= 0:
        return 0.0
    return round(100.0 * n / d, 1)


def _keyword_cell(gap: dict) -> str:
    """缺口分排行表中的关键词列：invalid / noisy 时追加标记。"""
    kw = str(gap.get("keyword", ""))
    status = gap.get("keyword_status")
    if status == "invalid":
        return f"{kw} [无效]"
    if status == "noisy":
        return f"{kw} [噪音]"
    return kw


def _status_label_zh(status: str) -> str:
    if status == "invalid":
        return "无效"
    if status == "noisy":
        return "噪音"
    if status == "valid":
        return "有效"
    return str(status)


def _find_invalid_keywords(gaps: list[dict]) -> list[str]:
    """
    启发式：单关键词下 unknown 占比过高，说明搜索结果与虚拟品词库偏离严重。
    """
    bad: list[str] = []
    for g in gaps:
        total = int(g.get("total_listings") or 0)
        if total < 5:
            continue
        dist = g.get("classification_dist") or {}
        unk = int(dist.get("unknown", 0))
        if unk / total >= 0.6:
            bad.append(str(g.get("keyword", "")))
    return bad


def _render_data_quality(gaps: list[dict]) -> list[str]:
    total_items = sum(int(g.get("total_listings") or 0) for g in gaps)
    non_unknown = 0
    want_pos = 0
    for g in gaps:
        dist = g.get("classification_dist") or {}
        unk = int(dist.get("unknown", 0))
        tl = int(g.get("total_listings") or 0)
        non_unknown += max(0, tl - unk)
        want_pos += int(g.get("want_positive_count") or 0)

    cov = _pct(non_unknown, float(total_items))
    want_eff = _pct(float(want_pos), float(total_items))
    invalid_kw = _find_invalid_keywords(gaps)
    invalid_line = "、".join(invalid_kw) if invalid_kw else "（无）"

    return [
        "## 数据质量",
        "",
        f"- 总扫描商品数：{total_items}",
        f"- 分类覆盖率：{cov}%（非 unknown 占比）",
        f"- want_num 有效率：{want_eff}%",
        f"- 无效关键词（搜索结果严重偏离）：{invalid_line}",
        "",
    ]


def generate(
    gaps: list[dict],
    date_str: str,
    advice_map: "dict | None" = None,
    keyword_evaluations: "dict | None" = None,
) -> str:
    """
    生成 Markdown 报告文本。

    参数：
        gaps:       calculate_gap() 返回的缺口数据列表
        date_str:   日期字符串（YYYY-MM-DD）
        advice_map: {keyword: OpportunityAdvice} 字典（Phase 2，可选）
        keyword_evaluations: {keyword: KeywordEvaluation}（可选；展示数据以 gaps 内嵌字段为准）

    返回：完整的 Markdown 字符串
    """
    if keyword_evaluations:
        for g in gaps:
            kw = g.get("keyword")
            ev = keyword_evaluations.get(kw) if kw else None
            if ev is None:
                continue
            g.setdefault("keyword_status", getattr(ev, "status", None))
            g.setdefault("relevance_score", getattr(ev, "relevance_score", None))
            g.setdefault("evaluation_reason", getattr(ev, "reason", None))
            g.setdefault(
                "suggested_alternatives",
                getattr(ev, "suggested_alternatives", []) or [],
            )

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
        "| 排名 | 关键词 | 缺口分 | 需求帖 | 虚拟供给 | 总挂牌 | 均价 | 中位价 | 近7天新品 | 建议定价 |",
        "|------|--------|--------|--------|----------|--------|------|--------|----------|----------|",
    ]

    for rank, gap in enumerate(sorted_gaps, 1):
        med = gap.get("price_median", gap.get("avg_price", 0)) or 0
        r7 = int(gap.get("recent_7d_count") or 0)
        lines.append(
            f"| {rank} | {_keyword_cell(gap)} | {gap['gap_score']} "
            f"| {gap['demand_posts']} | {gap['virtual_supply']} "
            f"| {gap['total_listings']} | ¥{gap['avg_price']:.0f} "
            f"| ¥{float(med):.0f} | {r7} "
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
            f"### {rank}. {_keyword_cell(gap)}",
            "",
            f"- **缺口分**：{gap['gap_score']}",
            f"- **需求帖 / 虚拟供给**：{gap['demand_posts']} / {gap['virtual_supply']}（弱信号 {gap.get('weak_virtual_count', 0)}，总挂牌 {gap['total_listings']}）",
            f"- **竞品均价**：¥{gap['avg_price']:.0f}（P25 ¥{gap.get('price_p25', 0):.0f} / 中位 ¥{gap.get('price_median', 0):.0f} / P75 ¥{gap.get('price_p75', 0):.0f}；平均 {gap['avg_want']:.0f} 人想要）",
            f"- **价格分布**：{gap.get('price_distribution', '—')}",
            f"- **建议定价**：{gap['suggested_price']}",
            "",
        ]

        # 分类质量
        dist = gap.get("classification_dist") or {}
        total_l = int(gap.get("total_listings") or 0)
        unk_n = int(dist.get("unknown", 0))
        unk_pct = _pct(float(unk_n), float(total_l)) if total_l else 0.0
        dist_parts = [f"{k}: {v}" for k, v in sorted(dist.items(), key=lambda x: -x[1])]
        lines += [
            f"- **分类分布**：{'; '.join(dist_parts) if dist_parts else '—'}",
            f"- **unknown 占比**：{unk_pct}%",
            "",
        ]

        # 市场活跃度
        n7 = int(gap.get("recent_7d_count") or 0)
        n30 = int(gap.get("recent_30d_count") or 0)
        newest = gap.get("newest_pub_date") or "—"
        oldest = gap.get("oldest_pub_date") or "—"
        lines += [
            f"- **市场活跃度**：近7天新品 {n7} 条，近30天新品 {n30} 条",
            f"- **发布时间范围**：最新 {newest}，最老 {oldest}",
            "",
        ]

        # 热门单品（想要数 > 0）
        tw = gap.get("top_want_items") or []
        tw_nonzero = [x for x in tw if int(x.get("want_num") or 0) > 0]
        if tw_nonzero:
            lines.append("- **热门单品（想要数 Top）**：")
            for i, it in enumerate(tw_nonzero[:3], 1):
                p = float(it.get('price', 0))
                lines.append(
                    f"  {i}. {it.get('title', '')[:60]} — ¥{p:.0f}，{it.get('want_num', 0)} 人想要"
                )
            lines.append("")
        else:
            lines += [
                "- **热门单品**：当前样本中 want_num 均为 0（可结合上方 debug 日志核对 API 字段）",
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

    # 数据质量摘要
    lines += _render_data_quality(sorted_gaps)

    eval_gaps = [g for g in sorted_gaps if g.get("keyword_status")]
    subdiv_gaps = [g for g in sorted_gaps if g.get("subdivision_suggestions")]

    if eval_gaps:
        lines += [
            "## 关键词质量评估",
            "",
            "| 关键词 | 状态 | 相关度 | 原因 | 替代建议 |",
            "|--------|------|--------|------|----------|",
        ]
        for g in eval_gaps:
            alts = g.get("suggested_alternatives") or []
            alts_str = ", ".join(alts) if alts else "—"
            st = str(g.get("keyword_status", ""))
            rs = g.get("relevance_score", 0.0)
            try:
                rs_f = float(rs)
            except (TypeError, ValueError):
                rs_f = 0.0
            reason = g.get("evaluation_reason") or "—"
            lines.append(
                f"| {g['keyword']} | {_status_label_zh(st)} | {rs_f:.2f} "
                f"| {reason} | {alts_str} |"
            )
        lines.append("")

    if subdiv_gaps:
        lines += [
            "## 关键词细分建议",
            "",
            "| 原关键词 | 建议细分 |",
            "|----------|----------|",
        ]
        for g in subdiv_gaps:
            subs = g.get("subdivision_suggestions") or []
            sub_str = ", ".join(subs) if subs else "—"
            lines.append(f"| {g['keyword']} | {sub_str} |")
        lines.append("")

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
