"""
闲鱼虚拟商品缺口扫描器 - 主入口

用法：
    python scan.py                    # 扫描 keywords.txt 中所有关键词
    python scan.py --dry-run          # 空跑，测试环境是否正常（不访问闲鱼）
    python scan.py --limit 5          # 只扫描前 5 个关键词（调试用）
    python scan.py --no-ai            # 跳过所有 AI 步骤（纯词库模式）
    python scan.py --no-learn         # 跳过扫描后的词库学习
    python scan.py --review-vocab     # 交互式审核 pending_review.txt 中的候选词

流程（默认完整模式）：
    1. 加载 keywords.txt + 词库 vocab/
    2. 对每个关键词：
       a. Playwright 搜索 → 词库快速分类
       b. AI 兜底分类（仅词库未命中项）
       c. 统计需求帖，计算缺口分
    3. 生成 reports/YYYY-MM-DD.md
    4. AI 词库学习（从本次标题提取新黑话）
"""

import argparse
import asyncio
import random
import sys
from datetime import date
from pathlib import Path

from fetcher import XianyuFetcher
from scanner import calculate_gap, save_raw
from reporter import generate, save
from vocabulary import Vocabulary
from config import get_settings
from ai_client import build_client_optional, AIClient

KEYWORDS_FILE = Path("keywords.txt")
VOCAB_DIR = Path("vocab")


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def load_keywords(limit: int | None = None) -> list[str]:
    """从 keywords.txt 加载关键词，跳过注释行和空行。"""
    if not KEYWORDS_FILE.exists():
        print(f"[ERROR] 找不到 {KEYWORDS_FILE}，请先创建关键词文件")
        sys.exit(1)
    lines = KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()
    keywords = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    if limit:
        keywords = keywords[:limit]
    return keywords


def _print_top5(gaps: list[dict]) -> None:
    sorted_gaps = sorted(gaps, key=lambda x: x["gap_score"], reverse=True)
    print("\n===== Top 5 缺口 =====")
    for i, g in enumerate(sorted_gaps[:5], 1):
        print(
            f"  {i}. {g['keyword']:20s}  缺口分 {g['gap_score']:6.2f}  "
            f"建议定价 {g['suggested_price']}"
        )


# ──────────────────────────────────────────────────────────
# 词库审核交互
# ──────────────────────────────────────────────────────────

def review_vocab(vocabulary: Vocabulary) -> None:
    """交互式审核 pending_review.txt 中的候选词。"""
    pending = vocabulary.load_pending()
    if not pending:
        print("[review] 没有待审核的词条。")
        return

    print(f"\n[review] 共 {len(pending)} 个待审核词条。")
    print("  y = 确认加入词库  n = 忽略  q = 退出审核\n")

    approved: list = []
    for entry in pending:
        prompt = (
            f"  词条: {entry.term:20s}  分类: {entry.category:15s}  "
            f"置信度: {entry.confidence:.2f}  理由: {entry.reason}\n"
            f"  操作 [y/n/q]: "
        )
        try:
            choice = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[review] 已中断。")
            break

        if choice == "q":
            print("[review] 退出审核。")
            break
        elif choice == "y":
            approved.append(entry)
        # n 或其他：跳过

    if approved:
        by_category: dict[str, list] = {}
        for e in approved:
            by_category.setdefault(e.category, []).append(e)
        for category, entries in by_category.items():
            added = vocabulary.add_terms(category, entries)
            print(f"[review] 已加入 {added} 个词到 {category}")
        vocabulary.clear_pending()
        print("[review] 审核完成，pending_review.txt 已清空。")
    else:
        print("[review] 没有词条被确认。")


# ──────────────────────────────────────────────────────────
# 空跑测试
# ──────────────────────────────────────────────────────────

def dry_run_test(
    keywords: list[str],
    today: str,
    vocabulary: Vocabulary,
    ai_client: AIClient | None,
) -> None:
    """空跑测试：用假数据走完完整流程，不访问闲鱼。"""
    import random as _r
    print(f"[dry-run] 生成 {len(keywords)} 个假数据条目...")
    gaps = []
    all_items_flat: list[dict] = []

    for kw in keywords:
        demand = _r.randint(1, 30)
        supply_count = _r.randint(1, 20)

        # 使用词库标注的假供给数据
        fake_supply = []
        virtual_titles = [
            f"{kw}教程合集 百度云秒发",
            f"{kw}保姆级入门资料 永久更新",
            f"手把手带你学{kw} 送售后群",
        ]
        for i in range(supply_count):
            title = virtual_titles[i % len(virtual_titles)]
            match = vocabulary.match(title)
            fake_supply.append({
                "item_id": f"fake_{i}",
                "title": title,
                "price": _r.uniform(9, 99),
                "want_num": _r.randint(0, 50),
                "pub_ts": 0,
                "classification": match.classification,
                "matched_terms": match.matched_terms,
                "is_virtual": match.classification == "virtual",
                "is_demand": match.classification == "demand",
            })

        save_raw(kw, fake_supply, today)
        gap = calculate_gap(kw, fake_supply, demand, vocabulary)
        gaps.append(gap)
        all_items_flat.extend(fake_supply)

    report = generate(gaps, today)
    path = save(report, today)
    print(f"\n[dry-run] 完成，报告已写入 {path}")
    _print_top5(gaps)

    # 展示词库统计
    stats = vocabulary.stats()
    print(f"\n[vocab] 当前词库：" + "  ".join(f"{k}={v}" for k, v in stats.items()))


# ──────────────────────────────────────────────────────────
# 真实扫描
# ──────────────────────────────────────────────────────────

async def run_scan(
    keywords: list[str],
    today: str,
    vocabulary: Vocabulary,
    ai_client: AIClient | None,
) -> tuple[list[dict], list[dict]]:
    """
    执行完整扫描。

    返回：(gaps, all_items_flat)
        gaps:           各关键词的缺口分析结果
        all_items_flat: 本次所有爬取的商品（供词库学习使用）
    """
    gaps: list[dict] = []
    all_items_flat: list[dict] = []

    async with XianyuFetcher(vocabulary=vocabulary) as fetcher:
        total = len(keywords)
        for idx, kw in enumerate(keywords, 1):
            print(f"\n[{idx}/{total}] 扫描关键词：{kw}")

            # Track B-1：搜索供给（fetcher 已在解析时做词库匹配）
            supply = await fetcher.search(kw, pages=2)
            save_raw(kw, supply, today)

            # AI 兜底分类（仅词库未命中项）
            if ai_client:
                from ai_classifier import classify_batch
                unmatched_count = sum(
                    1 for i in supply if i.get("classification") == "unknown"
                )
                if unmatched_count > 0:
                    print(f"  [AI] 对 {unmatched_count} 条未命中标题进行 AI 分类...")
                    await classify_batch(supply, ai_client, vocabulary)

            # Track B-2：搜索需求帖
            demand = await fetcher.count_demand(kw)

            # 计算缺口
            gap = calculate_gap(kw, supply, demand, vocabulary)
            gaps.append(gap)
            all_items_flat.extend(supply)

            print(
                f"  → 缺口分 {gap['gap_score']}  "
                f"（需求帖 {demand} / 虚拟供给 {gap['virtual_supply']} / "
                f"总挂牌 {gap['total_listings']}）"
            )

            # 关键词间随机间隔（减少被风控概率）
            if idx < total:
                wait = random.uniform(8, 15)
                print(f"  等待 {wait:.0f}s 后继续...")
                await asyncio.sleep(wait)

    return gaps, all_items_flat


# ──────────────────────────────────────────────────────────
# 主函数
# ──────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    today = date.today().isoformat()
    keywords = load_keywords(limit=args.limit)
    print(f"[scan] 日期 {today}，共 {len(keywords)} 个关键词")

    # 初始化词库
    vocabulary = Vocabulary(VOCAB_DIR)
    stats = vocabulary.stats()
    print(f"[vocab] 词库加载：" + "  ".join(f"{k}={v}" for k, v in stats.items()))

    # 初始化 AI 客户端（--no-ai 时跳过）
    ai_client: AIClient | None = None
    if not args.no_ai:
        try:
            settings = get_settings()
            ai_client = build_client_optional(settings)
            if ai_client:
                print(f"[AI] 已连接：{settings.ai_model} @ {settings.ai_base_url}")
            else:
                print("[AI] 未配置 AI，以纯词库模式运行（在 .env 中设置 AI_BASE_URL 等启用）")
        except ValueError as e:
            print(f"[AI] 配置不完整，以纯词库模式运行：{e}")

    # --review-vocab 模式
    if args.review_vocab:
        review_vocab(vocabulary)
        return

    # --dry-run 模式
    if args.dry_run:
        dry_run_test(keywords, today, vocabulary, ai_client)
        return

    # 检查登录状态文件
    state_files = [
        Path("state/login_state.json"),
        Path("state/acc_1.json"),
        Path("state/acc_2.json"),
    ]
    if not any(p.exists() for p in state_files):
        print("[ERROR] 未找到登录状态文件")
        print("  选项 A：运行 python login.py（Playwright 扫码登录）")
        print("  选项 B：安装 Chrome 扩展导出状态 → 保存为 state/login_state.json")
        print("          扩展地址：https://chromewebstore.google.com/detail/xianyu-login-state-extrac/eidlpfjiodpigmfcahkmlenhppfklcoa")
        print("  选项 C：如已安装 ai-goofish-monitor，其 state/acc_1.json 可直接使用")
        sys.exit(1)

    # 执行扫描
    gaps, all_items_flat = await run_scan(keywords, today, vocabulary, ai_client)

    # 生成 Phase 2 AI 建议（Phase 2 在下面的 reporter 升级后启用）
    advice_map: dict[str, dict] = {}
    if ai_client and not args.no_ai:
        from ai_advisor import generate_advice_for_top
        sorted_gaps = sorted(gaps, key=lambda x: x["gap_score"], reverse=True)
        print("\n[AI Advisor] 正在生成 Top 5 选品建议...")
        advice_map = await generate_advice_for_top(sorted_gaps[:5], ai_client)

    # 生成并保存报告
    report_content = generate(gaps, today, advice_map=advice_map)
    report_path = save(report_content, today)

    # 词库学习（--no-learn 时跳过）
    if not args.no_learn and ai_client and all_items_flat:
        from vocab_learner import learn_from_scan
        settings = get_settings()
        print("\n[learn] 正在从本次扫描学习新黑话...")
        learn_result = await learn_from_scan(all_items_flat, vocabulary, ai_client, settings)
        if learn_result.auto_added:
            print(f"[learn] 自动加入 {len(learn_result.auto_added)} 个新词")
        if learn_result.pending_review:
            print(f"[learn] {len(learn_result.pending_review)} 个词待审核（运行 --review-vocab 处理）")

    _print_top5(gaps)
    print(f"\n完整报告：{report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="闲鱼虚拟商品缺口扫描器")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="空跑测试（不访问闲鱼，用假数据验证流程）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="只扫描前 N 个关键词（调试用）",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="跳过所有 AI 步骤，以纯词库模式运行",
    )
    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="跳过扫描后的词库学习步骤",
    )
    parser.add_argument(
        "--review-vocab",
        action="store_true",
        help="交互式审核 pending_review.txt 中的候选词",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
