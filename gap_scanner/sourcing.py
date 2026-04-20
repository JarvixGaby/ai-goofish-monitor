"""
选品 Dossier 生成器 — 闲鱼选品情报系统的第一个真正 "可交付物"

与 gap 扫描不同：不输出评分，直接输出新手能照着做的行动方案。

输入：collect.py 采集到的真实搜索数据 + 详情页数据
处理：对每个关键词调用 sonnet 合成 dossier（markdown）
输出：dossiers/YYYY-MM-DD_<name>.md —— 一份完整的选品报告

用法：
    python sourcing.py                           # 用今天的数据
    python sourcing.py --date 2026-04-17         # 指定日期
    python sourcing.py --name 投资理财           # 给输出文件加个赛道名
    python sourcing.py --keywords 炒股教程 CFA备考资料  # 只分析这几个词
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from ai_client import AIClient, build_analysis_client
from config import get_settings

DATA_DIR = Path("data")
DOSSIER_DIR = Path("dossiers")


# ─────────────────────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────────────────────

def load_search_data(date_str: str) -> dict[str, list[dict]]:
    """从 data/raw/YYYY-MM-DD/search.jsonl 加载，按关键词聚合"""
    path = DATA_DIR / "raw" / date_str / "search.jsonl"
    if not path.exists():
        # 兼容旧版平铺路径
        path = DATA_DIR / f"{date_str}.jsonl"
    if not path.exists():
        print(f"[sourcing] 没找到采集数据：{path}")
        return {}

    data: dict[str, list[dict]] = defaultdict(list)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                data[entry["keyword"]].extend(entry.get("items", []))
            except json.JSONDecodeError:
                continue
    return dict(data)


def load_enriched_data(date_str: str) -> dict[str, dict]:
    """详情页增强数据，item_id -> enriched 字典"""
    dir_path = DATA_DIR / "enriched" / date_str
    if not dir_path.exists():
        return {}
    result: dict[str, dict] = {}
    for p in dir_path.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            iid = d.get("item_id", p.stem)
            result[iid] = d
        except Exception:
            continue
    return result


# ─────────────────────────────────────────────────────────────
# 构建 AI 输入 —— 关键：把原始数据压成有信息密度的文本
# ─────────────────────────────────────────────────────────────

def _format_item_row(item: dict, enriched: dict | None) -> str:
    """单条商品的紧凑表示。

    注意（2026-04-20 更新）：
    闲鱼搜索 API 的 title 字段实际上包含"标题 + 描述前 100-250 字"，
    所以即使没有详情页，AI 也能从 title 里读出货源类型、卖点叙事和合规黑话。
    want_count/browse_count 等字段搜索 API 不返回，详情页又被风控，
    本函数在 enriched=None 时会尽量用搜索卡片里的其他信号补足。
    """
    # title 可能很长（最长见过 4700+ 字），截到 500 字足够 AI 理解卖点
    title = (item.get("title") or "").strip().replace("\n", " ")
    if len(title) > 500:
        title = title[:500] + "…"
    price = item.get("price", 0)
    cls = item.get("classification", "?")

    parts = [f"[¥{price}] {title}"]
    meta = []

    # 原价：体现折扣力度（31% 商品有原价）
    ori_price = item.get("ori_price") or ""
    if ori_price and str(ori_price).strip():
        meta.append(f"原价{ori_price}")

    # 发布时间：判断上架新鲜度
    pub_ts = item.get("pub_ts", 0)
    if pub_ts:
        try:
            import time
            days_ago = int((time.time() * 1000 - int(pub_ts)) / (86400 * 1000))
            if days_ago >= 0:
                meta.append(f"{days_ago}天前发")
        except Exception:
            pass

    if cls and cls not in ("unknown", "?"):
        meta.append(cls)

    seller = item.get("seller_name") or ""
    if seller:
        meta.append(f"卖家:{seller}")

    area = item.get("area") or ""
    if area:
        meta.append(area)

    # 闲鱼标签（包邮/转卖）
    tags = item.get("fish_tags") or []
    # 过滤掉 icon 类内部字段
    display_tags = [t for t in tags if t and not t.endswith("Icon")]
    if display_tags:
        meta.append(",".join(display_tags))

    if enriched:
        # 详情页有的话继续用（目前被风控，实际拿不到）
        browse = enriched.get("browse_count")
        if browse:
            meta.append(f"浏览{browse}")
        ew = enriched.get("want_count")
        if ew:
            meta.append(f"想要{ew}")
        regdays = enriched.get("seller_reg_days")
        if regdays:
            meta.append(f"卖家注册{regdays}天")
        desc = (enriched.get("description") or "").strip()
        if desc:
            desc_short = desc.replace("\n", " ")[:200]
            parts.append(f"    描述: {desc_short}")

    if meta:
        parts[0] += "  (" + ", ".join(meta) + ")"
    return "\n".join(parts)


def build_prompt_for_keyword(
    keyword: str,
    items: list[dict],
    enriched_map: dict[str, dict],
) -> str:
    """给单个关键词组装 AI 输入"""
    # 详情页数据更宝贵，先展示带详情的
    with_detail: list[str] = []
    without_detail: list[str] = []
    for item in items:
        iid = item.get("item_id", "")
        enriched = enriched_map.get(iid) if iid else None
        row = _format_item_row(item, enriched)
        if enriched:
            with_detail.append(row)
        else:
            without_detail.append(row)

    # 价格统计给 AI 参考
    prices = [i.get("price", 0) for i in items if i.get("price", 0) > 0]
    virtual_ratio = sum(1 for i in items if i.get("is_virtual")) / max(len(items), 1)

    lines = [
        f"关键词：{keyword}",
        f"搜索结果总数：{len(items)} 条",
        f"价格范围：¥{min(prices):.0f} ~ ¥{max(prices):.0f}" if prices else "",
        f"价格中位数：¥{sorted(prices)[len(prices)//2]:.0f}" if prices else "",
        f"虚拟商品占比：{virtual_ratio*100:.0f}%",
        "",
        f"=== 有详情页数据的商品（{len(with_detail)} 条）===",
        *with_detail,
        "",
        f"=== 仅搜索卡片的商品（{len(without_detail)} 条，为节省篇幅只展示前 30）===",
        *without_detail[:30],
    ]
    return "\n".join(l for l in lines if l is not None)


# ─────────────────────────────────────────────────────────────
# Prompt —— 核心产出，决定 dossier 的质量
# ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是闲鱼平台的选品分析师，专门服务"零经验、零资金、想开始卖虚拟商品"的新手。

你的任务：基于我提供的真实搜索数据，输出一份让新手能直接照着做的选品报告。

**数据说明（很重要）**：
- 我提供的数据来自闲鱼搜索 API。`title` 字段实际上包含"商品标题 + 描述前 100-250 字"，
  所以你能从 title 里读到卖点叙事、货源暗语（如"kc资料""非纸""百度网盘秒发"）、包邮承诺、禁忌词等。
- 搜索 API **不返回"想要数/浏览数/成交数"**，所以你**不要编造**这些数字，也不要说"根据想要数推断"。
  判断热度只能用：发布时间分布（多少天前发的）、卖家重复度（同一赛道多少家在卖）、价格分层密度、原价折扣结构。
- 如果看到 `{X}天前发`，表示商品多久前上架。大量"7 天内新品"说明这个赛道在快速进场。
- 如果看到"原价¥10 现价¥2"，说明是折扣叙事——对"捡漏心理"的买家有效。

**关键原则**：
1. **用数据说话**，不许讲"潜力大""市场广阔"这种空话。每个结论必须指向具体的商品编号、价格、标题片段。
2. **不要编造没有的指标**（销量、想要数、月入数字都不要凭空给）。只能说"从 XX 条搜索结果看，有 XX 家在卖 XX 价位"这种可验证的话。
3. **如实报告货源模式**，包括"百度云搬运""某某课程复制""代找资料"这类灰色模式——新手需要知道真相才能决策。不做道德判断。
4. **面向抄作业式的执行者**。不要讲抽象策略，要讲"你打开闲鱼，在标题里写什么字"。
5. **冷峻、不奉承**。如果这个赛道不适合新手，直接说"劝退"。

**输出格式**（严格使用 markdown，不要任何额外包装）：

## 一、市场画像（一眼看懂这个赛道）
- **供给规模**：搜索能看到多少家在卖，主力卖家特征（个人/工作室/书店，从卖家昵称推断）
- **价格结构**：引流款 ¥X-X、主力款 ¥X-X、高价款 ¥X-X，分别对应什么类型商品
- **上架活跃度**：多少比例是最近 30 天内发布的（从"X 天前发"汇总）——高比例 = 赛道在快速进场 = 更卷
- **买家画像**：根据标题里的话术（入门/速成/保过/真题）推断谁在买
- **数据锚点**：引用至少 3 个真实标题 + 价格作为证据

## 二、爆款标题模板（Top 3 可抄作业的命名公式）
逐个列出：
- **模板 X**：`[公式]`（如 `{主题}保姆级教程+{附加价值} 永久更新`）
- **真实案例**：引用 2-3 个真实标题
- **价格区间**：¥X-X
- **起效机制**：为什么这种标题好卖（情绪钩子？信息量？）

## 三、货源线索（真相）
列出这个赛道的货源获取路径，每条附真实证据：
- **搬运型**（百度云/夸克/链接分享）：从哪来的原材料？价格规律？
- **自制型**（原创整理）：识别特征（通常标题强调"自制""整理""独家"）
- **代理型**（代拿/代找）：识别特征
- **如有**：平台风险提示（会不会被举报下架）

## 四、商业评估（新手视角）
- **单笔利润预估**：售价 ¥X - 获客/沟通时间 X 分钟 - 售后概率 X%（如果没有销量数据，只给"单笔毛利"推算，**不要编造"月入 XXX""日均 X 单"**）
- **竞争度**：红海 / 温海 / 蓝海（判断依据必须来自你能看到的：供给家数、价格分层密度、上架新鲜度、标题雷同度）
- **新号能做吗**：0 信用的新号，对比头部卖家有什么劣势？可以怎么绕过？
- **合规风险**：版权/广告/平台规则方面可能踩的雷（从标题里的暗语能推断——"kc资料""非纸""无加密"都是规避审核的信号）

## 五、7 天行动计划（照做就行）
每天一条，具体到操作：
- **Day 1**：具体做什么（含文案示例）
- **Day 2**：...
- ...
- **Day 7**：首单复盘

## 六、go / no-go 结论
- **推荐度**：⭐ / ⭐⭐ / ⭐⭐⭐ / ⭐⭐⭐⭐ / ⭐⭐⭐⭐⭐（五星=强烈推荐）
- **一句话判断**：新手该不该进？
- **如果 go**：最小切入方案——具体三步操作（不要承诺"7 天内出单概率 X%"这种编造的数字，改说"如果到第 X 天还 0 咨询就换赛道"）
- **如果 no-go**：核心阻碍是什么，需要什么条件才能进

只输出 markdown，不要解释你在做什么。"""


async def generate_keyword_dossier(
    client: AIClient,
    keyword: str,
    items: list[dict],
    enriched_map: dict[str, dict],
) -> str:
    user_content = build_prompt_for_keyword(keyword, items, enriched_map)
    # 单个关键词一次性调用，直接返回 markdown
    return await client.chat_text(system=_SYSTEM_PROMPT, user=user_content)


# ─────────────────────────────────────────────────────────────
# 赛道总览（把所有关键词的 dossier 合并并加入综合判断）
# ─────────────────────────────────────────────────────────────

_OVERVIEW_SYSTEM = """你是选品分析师，现在要为一个赛道写"综合选品策略"。

我会给你这个赛道下每个关键词的独立 dossier，请你：
1. 归纳这个赛道的整体规律（哪类关键词好切入，哪类是陷阱）
2. 给新手一个明确的"从哪开始做"的排序（第一个做什么，第二个做什么）
3. 揭示这个赛道的共性风险

输出 markdown，不要重复个关键词 dossier 的内容，只做综合判断：

## 赛道综合判断

### 一、整体画像（一句话总结）

### 二、关键词切入排序（从新手最适合到最不适合）
列表形式，每个关键词一行简要判断 + 理由

### 三、赛道共性机会
（这个赛道的通用打法）

### 四、赛道共性风险
（这个赛道的坑）

### 五、新手第一周最该做的事
具体、可执行、不超过 3 条

只输出 markdown。"""


async def generate_overview(
    client: AIClient, dossiers_by_kw: dict[str, str],
) -> str:
    parts = []
    for kw, d in dossiers_by_kw.items():
        parts.append(f"### 关键词：{kw}\n\n{d}\n\n---\n")
    user_content = "\n".join(parts)
    return await client.chat_text(system=_OVERVIEW_SYSTEM, user=user_content)


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    date_str = args.date or date.today().isoformat()
    print(f"[sourcing] 日期：{date_str}")

    search_data = load_search_data(date_str)
    if not search_data:
        print("[sourcing] 没数据，请先跑 collect.py")
        sys.exit(1)

    # 过滤关键词优先级：--keywords-file > --keywords > 全部
    if args.keywords_file:
        kw_path = Path("keywords") / args.keywords_file
        if not kw_path.exists() and not args.keywords_file.endswith(".txt"):
            kw_path = Path("keywords") / f"{args.keywords_file}.txt"
        if not kw_path.exists():
            print(f"[sourcing] 找不到 {kw_path}")
            sys.exit(1)
        target_kws = set()
        for line in kw_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                target_kws.add(line)
        print(f"[sourcing] 从 {kw_path.name} 加载 {len(target_kws)} 个目标关键词")
        search_data = {k: v for k, v in search_data.items() if k in target_kws}
        missing = target_kws - set(search_data.keys())
        if missing:
            print(f"[sourcing] 以下关键词未采集，已跳过：{missing}")
    elif args.keywords:
        target_kws = set(args.keywords)
        search_data = {k: v for k, v in search_data.items() if k in target_kws}
        missing = target_kws - set(search_data.keys())
        if missing:
            print(f"[sourcing] 以下关键词未采集，已跳过：{missing}")

    if not search_data:
        print("[sourcing] 过滤后无数据")
        sys.exit(1)

    enriched_map = load_enriched_data(date_str)
    print(f"[sourcing] 搜索数据 {sum(len(v) for v in search_data.values())} 条，详情页增强 {len(enriched_map)} 条")
    print(f"[sourcing] 将为 {len(search_data)} 个关键词生成 dossier：{', '.join(search_data.keys())}")

    # 构建分析 client（sonnet）—— dossier 输出长，把超时拉到 180s
    from dataclasses import replace
    base_settings = get_settings()
    settings = replace(base_settings, ai_timeout=180)
    client = build_analysis_client(settings)
    print(f"[sourcing] 使用模型：{settings.ai_analysis_model}  timeout={settings.ai_timeout}s")

    # 并发生成每个关键词的 dossier
    # 2026-04-20：用 Semaphore 控制并发（=3），避免 Anthropic 429 并发限制
    # 12 个并发会直接超限，实测 3 个并发稳定
    concurrency = max(1, args.concurrency)
    print(f"[sourcing] 并发上限：{concurrency}")
    sem = asyncio.Semaphore(concurrency)
    dossiers: dict[str, str] = {}

    async def _gen(kw: str, items: list[dict]) -> tuple[str, str]:
        async with sem:
            print(f"  [AI] 生成 dossier: {kw}（{len(items)} 条数据）")
            md = await generate_keyword_dossier(client, kw, items, enriched_map)
            return kw, md

    tasks = [_gen(kw, items) for kw, items in search_data.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failed_kws: list[str] = []
    for (kw, _items), r in zip(search_data.items(), results):
        if isinstance(r, Exception):
            print(f"  [ERROR] {kw} 生成失败：{r}")
            failed_kws.append(kw)
            continue
        _kw, md = r
        dossiers[_kw] = md

    if failed_kws:
        print(f"[sourcing] 失败的关键词：{failed_kws}（可重跑 --keywords {' '.join(failed_kws)} 补齐）")

    if not dossiers:
        print("[sourcing] 所有关键词生成失败")
        sys.exit(1)

    # 赛道综合判断
    print(f"[sourcing] 生成赛道综合判断...")
    try:
        overview = await generate_overview(client, dossiers)
    except Exception as e:
        print(f"[sourcing] 综合判断失败：{e}")
        overview = f"_综合判断生成失败：{e}_"

    # 拼接输出
    DOSSIER_DIR.mkdir(exist_ok=True)
    name_suffix = f"_{args.name}" if args.name else ""
    out_path = DOSSIER_DIR / f"{date_str}{name_suffix}.md"

    header = [
        f"# 选品 Dossier — {args.name or '默认赛道'}",
        f"",
        f"- 生成日期：{date_str}",
        f"- 关键词数：{len(dossiers)}",
        f"- 数据来源：搜索卡片 {sum(len(v) for v in search_data.values())} 条 + 详情页 {len(enriched_map)} 条",
        f"- 分析模型：{settings.ai_analysis_model}",
        f"",
        f"---",
        f"",
        overview,
        f"",
        f"---",
        f"",
        f"# 逐关键词详细 Dossier",
        f"",
    ]

    body = []
    for kw, md in dossiers.items():
        body.append(f"## 关键词：{kw}")
        body.append("")
        body.append(md)
        body.append("")
        body.append("---")
        body.append("")

    out_path.write_text("\n".join(header + body), encoding="utf-8")
    print(f"\n[sourcing] ✓ 已生成：{out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="闲鱼选品 Dossier 生成器")
    parser.add_argument("--date", type=str, default=None, help="日期（YYYY-MM-DD），默认今天")
    parser.add_argument("--name", type=str, default=None, help="赛道名（写入文件名和标题）")
    parser.add_argument("--keywords", nargs="+", default=None, help="只分析指定关键词")
    parser.add_argument("--keywords-file", type=str, default=None,
                        help="从 keywords/xxx.txt 加载目标关键词（优先级高于 --keywords）")
    parser.add_argument("--concurrency", type=int, default=3,
                        help="AI 并发数，默认 3（太高会触发 Anthropic 429 限流）")
    args = parser.parse_args()
    asyncio.run(main(args))
