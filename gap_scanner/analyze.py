"""
闲鱼虚拟商品缺口扫描器 v2 — Phase 2: 异步分析

读取 collect.py 采集的数据，离线完成全部分析：
    1. 缺口分计算（含详情页增强数据）
    2. 高销量/高浏览商品深度分析（sonnet）
    3. n-gram 关键词发现
    4. 关键词生命周期管理
    5. 生成报告

用法：
    python analyze.py                       # 分析今日数据
    python analyze.py --date 2026-04-17     # 分析指定日期
    python analyze.py --no-ai               # 跳过 AI 分析步骤
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from scanner import calculate_gap, load_raw
from reporter import generate, save
from vocabulary import Vocabulary
from config import get_settings
from ai_client import build_client_optional, build_analysis_client, AIClient
from keyword_analyzer import KeywordEvaluation, load_keyword_status

VOCAB_DIR = Path("vocab")
DATA_DIR = Path("data")
KEYWORDS_DIR = Path("keywords")


# ──────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────

def load_raw_data(date_str: str) -> dict[str, list[dict]]:
    """加载某天的原始搜索数据。优先新格式，回退旧格式。"""
    new_path = DATA_DIR / "raw" / date_str / "search.jsonl"
    if new_path.exists():
        result: dict[str, list[dict]] = {}
        with open(new_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    result[entry["keyword"]] = entry.get("items", [])
                except (json.JSONDecodeError, KeyError):
                    pass
        return result

    return load_raw(date_str)


def load_enriched_data(date_str: str) -> dict[str, dict]:
    """加载某天的详情页增强数据。返回 {item_id: enriched_dict}。"""
    enriched_dir = DATA_DIR / "enriched" / date_str
    if not enriched_dir.exists():
        return {}

    result: dict[str, dict] = {}
    for path in enriched_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            item_id = data.get("item_id", path.stem)
            result[item_id] = data
        except (json.JSONDecodeError, OSError):
            pass
    return result


def load_collect_summary(date_str: str) -> dict | None:
    path = DATA_DIR / "raw" / date_str / "collect_summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ──────────────────────────────────────────────────────────
# 关键词发现（n-gram）
# ──────────────────────────────────────────────────────────

_STOPWORDS = {"的", "了", "在", "是", "和", "有", "个", "这", "那", "也",
              "就", "不", "都", "看", "我", "你", "一", "上", "中", "到"}

def _tokenize_title(title: str) -> list[str]:
    """简易中文分词：按标点/空格切段，每段做 2-4 gram。"""
    segments = re.split(r'[\s\|｜\-—·•\[\]【】（）()「」《》,，。！!？?;；:：/\\+&]', title)
    tokens: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if len(seg) < 2:
            continue
        # 英文单词保持完整
        if re.match(r'^[A-Za-z0-9]', seg):
            tokens.append(seg.lower())
            continue
        # 中文 n-gram
        for n in (4, 3, 2):
            for i in range(len(seg) - n + 1):
                gram = seg[i:i+n]
                if any(c in _STOPWORDS for c in gram):
                    continue
                tokens.append(gram)
    return tokens


def discover_keywords(
    raw_data: dict[str, list[dict]],
    existing_keywords: set[str],
    top_n: int = 30,
) -> list[tuple[str, int]]:
    """
    从所有标题中挖掘高频词组，发现潜在的新关键词。

    返回 [(candidate_keyword, frequency)] 列表。
    """
    counter: Counter = Counter()
    for kw, items in raw_data.items():
        for item in items:
            title = item.get("title", "")
            if not title:
                continue
            tokens = _tokenize_title(title)
            counter.update(tokens)

    results: list[tuple[str, int]] = []
    noise_pattern = re.compile(r'^[\d.…·\-_=+*#@!~]+$')
    # 交付话术的 n-gram 碎片也要过滤
    delivery_fragments = {"动发", "发货", "接拍", "自发", "秒发", "下拍",
                          "拍即", "标价", "零基", "子版", "电子",
                          "随时", "hot", "实战"}
    for token, freq in counter.most_common(top_n * 5):
        if freq < 5:
            break
        if len(token) < 2:
            continue
        if token in existing_keywords:
            continue
        if noise_pattern.match(token):
            continue
        if token in delivery_fragments:
            continue
        generic_words = {
            # 通用虚词
            "包含", "适合", "直接", "内容", "可以", "非常", "什么",
            "以及", "完整", "所有", "需要", "支持", "提供", "学习", "使用",
            "已经", "全部", "一个", "没有", "就是", "如果", "或者", "自动",
            "价格", "更新", "年新", "一次", "还有", "但是", "绝对", "超级",
            "超值", "可以", "如何", "怎么", "这个", "那个", "比较", "特别",
            "真的", "最新", "欢迎", "感谢",
            # 闲鱼交付话术（n-gram 高频但无选品价值）
            "发货", "拍下", "秒发", "自动发", "动发货", "自动发货",
            "直接拍", "接拍", "标价", "即卖", "卖价", "标价即",
            "网盘", "夸克", "百度", "阿里", "链接",
            "包邮", "下单", "私聊", "点我", "想要", "我想要",
            "感兴趣", "咨询客服", "客服",
            # 平台/格式类
            "电子版", "电子", "虚拟", "虚拟商品",
            # 模糊程度词
            "零基础", "基础", "入门", "全套", "精通", "从零", "新手",
            "系统", "完全", "高级", "精品", "专业",
            # 二字高频但无意义
            "视频", "商品", "资源", "工具", "设计", "运营",
        }
        if token in generic_words:
            continue
        results.append((token, freq))
        if len(results) >= top_n:
            break

    return results


# ──────────────────────────────────────────────────────────
# 高价值商品分析（sonnet）
# ──────────────────────────────────────────────────────────

_INSIGHT_SYSTEM_PROMPT = """\
你是一位闲鱼虚拟商品市场分析专家。

任务：分析一组「高浏览量/高想要数」的虚拟商品详情，提炼出可复制的成功模式。

请从以下维度分析：
1. **标题结构**：成功标题的共性模式（如关键词位置、数量化承诺、信任信号）
2. **定价策略**：有效的价格区间，高价 vs 低价的成交差异
3. **内容差异化**：成功卖家怎么做差异化（更新、服务、打包、话术）
4. **空白机会**：从数据中发现的、竞争尚少但有需求的细分方向
5. **关键词建议**：基于分析，推荐 3-5 个值得新开发的关键词

以 JSON 返回：
{
  "title_patterns": ["模式1", "模式2"],
  "pricing_insight": "定价策略分析",
  "differentiation": "差异化方法",
  "opportunities": ["空白机会1", "空白机会2"],
  "suggested_keywords": ["关键词1", "关键词2", "关键词3"]
}
"""

_KW_DERIVE_SYSTEM_PROMPT = """\
你是闲鱼虚拟商品搜索词优化专家。

任务：根据以下宽泛关键词的搜索结果（标题 + 描述摘要），衍生出更精准的细分关键词，
每个细分词应能在闲鱼上搜到具体的虚拟商品（教程/资料/模板/课程/素材/源码/工具等数字内容）。

要求：
1. 每个细分词 2-6 个中文字（或英文单词），适合直接粘贴到闲鱼搜索框
2. 细分词必须比原词更具体，指向一个可操作的商品品类
3. 排除纯线下服务、实物商品、平台账号转让等非虚拟商品方向
4. 优先输出你认为「竞争少但需求存在」的方向
5. 不要输出已有的关键词（会在用户消息里列出）

以 JSON 返回：
{
  "derived_keywords": [
    {"keyword": "细分词", "reason": "一句话解释为什么这个词有价值", "source_keyword": "来源宽泛词"},
    ...
  ]
}

只输出 JSON。"""


async def derive_keywords_from_data(
    raw_data: dict[str, list[dict]],
    enriched_data: dict[str, dict],
    existing_keywords: set[str],
    client: AIClient,
) -> list[dict]:
    """
    AI 驱动的关键词衍生：从搜索结果标题和详情页描述中发现新的细分关键词。

    与 n-gram 不同，这里让 AI 理解内容语义后推荐，质量远高于统计法。
    """
    # 为每个宽泛词组织数据摘要（标题 + 描述片段）
    keyword_summaries: list[str] = []
    for kw, items in raw_data.items():
        if not items:
            continue
        lines = [f"\n## 关键词：{kw}（{len(items)} 条结果）"]
        for i, item in enumerate(items[:12], 1):
            title = item.get("title", "")[:80]
            price = item.get("price", 0)
            cls = item.get("classification", "?")
            desc = ""
            iid = item.get("item_id", "")
            if iid in enriched_data:
                desc = enriched_data[iid].get("description", "")[:100]
            line = f"  {i}. [{cls}] ¥{price:.0f} {title}"
            if desc:
                line += f"\n     描述：{desc}"
            lines.append(line)
        keyword_summaries.append("\n".join(lines))

    if not keyword_summaries:
        return []

    existing_list = "、".join(sorted(existing_keywords)[:50])
    user_msg = (
        f"已有关键词（不要重复）：{existing_list}\n\n"
        f"以下是各宽泛词的搜索结果摘要：\n"
        + "\n".join(keyword_summaries)
    )

    try:
        result = await client.chat(_KW_DERIVE_SYSTEM_PROMPT, user_msg)
        derived = result.get("derived_keywords", [])
        if not isinstance(derived, list):
            return []
        valid: list[dict] = []
        for item in derived:
            kw = str(item.get("keyword", "")).strip()
            if not kw or kw in existing_keywords or len(kw) < 2:
                continue
            valid.append({
                "keyword": kw,
                "reason": str(item.get("reason", "")),
                "source": str(item.get("source_keyword", "")),
            })
        return valid
    except Exception as e:
        print(f"  [AI] 关键词衍生失败：{e}")
        return []


async def analyze_high_value_items(
    enriched_data: dict[str, dict],
    client: AIClient,
) -> dict | None:
    """对高浏览/高想要的商品做深度分析（使用 sonnet）。"""
    # 筛选高价值商品
    high_value = sorted(
        enriched_data.values(),
        key=lambda x: (x.get("want_count", 0) + x.get("browse_count", 0) / 100),
        reverse=True,
    )[:15]

    if not high_value:
        return None

    items_text = []
    for i, item in enumerate(high_value, 1):
        items_text.append(
            f"{i}. 标题: {item.get('title', '')}\n"
            f"   价格: ¥{item.get('price', 0)}\n"
            f"   想要: {item.get('want_count', 0)}  浏览: {item.get('browse_count', 0)}\n"
            f"   描述: {item.get('description', '')[:200]}\n"
            f"   分类: {item.get('classification', '')}  关键词: {item.get('keyword', '')}"
        )

    user_msg = (
        "以下是闲鱼上高浏览量/高想要数的虚拟商品详情：\n\n"
        + "\n\n".join(items_text)
    )

    try:
        result = await client.chat(_INSIGHT_SYSTEM_PROMPT, user_msg)
        return result
    except Exception as e:
        print(f"  [分析] 高价值商品分析失败：{e}")
        return None


# ──────────────────────────────────────────────────────────
# 关键词生命周期管理
# ──────────────────────────────────────────────────────────

def _load_kw_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip().split("#")[0].strip() for l in lines
            if l.strip() and not l.startswith("#")]


def _append_kw_file(path: Path, keywords: list[str], comment: str = "") -> int:
    """追加关键词到文件，返回实际新增数量。"""
    existing = set(_load_kw_file(path))
    new_kws = [kw for kw in keywords if kw not in existing]
    if not new_kws:
        return 0

    with open(path, "a", encoding="utf-8") as f:
        if comment:
            f.write(f"\n# {comment}\n")
        for kw in new_kws:
            f.write(kw + "\n")
    return len(new_kws)


def _remove_from_kw_file(path: Path, keywords_to_remove: set[str]) -> int:
    """从文件中移除指定关键词（保留注释和格式）。"""
    if not path.exists() or not keywords_to_remove:
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            kw = stripped.split("#")[0].strip()
            if kw in keywords_to_remove:
                removed += 1
                continue
        new_lines.append(line)
    if removed > 0:
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return removed


def update_keyword_lifecycle(
    keyword_stats: list[dict],
    ngram_candidates: list[tuple[str, int]],
    ai_suggested_keywords: list[str] | None,
    ai_derived_keywords: list[dict] | None,
    keyword_status_store: dict,
    today: str,
) -> dict:
    """
    更新关键词生命周期：
    - invalid 关键词 → retired.txt（从 active + broad 移除）
    - AI 衍生词 / AI insight 建议 / n-gram 高频词 / invalid 替代词 → candidates.txt
    """
    active_path = KEYWORDS_DIR / "active.txt"
    broad_path = KEYWORDS_DIR / "broad.txt"
    candidates_path = KEYWORDS_DIR / "candidates.txt"
    retired_path = KEYWORDS_DIR / "retired.txt"

    retired_set = set(_load_kw_file(retired_path))
    result = {"retired": [], "new_candidates": [], "ai_derived": []}

    # 1. 淘汰无效关键词（从 active 和 broad 同时移除）
    invalid_kws = {s["keyword"] for s in keyword_stats if s.get("keyword_status") == "invalid"}
    new_invalid = invalid_kws - retired_set
    if new_invalid:
        removed_active = _remove_from_kw_file(active_path, new_invalid)
        removed_broad = _remove_from_kw_file(broad_path, new_invalid)
        for kw in new_invalid:
            with open(retired_path, "a", encoding="utf-8") as f:
                f.write(f"{kw}  # 无效关键词  {today}\n")
        result["retired"] = list(new_invalid)
        print(f"  [生命周期] 淘汰 {removed_active + removed_broad} 个无效关键词：{', '.join(new_invalid)}")

    # 2. 收集所有已有关键词（排重用）
    existing_all = set(
        _load_kw_file(active_path) + _load_kw_file(broad_path)
        + _load_kw_file(candidates_path) + list(retired_set | new_invalid)
    )

    all_new: list[str] = []

    # 3. AI 衍生的细分关键词（最高优先级，来自 derive_keywords_from_data）
    if ai_derived_keywords:
        for item in ai_derived_keywords:
            kw = item["keyword"]
            if kw not in existing_all:
                all_new.append(kw)
                result["ai_derived"].append(item)
        if result["ai_derived"]:
            print(f"  [生命周期] AI 衍生 {len(result['ai_derived'])} 个细分关键词")

    # 4. noisy 关键词的 AI 替代建议（invalid 且 relevance=0 的不纳入，说明整个领域不靠谱）
    for kw_info in keyword_status_store.values():
        if not isinstance(kw_info, dict):
            continue
        status = kw_info.get("status", "")
        score = float(kw_info.get("relevance_score", 0))
        if status not in ("invalid", "noisy"):
            continue
        # relevance_score > 0.1 说明至少有部分结果沾边，替代词可能有用
        if score <= 0.1:
            continue
        for alt in kw_info.get("alternatives", []):
            alt = alt.strip()
            if alt and alt not in existing_all:
                all_new.append(alt)

    # 5. AI insight 建议的关键词
    if ai_suggested_keywords:
        for kw in ai_suggested_keywords:
            if kw not in existing_all:
                all_new.append(kw)

    # 6. n-gram 候选词（频率 >= 15 且长度 >= 3）
    ngram_new = [kw for kw, freq in ngram_candidates
                 if freq >= 15 and len(kw) >= 3 and kw not in existing_all]
    all_new.extend(ngram_new)

    # 去重保序
    seen: set[str] = set()
    unique_new: list[str] = []
    for kw in all_new:
        if kw not in seen and kw not in existing_all:
            seen.add(kw)
            unique_new.append(kw)

    if unique_new:
        added = _append_kw_file(candidates_path, unique_new, f"自动发现 {today}")
        if added > 0:
            result["new_candidates"] = unique_new[:added]
            print(f"  [生命周期] 新增 {added} 个候选关键词到 candidates.txt")

    return result


# ──────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────

async def run_analyze(date_str: str, no_ai: bool = False) -> None:
    print(f"[analyze] 分析日期：{date_str}")

    # 1. 加载数据
    raw_data = load_raw_data(date_str)
    if not raw_data:
        print(f"[ERROR] 未找到 {date_str} 的采集数据。先运行 collect.py。")
        sys.exit(1)

    enriched_data = load_enriched_data(date_str)
    collect_summary = load_collect_summary(date_str)

    print(f"  搜索数据：{len(raw_data)} 个关键词")
    print(f"  详情页数据：{len(enriched_data)} 条")

    # 2. 初始化
    vocabulary = Vocabulary(VOCAB_DIR)
    settings = None
    light_client: AIClient | None = None
    analysis_client: AIClient | None = None

    if not no_ai:
        try:
            settings = get_settings()
            light_client = build_client_optional(settings)
            analysis_client = build_analysis_client(settings)
            if light_client:
                print(f"  [AI] 轻量模型：{settings.ai_model}")
                print(f"  [AI] 分析模型：{settings.ai_analysis_model}")
        except ValueError as e:
            print(f"  [AI] 配置不完整：{e}")

    # 3. 缺口分计算
    print("\n[分析] 计算缺口分...")
    gaps: list[dict] = []
    all_items_flat: list[dict] = []

    keyword_stats = []
    if collect_summary:
        keyword_stats = collect_summary.get("keyword_stats", [])

    kw_status_map = {s["keyword"]: s.get("keyword_status", "valid") for s in keyword_stats}
    kw_demand_map = {s["keyword"]: s.get("demand_count", 0) for s in keyword_stats}

    for kw, items in raw_data.items():
        # 用详情页数据增强卡片数据
        for item in items:
            iid = item.get("item_id", "")
            if iid in enriched_data:
                en = enriched_data[iid]
                item["want_count_detail"] = en.get("want_count", 0)
                item["browse_count"] = en.get("browse_count", 0)
                item["description"] = en.get("description", "")

        demand = kw_demand_map.get(kw, 0)
        gap = calculate_gap(kw, items, demand, vocabulary)

        status = kw_status_map.get(kw, "valid")
        gap["keyword_status"] = status
        if status == "invalid":
            gap["gap_score"] = 0.0

        gaps.append(gap)
        all_items_flat.extend(items)

    # 按缺口分排序
    gaps.sort(key=lambda x: x["gap_score"], reverse=True)
    print(f"  计算完成：{len(gaps)} 个关键词")

    # 4. 高价值商品深度分析（sonnet）
    market_insight = None
    if analysis_client and enriched_data:
        print("\n[分析] 高价值商品深度分析（sonnet）...")
        market_insight = await analyze_high_value_items(enriched_data, analysis_client)
        if market_insight:
            print(f"  分析完成：发现 {len(market_insight.get('opportunities', []))} 个机会")

    # 5. AI 选品建议（sonnet，只对有效的 Top 5）
    advice_map: dict = {}
    if analysis_client:
        from ai_advisor import generate_advice_for_top
        valid_gaps = [g for g in gaps if g.get("keyword_status") != "invalid"]
        top_gaps = valid_gaps[:5]
        if top_gaps:
            print(f"\n[分析] 为 Top {len(top_gaps)} 生成选品建议（sonnet）...")
            advice_map = await generate_advice_for_top(top_gaps, analysis_client)

    # 6. 关键词发现
    print("\n[分析] 挖掘候选关键词...")
    existing_kws = set(kw for kw in raw_data.keys())
    # 把 active + broad + retired 都算进已有词表，避免重复推荐
    all_known_kws = existing_kws | set(
        _load_kw_file(KEYWORDS_DIR / "active.txt")
        + _load_kw_file(KEYWORDS_DIR / "broad.txt")
        + _load_kw_file(KEYWORDS_DIR / "retired.txt")
        + _load_kw_file(KEYWORDS_DIR / "candidates.txt")
    )

    # 6a. n-gram 统计候选
    ngram_candidates = discover_keywords(raw_data, all_known_kws, top_n=20)
    if ngram_candidates:
        print(f"  n-gram 候选：{', '.join(f'{kw}({freq})' for kw, freq in ngram_candidates[:10])}")

    # 6b. AI 衍生关键词（基于搜索结果 + 详情页内容语义分析）
    ai_derived: list[dict] = []
    if analysis_client:
        print("  [AI] 从搜索结果和详情页内容衍生细分关键词（sonnet）...")
        ai_derived = await derive_keywords_from_data(
            raw_data, enriched_data, all_known_kws, analysis_client
        )
        if ai_derived:
            for d in ai_derived[:8]:
                print(f"    → {d['keyword']:15s}  ({d['source']}) {d['reason'][:40]}")

    # 6c. AI insight 建议的关键词
    ai_suggested = None
    if market_insight:
        ai_suggested = market_insight.get("suggested_keywords", [])
        if ai_suggested:
            print(f"  AI 市场洞察推荐：{', '.join(ai_suggested)}")

    # 7. 关键词生命周期更新
    print("\n[分析] 更新关键词生命周期...")
    kw_status_store = load_keyword_status(VOCAB_DIR)
    lifecycle_result = update_keyword_lifecycle(
        keyword_stats, ngram_candidates, ai_suggested, ai_derived,
        kw_status_store, date_str,
    )

    # 8. 词库学习
    if light_client and all_items_flat:
        from vocab_learner import learn_from_scan
        print("\n[学习] 从标题学习新信号词...")
        learn_result = await learn_from_scan(all_items_flat, vocabulary, light_client, settings)
        if learn_result.auto_added:
            print(f"  自动加入 {len(learn_result.auto_added)} 个新词")
        if learn_result.pending_review:
            print(f"  {len(learn_result.pending_review)} 个词待审核")

    # 9. 生成报告
    print("\n[报告] 生成分析报告...")

    # 把 keyword_evaluations 从 keyword_status_store 还原（供 reporter 使用）
    kw_status_store_for_report = load_keyword_status(VOCAB_DIR)
    keyword_evaluations = {}
    for kw, info in kw_status_store_for_report.items():
        if isinstance(info, dict):
            keyword_evaluations[kw] = KeywordEvaluation(
                keyword=kw,
                status=info.get("status", "valid"),
                relevance_score=float(info.get("relevance_score", 0.75)),
                reason=info.get("reason", ""),
                suggested_alternatives=info.get("alternatives", []),
            )

    # 为报告添加细分建议（从 keyword_analyzer 或 n-gram）
    for gap in gaps:
        if "subdivision_suggestions" not in gap:
            gap["subdivision_suggestions"] = []

    report_content = generate(
        gaps,
        date_str,
        advice_map=advice_map,
        keyword_evaluations=keyword_evaluations,
    )

    # 追加市场洞察和关键词发现到报告末尾
    extra_sections = []

    if market_insight:
        extra_sections.append("\n## 市场洞察（AI 深度分析）\n")
        if market_insight.get("title_patterns"):
            extra_sections.append("**成功标题模式**：\n")
            for p in market_insight["title_patterns"]:
                extra_sections.append(f"- {p}\n")
        if market_insight.get("pricing_insight"):
            extra_sections.append(f"\n**定价策略**：{market_insight['pricing_insight']}\n")
        if market_insight.get("differentiation"):
            extra_sections.append(f"\n**差异化方法**：{market_insight['differentiation']}\n")
        if market_insight.get("opportunities"):
            extra_sections.append("\n**空白机会**：\n")
            for opp in market_insight["opportunities"]:
                extra_sections.append(f"- {opp}\n")

    if lifecycle_result.get("ai_derived"):
        extra_sections.append("\n## AI 衍生关键词（基于内容语义）\n\n")
        extra_sections.append("| 细分关键词 | 来源宽泛词 | 推荐理由 |\n")
        extra_sections.append("|-----------|-----------|----------|\n")
        for d in lifecycle_result["ai_derived"][:15]:
            extra_sections.append(f"| {d['keyword']} | {d['source']} | {d['reason'][:60]} |\n")

    if ngram_candidates:
        extra_sections.append("\n## 关键词发现（n-gram 统计）\n\n")
        extra_sections.append("| 候选词 | 出现频率 |\n")
        extra_sections.append("|--------|----------|\n")
        for kw, freq in ngram_candidates[:15]:
            extra_sections.append(f"| {kw} | {freq} |\n")

    if lifecycle_result.get("retired"):
        extra_sections.append(f"\n**本次淘汰**：{', '.join(lifecycle_result['retired'])}\n")
    if lifecycle_result.get("new_candidates"):
        extra_sections.append(f"\n**新增候选**（已自动写入 candidates.txt）：{', '.join(lifecycle_result['new_candidates'])}\n")

    if extra_sections:
        report_content += "\n---\n" + "".join(extra_sections)

    report_path = save(report_content, date_str)

    # 10. 保存分析结果
    analysis_result = {
        "date": date_str,
        "gaps_count": len(gaps),
        "enriched_count": len(enriched_data),
        "market_insight": market_insight,
        "ai_derived_keywords": ai_derived[:20],
        "ngram_candidates": ngram_candidates[:20],
        "lifecycle": lifecycle_result,
    }
    analysis_dir = DATA_DIR / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / f"{date_str}.json").write_text(
        json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 摘要
    print(f"\n{'='*50}")
    print(f"[analyze] 分析完成")
    valid_gaps = [g for g in gaps if g.get("keyword_status") != "invalid"]
    print(f"\n  Top 5 缺口：")
    for i, g in enumerate(valid_gaps[:5], 1):
        print(f"    {i}. {g['keyword']:20s}  缺口分 {g['gap_score']:6.2f}  建议定价 {g['suggested_price']}")
    print(f"\n  完整报告：{report_path}")


async def main(args: argparse.Namespace) -> None:
    date_str = args.date or date.today().isoformat()
    await run_analyze(date_str, no_ai=args.no_ai)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="闲鱼虚拟商品缺口扫描器 v2 — 分析")
    parser.add_argument("--date", type=str, default=None, help="分析日期（YYYY-MM-DD）")
    parser.add_argument("--no-ai", action="store_true", help="跳过 AI 分析步骤")
    args = parser.parse_args()
    asyncio.run(main(args))
