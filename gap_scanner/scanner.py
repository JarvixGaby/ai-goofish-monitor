"""
缺口计算模块。

核心公式：
    缺口分 = 需求帖数 / max(虚拟供给数, 1)

缺口分越高 → 需求旺盛但供给稀少 → 值得今天创作并上架。
"""

import json
from pathlib import Path

DATA_DIR = Path("data")

# 认定为「虚拟供给」的标题关键词（与 fetcher.py 同步，可按需扩充）
VIRTUAL_SUPPLY_KWS = frozenset(
    ["教程", "文档", "资料", "课程", "指导", "网盘", "模板", "方法论",
     "指南", "攻略", "代操作", "代写", "代发", "服务", "合集", "电子书"]
)


def _is_virtual_supply(title: str) -> bool:
    return any(kw in title for kw in VIRTUAL_SUPPLY_KWS)


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


def calculate_gap(keyword: str, supply_items: list[dict], demand_count: int) -> dict:
    """
    计算单个关键词的缺口指标。

    返回包含以下字段的字典：
    - keyword, gap_score, demand_posts, virtual_supply, total_listings
    - avg_price, avg_want, suggested_price, top_titles
    """
    virtual = [i for i in supply_items if _is_virtual_supply(i.get("title", ""))]
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

    # 代表性竞品标题（前 3 条虚拟商品）
    top_titles = [i["title"] for i in virtual[:3]]

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
    }
