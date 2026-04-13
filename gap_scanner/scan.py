"""
闲鱼虚拟商品缺口扫描器 - 主入口

用法：
    python scan.py            # 扫描 keywords.txt 中所有关键词
    python scan.py --dry-run  # 空跑，测试环境是否正常（不访问闲鱼）
    python scan.py --limit 5  # 只扫描前 5 个关键词（调试用）

流程：
    1. 读取 keywords.txt
    2. 对每个关键词：
       a. 搜索「关键词」→ 获取供给数据
       b. 搜索「关键词 求」→ 统计需求帖数
    3. 计算缺口分，生成 reports/YYYY-MM-DD.md
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

KEYWORDS_FILE = Path("keywords.txt")


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


async def run_scan(keywords: list[str], today: str) -> list[dict]:
    """执行完整扫描，返回所有关键词的缺口数据。"""
    gaps: list[dict] = []

    async with XianyuFetcher() as fetcher:
        total = len(keywords)
        for idx, kw in enumerate(keywords, 1):
            print(f"\n[{idx}/{total}] 扫描关键词：{kw}")

            # Track B-1：搜索供给
            supply = await fetcher.search(kw, pages=2)
            save_raw(kw, supply, today)

            # Track B-2：搜索需求帖
            demand = await fetcher.count_demand(kw)

            # 计算缺口
            gap = calculate_gap(kw, supply, demand)
            gaps.append(gap)

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

    return gaps


def dry_run_test(keywords: list[str], today: str) -> None:
    """空跑测试：用假数据走完完整流程，不访问闲鱼。"""
    import random as _r
    print(f"[dry-run] 生成 {len(keywords)} 个假数据条目...")
    gaps = []
    for kw in keywords:
        demand = _r.randint(1, 30)
        supply_count = _r.randint(1, 20)
        fake_supply = [
            {
                "item_id": f"fake_{i}",
                "title": f"{kw}教程合集",
                "price": _r.uniform(9, 99),
                "want_num": _r.randint(0, 50),
                "pub_ts": 0,
                "is_virtual": True,
                "is_demand": False,
            }
            for i in range(supply_count)
        ]
        save_raw(kw, fake_supply, today)
        gap = calculate_gap(kw, fake_supply, demand)
        gaps.append(gap)

    report = generate(gaps, today)
    path = save(report, today)
    print(f"\n[dry-run] 完成，报告已写入 {path}")
    _print_top5(gaps)


def _print_top5(gaps: list[dict]) -> None:
    sorted_gaps = sorted(gaps, key=lambda x: x["gap_score"], reverse=True)
    print("\n===== Top 5 缺口 =====")
    for i, g in enumerate(sorted_gaps[:5], 1):
        print(
            f"  {i}. {g['keyword']:20s}  缺口分 {g['gap_score']:6.2f}  "
            f"建议定价 {g['suggested_price']}"
        )


async def main(args: argparse.Namespace) -> None:
    today = date.today().isoformat()
    keywords = load_keywords(limit=args.limit)
    print(f"[scan] 日期 {today}，共 {len(keywords)} 个关键词")

    if args.dry_run:
        dry_run_test(keywords, today)
        return

    # 检查登录状态文件（支持我们自己的 + ai-goofish-monitor 的 state 格式）
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

    gaps = await run_scan(keywords, today)

    # 生成并保存报告
    report_content = generate(gaps, today)
    report_path = save(report_content, today)

    # 打印 Top 5 缩略信息到终端
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
    args = parser.parse_args()
    asyncio.run(main(args))
