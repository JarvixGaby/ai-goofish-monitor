"""
缺口计算模块（纯逻辑，无 AI 依赖）。

核心公式：
    缺口分 = 需求帖数 / max(虚拟供给数, 1)

缺口分越高 → 需求旺盛但供给稀少 → 值得今天创作并上架。

依赖：vocabulary.Vocabulary（注入，不在此处实例化）
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vocabulary import Vocabulary

DATA_DIR = Path("data")


def save_raw(keyword: str, items: list[dict], date_str: str) -> None:
    """将原始商品数据追加写入每日 JSONL 文件，一行一个关键词。"""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{date_str}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(
            {"keyword": keyword, "items": items, "date": date_str},
            ensure_ascii=False
        ) + "\n")


def load_raw(date_str: str) -> dict[str, list[dict]]:
    """读取某天的原始数据，返回 {keyword: [items]} 字典。"""
    path = DATA_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return {}
    result: dict[str, list[dict]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                result[entry["keyword"]] = entry.get("items", [])
            except json.JSONDecodeError:
                pass
    return result


def calculate_gap(
    keyword: str,
    supply_items: list[dict],
    demand_count: int,
    vocabulary: "Vocabulary | None" = None,
) -> dict:
    """
    计算单个关键词的缺口指标。

    参数：
        keyword:      搜索关键词
        supply_items: 从闲鱼搜索到的商品列表
                      每条 item 至少含 title、price、want_num 字段。
                      若 item 已有 "classification" 字段（由 ai_classifier 设置），
                      则直接使用；否则通过 vocabulary 或 item["is_virtual"] 判断。
        demand_count: 需求帖数量
        vocabulary:   Vocabulary 实例（可选）；若提供则用于分类；
                      若为 None，则回退到 item["is_virtual"] 字段（fetcher 已标注）

    返回包含以下字段的字典：
        keyword, gap_score, demand_posts, virtual_supply, total_listings,
        avg_price, avg_want, suggested_price, top_titles, top_items
    """
    virtual: list[dict] = []

    for item in supply_items:
        # 优先使用已有分类结果（来自 AI 分类器或 fetcher）
        classification = item.get("classification", "")

        if classification == "virtual":
            virtual.append(item)
        elif classification in ("demand", "blacklisted", "physical"):
            continue
        elif vocabulary is not None:
            # 使用词库匹配
            result = vocabulary.match(item.get("title", ""))
            if result.classification == "virtual":
                item["classification"] = "virtual"
                virtual.append(item)
            else:
                item["classification"] = result.classification or "unknown"
        else:
            # 最终回退：使用 fetcher 标注的 is_virtual 字段
            if item.get("is_virtual", False):
                item["classification"] = "virtual"
                virtual.append(item)
            else:
                item["classification"] = "unknown"

    virtual_count = len(virtual)
    total = len(supply_items)

    # 平均售价（只统计有效价格）
    prices = [i["price"] for i in virtual if i.get("price", 0) > 0]
    avg_price = round(sum(prices) / len(prices), 1) if prices else 0.0

    # 平均想要数
    wants = [i["want_num"] for i in virtual if i.get("want_num", 0) > 0]
    avg_want = round(sum(wants) / len(wants), 1) if wants else 0.0

    # 核心缺口分
    gap_score = round(demand_count / max(virtual_count, 1), 2)

    # 建议定价：比现有均价低 10-20%，向 5 取整
    if avg_price > 5:
        lo = max(1, round(avg_price * 0.80 / 5) * 5)
        hi = max(lo + 5, round(avg_price * 0.95 / 5) * 5)
        suggested_price = f"¥{lo}-{hi}"
    else:
        suggested_price = "¥9-19"

    # 代表性竞品（前 5 条虚拟商品，供 AI Advisor 使用）
    top_items = virtual[:5]
    top_titles = [i["title"] for i in top_items]

    return {
        "keyword": keyword,
        "gap_score": gap_score,
        "demand_posts": demand_count,
        "virtual_supply": virtual_count,
        "total_listings": total,
        "avg_price": avg_price,
        "avg_want": avg_want,
        "suggested_price": suggested_price,
        "top_titles": top_titles,
        "top_items": top_items,     # 新增：供 Phase 2 AI Advisor 使用
    }
