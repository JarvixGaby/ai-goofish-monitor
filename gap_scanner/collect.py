"""
闲鱼虚拟商品缺口扫描器 v2 — Phase 1: 采集

只负责搜索 + 存数据，不做分析。分析由 analyze.py 异步完成。

用法：
    python collect.py                     # broad + active 全扫
    python collect.py --broad-only        # 只扫宽泛词（发现模式）
    python collect.py --active-only       # 只扫活跃词（日常模式）
    python collect.py --no-detail         # 跳过详情页增强
    python collect.py --detail-limit 10   # 限制详情页数（默认 20）
    python collect.py --limit 5           # 只扫前 N 个关键词（调试）
    python collect.py --no-ai             # 纯词库模式
"""

import argparse
import asyncio
import json
import random
import sys
from datetime import date
from pathlib import Path

from fetcher import XianyuFetcher
from vocabulary import Vocabulary
from config import get_settings
from ai_client import build_client_optional, AIClient
from keyword_analyzer import evaluate_keyword_relevance, load_keyword_status, save_keyword_status

KEYWORDS_DIR = Path("keywords")
VOCAB_DIR = Path("vocab")
DATA_DIR = Path("data")


def _load_keyword_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def load_keywords(
    broad_only: bool = False,
    active_only: bool = False,
    limit: int | None = None,
    keywords_file: str | None = None,
) -> tuple[list[str], list[str]]:
    """
    加载关键词。broad + active + candidates 合并去重，自动排除 retired.txt。

    返回 (main_keywords, candidate_keywords)：
    - main_keywords: broad + active（正式扫描）
    - candidate_keywords: candidates.txt 中的待验证词（扫完后根据 AI 评估自动晋升/淘汰）

    keywords_file：如果指定，只从该文件加载关键词作为 main（用于赛道专项扫描，
    如投资理财 MVP）。此时不加载 broad/active/candidates。
    """
    # 赛道专项模式：只加载指定文件
    if keywords_file:
        path = KEYWORDS_DIR / keywords_file
        if not path.exists() and not keywords_file.endswith(".txt"):
            path = KEYWORDS_DIR / f"{keywords_file}.txt"
        if not path.exists():
            print(f"[collect] 错误：找不到 {path}")
            return [], []
        kws = _load_keyword_file(path)
        retired = set(_load_keyword_file(KEYWORDS_DIR / "retired.txt"))
        main_result = [k for k in kws if k not in retired]
        if limit:
            main_result = main_result[:limit]
        print(f"[collect] 赛道专项模式：从 {path.name} 加载 {len(main_result)} 个词")
        return main_result, []

    broad = [] if active_only else _load_keyword_file(KEYWORDS_DIR / "broad.txt")
    active = [] if broad_only else _load_keyword_file(KEYWORDS_DIR / "active.txt")
    candidates = _load_keyword_file(KEYWORDS_DIR / "candidates.txt")

    if not broad and not active:
        fallback = Path("keywords.txt")
        if fallback.exists():
            active = _load_keyword_file(fallback)
            print(f"[collect] 使用旧版 keywords.txt（{len(active)} 个词）")

    retired = set(_load_keyword_file(KEYWORDS_DIR / "retired.txt"))

    seen: set[str] = set()
    main_result: list[str] = []
    skipped: list[str] = []
    for kw in broad + active:
        if kw in retired:
            if kw not in seen:
                skipped.append(kw)
                seen.add(kw)
            continue
        if kw not in seen:
            seen.add(kw)
            main_result.append(kw)

    # candidates 去重（排除已在 main 和 retired 中的），每次最多验证 5 个
    MAX_CANDIDATES_PER_RUN = 5
    cand_result: list[str] = []
    for kw in candidates:
        if kw not in seen and kw not in retired:
            seen.add(kw)
            cand_result.append(kw)
        if len(cand_result) >= MAX_CANDIDATES_PER_RUN:
            break

    if skipped:
        print(f"[collect] 跳过已淘汰关键词：{', '.join(skipped)}")
    if cand_result:
        remaining = len(candidates) - len(cand_result)
        extra = f"（剩余 {remaining} 个下次验证）" if remaining > 0 else ""
        print(f"[collect] 本次验证候选词：{len(cand_result)} 个{extra}")

    if limit:
        main_result = main_result[:limit]
        cand_result = cand_result[:max(2, limit // 3)]

    return main_result, cand_result


def _save_raw(keyword: str, items: list[dict], date_str: str) -> None:
    """每个关键词的搜索结果存为独立行（兼容旧格式）。"""
    day_dir = DATA_DIR / "raw" / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "search.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(
            {"keyword": keyword, "items": items, "date": date_str},
            ensure_ascii=False,
        ) + "\n")

    # 同时写入旧格式（兼容 scan.py 和 analyze.py 读取）
    DATA_DIR.mkdir(exist_ok=True)
    old_path = DATA_DIR / f"{date_str}.jsonl"
    with open(old_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(
            {"keyword": keyword, "items": items, "date": date_str},
            ensure_ascii=False,
        ) + "\n")


def _save_enriched(item_id: str, enriched: dict, date_str: str) -> None:
    """详情页增强数据，每条商品一个 JSON 文件。"""
    day_dir = DATA_DIR / "enriched" / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{item_id}.json"
    path.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _select_items_for_detail(
    all_items: list[dict],
    detail_limit: int,
) -> list[dict]:
    """
    从搜索结果中筛选值得进入详情页的商品。

    筛选策略：
    1. 分类为 virtual 或 weak_virtual
    2. 价格在合理区间（¥1-500）
    3. 按价格排序取 Top N（高价商品更可能有成交）
    """
    candidates = []
    for item in all_items:
        cls = item.get("classification", "")
        if cls not in ("virtual", "weak_virtual"):
            continue
        price = item.get("price", 0)
        if not (1 <= price <= 500):
            continue
        if not item.get("item_url"):
            continue
        candidates.append(item)

    # 去重（同一商品可能出现在多个关键词搜索中）
    seen_ids: set[str] = set()
    unique: list[dict] = []
    for item in candidates:
        iid = item.get("item_id", "")
        if iid and iid in seen_ids:
            continue
        seen_ids.add(iid)
        unique.append(item)

    # 按价格降序（高价更可能有成交 → 更值得深入）
    unique.sort(key=lambda x: x.get("price", 0), reverse=True)
    return unique[:detail_limit]


def _promote_candidates(keyword_status_store: dict, today: str) -> dict:
    """
    根据 AI 评估结果，对 candidates 执行晋升/淘汰：
    - valid → active.txt（晋升）
    - invalid → retired.txt（淘汰）
    - noisy → 保留在 candidates.txt（下次再评估）
    """
    candidates_path = KEYWORDS_DIR / "candidates.txt"
    active_path = KEYWORDS_DIR / "active.txt"
    retired_path = KEYWORDS_DIR / "retired.txt"

    candidates = _load_keyword_file(candidates_path)
    if not candidates:
        return {"promoted": [], "retired": [], "kept": []}

    promoted: list[str] = []
    retired: list[str] = []
    kept: list[str] = []

    active_set = set(_load_keyword_file(active_path))

    for kw in candidates:
        info = keyword_status_store.get(kw, {})
        status = info.get("status", "") if isinstance(info, dict) else ""

        if status == "valid":
            if kw not in active_set:
                promoted.append(kw)
        elif status == "invalid":
            retired.append(kw)
        else:
            kept.append(kw)

    # 执行晋升
    if promoted:
        with open(active_path, "a", encoding="utf-8") as f:
            f.write(f"\n# 自动晋升 {today}\n")
            for kw in promoted:
                f.write(kw + "\n")

    # 执行淘汰
    if retired:
        with open(retired_path, "a", encoding="utf-8") as f:
            for kw in retired:
                f.write(f"{kw}  # 候选词验证无效  {today}\n")

    # 重写 candidates（只保留未决定的）
    if promoted or retired:
        with open(candidates_path, "w", encoding="utf-8") as f:
            f.write("# 待验证关键词 — 由 analyze.py 自动写入\n")
            f.write("# 验证有效后会晋升到 active.txt\n")
            if kept:
                f.write(f"\n# 待复验 {today}\n")
                for kw in kept:
                    f.write(kw + "\n")

    if promoted:
        print(f"  [晋升] {len(promoted)} 个候选词晋升为活跃词：{', '.join(promoted)}")
    if retired:
        print(f"  [淘汰] {len(retired)} 个候选词验证无效：{', '.join(retired)}")
    if kept:
        print(f"  [保留] {len(kept)} 个候选词待下次复验")

    return {"promoted": promoted, "retired": retired, "kept": kept}


async def run_collect(
    keywords: list[str],
    candidate_keywords: list[str],
    today: str,
    vocabulary: Vocabulary,
    ai_client: AIClient | None,
    no_detail: bool = False,
    detail_limit: int = 20,
    detail_only: bool = False,
) -> dict:
    """
    执行采集。返回采集摘要 dict。

    keywords: 正式关键词（broad + active）
    candidate_keywords: 待验证的候选词（扫描后根据评估结果晋升/淘汰）
    detail_only: 跳过搜索阶段，读取今日已有的 search.jsonl 只做详情页增强
                （用于风控后重试详情页，避免重复搜索消耗）
    """
    all_keywords = keywords + candidate_keywords
    all_items: list[dict] = []
    keyword_stats: list[dict] = []
    keyword_status_store = load_keyword_status(VOCAB_DIR)

    # detail-only 模式：从已有 search.jsonl 读取商品，跳过搜索
    if detail_only:
        search_path = DATA_DIR / "raw" / today / "search.jsonl"
        if not search_path.exists():
            print(f"[collect] detail-only 模式需要 {search_path}，但该文件不存在")
            return {"date": today, "error": "no_search_data"}
        with open(search_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    all_items.extend(entry.get("items", []))
                except json.JSONDecodeError:
                    continue
        print(f"[collect] detail-only 模式：从 {search_path} 加载 {len(all_items)} 条商品")

    candidate_set = set(candidate_keywords)
    async with XianyuFetcher(vocabulary=vocabulary) as fetcher:
        # detail-only 模式跳过搜索，直接进详情页阶段
        if detail_only:
            total = 0
        else:
            total = len(all_keywords)
        for idx, kw in enumerate(all_keywords if not detail_only else [], 1):
            is_candidate = kw in candidate_set
            label = " [候选验证]" if is_candidate else ""
            print(f"\n[{idx}/{total}] 采集关键词：{kw}{label}")

            # 搜索：宽泛词 3 页，精确词 2 页
            is_broad = kw in _load_keyword_file(KEYWORDS_DIR / "broad.txt")
            pages = 3 if is_broad else 2

            supply = await fetcher.search(kw, pages=pages)
            _save_raw(kw, supply, today)

            # AI 兜底分类（仅未命中项）
            if ai_client:
                from ai_classifier import classify_batch
                unmatched = sum(1 for i in supply if i.get("classification") == "unknown")
                if unmatched > 0:
                    print(f"  [AI] 对 {unmatched} 条未命中标题进行分类...")
                    await classify_batch(supply, ai_client, vocabulary)

            # 搜索需求帖
            demand_count = await fetcher.count_demand(kw)

            # AI 关键词有效性评估
            kw_status = "valid"
            if ai_client:
                eval_result = await evaluate_keyword_relevance(kw, supply, ai_client)
                kw_status = eval_result.status
                keyword_status_store[kw] = {
                    "status": eval_result.status,
                    "relevance_score": eval_result.relevance_score,
                    "reason": eval_result.reason,
                    "alternatives": eval_result.suggested_alternatives,
                    "last_evaluated": today,
                }
                save_keyword_status(VOCAB_DIR, keyword_status_store)
                if eval_result.status == "invalid":
                    print(f"  [WARN] 关键词「{kw}」无效，搜索结果与虚拟商品意图不符")

            stat = {
                "keyword": kw,
                "is_broad": is_broad,
                "total_items": len(supply),
                "demand_count": demand_count,
                "keyword_status": kw_status,
            }
            keyword_stats.append(stat)
            all_items.extend(supply)

            print(
                f"  → {len(supply)} 条商品，{demand_count} 条需求帖"
                f"{'  [无效关键词]' if kw_status == 'invalid' else ''}"
            )

            if idx < total:
                wait = random.uniform(8, 15)
                print(f"  等待 {wait:.0f}s...")
                await asyncio.sleep(wait)

        # Phase 1.5: 详情页增强
        # 反风控策略（2026-04-20）：
        # - 每 N 条做一次长休息（模拟人类去干别的事）
        # - 触发风控时暂停 + 提示用户手动过滑块，而不是直接退出
        enriched_count = 0
        if not no_detail:
            detail_candidates = _select_items_for_detail(all_items, detail_limit)
            if detail_candidates:
                print(f"\n[详情页] 从 {len(all_items)} 条中筛选 {len(detail_candidates)} 条进行详情页增强...")
                from config import get_settings
                settings = get_settings()
                REST_EVERY = 5  # 每抓 5 个详情页长休息一次
                REST_MIN, REST_MAX = 60, 120  # 长休息 1-2 分钟

                stop_all = False
                for i, item in enumerate(detail_candidates, 1):
                    if stop_all:
                        break
                    title_short = item.get("title", "")[:30]
                    print(f"  [{i}/{len(detail_candidates)}] {title_short}...")

                    detail = await fetcher.fetch_detail(item.get("item_url", ""))
                    if detail is None:
                        continue

                    if detail.get("_risk_control"):
                        # 风控触发：给用户一次手动过验证的机会
                        print("  [RISK] 触发风控，请在打开的浏览器窗口中完成滑块验证")
                        print("         完成后按 Enter 继续，或直接 Ctrl+C 停止")
                        try:
                            # 等待用户手动过验证后按 Enter
                            await asyncio.get_event_loop().run_in_executor(
                                None, input, "  [等待] 手动过验证后按 Enter："
                            )
                            # 过完验证后再等一小会儿让 session 稳定
                            print("  [RISK] 验证后冷却 30s...")
                            await asyncio.sleep(30)
                            # 重试这个 item（不算 enrich 成功）
                            detail = await fetcher.fetch_detail(item.get("item_url", ""))
                            if detail is None or detail.get("_risk_control"):
                                print("  [STOP] 验证后仍无法访问，停止详情页采集")
                                stop_all = True
                                continue
                        except (KeyboardInterrupt, EOFError):
                            print("\n  [STOP] 用户中止")
                            stop_all = True
                            continue

                    # 合并数据
                    enriched = {**item, **detail, "enriched_date": today}
                    _save_enriched(item.get("item_id", f"unknown_{i}"), enriched, today)
                    enriched_count += 1

                    if i < len(detail_candidates):
                        # 每 N 条长休息一次
                        if i % REST_EVERY == 0:
                            rest = random.uniform(REST_MIN, REST_MAX)
                            print(f"    [rest] 已抓 {i} 条，长休息 {rest:.0f}s（减低风控）...")
                            await asyncio.sleep(rest)
                        else:
                            # 常规间隔：比原来拉长一倍（真人看详情 20-40s 才正常）
                            wait = random.uniform(
                                max(settings.detail_interval_min * 1.5, 20),
                                max(settings.detail_interval_max * 1.5, 40),
                            )
                            print(f"    等待 {wait:.0f}s...")
                            await asyncio.sleep(wait)

    # 候选词晋升/淘汰
    promotion_result = {}
    if candidate_keywords and ai_client:
        print("\n[候选词] 根据 AI 评估结果执行晋升/淘汰...")
        promotion_result = _promote_candidates(keyword_status_store, today)

    summary = {
        "date": today,
        "total_keywords": len(all_keywords),
        "total_items": len(all_items),
        "enriched_items": enriched_count,
        "keyword_stats": keyword_stats,
        "candidate_promotion": promotion_result,
    }

    # 保存采集摘要
    summary_path = DATA_DIR / "raw" / today / "collect_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


async def main(args: argparse.Namespace) -> None:
    today = date.today().isoformat()
    keywords, candidate_keywords = load_keywords(
        broad_only=args.broad_only,
        active_only=args.active_only,
        limit=args.limit,
        keywords_file=args.keywords_file,
    )

    if not keywords and not candidate_keywords:
        print("[collect] 没有关键词可扫描。检查 keywords/ 目录。")
        sys.exit(1)

    total = len(keywords) + len(candidate_keywords)
    print(f"[collect] 日期 {today}，正式词 {len(keywords)} 个 + 候选词 {len(candidate_keywords)} 个 = {total} 个")

    vocabulary = Vocabulary(VOCAB_DIR)
    stats = vocabulary.stats()
    print(f"[vocab] 词库：" + "  ".join(f"{k}={v}" for k, v in stats.items()))

    ai_client: AIClient | None = None
    if not args.no_ai:
        try:
            settings = get_settings()
            ai_client = build_client_optional(settings)
            if ai_client:
                print(f"[AI] 轻量模型：{settings.ai_model}")
                print(f"[AI] 分析模型：{settings.ai_analysis_model}")
        except ValueError as e:
            print(f"[AI] 配置不完整，纯词库模式：{e}")

    # 检查登录状态
    state_files = [
        Path("state/login_state.json"),
        Path("state/acc_1.json"),
        Path("state/acc_2.json"),
    ]
    if not any(p.exists() for p in state_files):
        print("[ERROR] 未找到登录状态文件，运行 python login.py")
        sys.exit(1)

    detail_limit = args.detail_limit
    if detail_limit is None:
        try:
            detail_limit = get_settings().detail_limit
        except Exception:
            detail_limit = 20

    summary = await run_collect(
        keywords=keywords,
        candidate_keywords=candidate_keywords,
        today=today,
        vocabulary=vocabulary,
        ai_client=ai_client,
        no_detail=args.no_detail,
        detail_limit=detail_limit,
        detail_only=args.detail_only,
    )

    print(f"\n{'='*50}")
    print(f"[collect] 采集完成")
    print(f"  关键词：{summary['total_keywords']} 个")
    print(f"  商品卡片：{summary['total_items']} 条")
    print(f"  详情页增强：{summary['enriched_items']} 条")
    print(f"  数据保存：data/raw/{today}/")
    if summary['enriched_items'] > 0:
        print(f"  增强数据：data/enriched/{today}/")
    promo = summary.get("candidate_promotion", {})
    if promo.get("promoted"):
        print(f"  候选词晋升：{', '.join(promo['promoted'])}")
    if promo.get("retired"):
        print(f"  候选词淘汰：{', '.join(promo['retired'])}")
    print(f"\n下一步：python analyze.py  （异步分析今日数据）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="闲鱼虚拟商品缺口扫描器 v2 — 采集")
    parser.add_argument("--broad-only", action="store_true", help="只扫宽泛种子词")
    parser.add_argument("--active-only", action="store_true", help="只扫活跃词")
    parser.add_argument("--no-detail", action="store_true", help="跳过详情页增强")
    parser.add_argument("--detail-limit", type=int, default=None, help="详情页数量上限")
    parser.add_argument(
        "--detail-only",
        action="store_true",
        help="只跑详情页：读今日 search.jsonl 中的商品做详情页增强（风控后重试用）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只扫前 N 个关键词")
    parser.add_argument("--no-ai", action="store_true", help="纯词库模式")
    parser.add_argument(
        "--keywords-file",
        type=str,
        default=None,
        help="赛道专项模式：指定 keywords/ 下的文件名（如 investment.txt），只扫该文件",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
