"""
Xianyu 爬虫核心模块。

使用 Playwright 拦截 MTOP API 响应，提取搜索结果。
关键 API：mtop.idle.web.xyh.item.list（搜索商品列表）

数据来源参考：https://github.com/Usagi-org/ai-goofish-monitor
"""

import asyncio
import random
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# 按优先级查找可用的 state 文件：
#   1. 我们自己的 state/login_state.json（login.py 保存的）
#   2. ai-goofish-monitor 的 state/acc_1.json（已有安装时可直接复用）
_STATE_CANDIDATES = [
    Path("state/login_state.json"),
    Path("state/acc_1.json"),
    Path("state/acc_2.json"),
]


def _find_state_file() -> Path | None:
    """返回第一个存在的 state 文件，没有则返回 None。"""
    for p in _STATE_CANDIDATES:
        if p.exists():
            print(f"[fetcher] 使用登录状态：{p}")
            return p
    return None

# 虚拟商品标题关键词：命中其一即认为是虚拟供给
VIRTUAL_KWS = frozenset(
    ["教程", "文档", "资料", "课程", "指导", "网盘", "模板", "方法论",
     "指南", "攻略", "代操作", "代写", "代发", "服务", "合集", "电子书"]
)

# 需求帖关键词：命中其一即认为是求购类帖子
DEMAND_KWS = frozenset(
    ["求", "有没有", "哪里", "推荐", "收", "求购", "想要", "需要"]
)


async def _random_sleep(min_s: float = 2.0, max_s: float = 5.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


def _is_virtual(title: str) -> bool:
    return any(kw in title for kw in VIRTUAL_KWS)


def _is_demand(title: str) -> bool:
    return any(kw in title for kw in DEMAND_KWS)


def _parse_price(price_parts) -> float:
    """从 MTOP price 字段（list of dicts）解析出数值价格。"""
    if not price_parts or not isinstance(price_parts, list):
        return 0.0
    text = "".join(str(p.get("text", "")) for p in price_parts if isinstance(p, dict))
    text = (text
            .replace("¥", "")
            .replace("当前价", "")
            .replace(",", "")
            .strip())
    if "万" in text:
        text = text.replace("万", "")
        try:
            return float(text) * 10000
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_items_from_response(json_data: dict) -> list[dict]:
    """
    从 mtop.idle.web.xyh.item.list 的 JSON 响应中解析商品列表。

    数据路径：json_data.data.resultList[].data.item.main.{exContent, clickParam.args}
    """
    items = []
    try:
        result_list = json_data.get("data", {}).get("resultList", [])
        for entry in result_list:
            main = (entry
                    .get("data", {})
                    .get("item", {})
                    .get("main", {}))
            ex = main.get("exContent", {})
            args = main.get("clickParam", {}).get("args", {})

            title = ex.get("title", "")
            price = _parse_price(ex.get("price", []))

            raw_want = args.get("wantNum", "0")
            try:
                want_num = int(raw_want)
            except (ValueError, TypeError):
                want_num = 0

            pub_ts = str(args.get("publishTime", "0"))
            item_id = ex.get("itemId", "") or args.get("itemId", "")

            items.append({
                "item_id": item_id,
                "title": title,
                "price": price,
                "want_num": want_num,
                "pub_ts": int(pub_ts) if pub_ts.isdigit() else 0,
                "is_virtual": _is_virtual(title),
                "is_demand": _is_demand(title),
            })
    except Exception as e:
        print(f"  [parse error] {e}")
    return items


class XianyuFetcher:
    """
    Playwright-based Xianyu fetcher。

    用法：
        async with XianyuFetcher() as f:
            items = await f.search("Cursor教程")
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    async def start(self) -> None:
        """启动浏览器，加载登录状态，完成首页预热。"""
        self._playwright = await async_playwright().start()

        launch_kwargs = dict(
            headless=False,  # 闲鱼反爬需要有头模式
            args=["--disable-blink-features=AutomationControlled"],
        )

        # 优先使用本机 Chrome（指纹更真实）
        try:
            self._browser = await self._playwright.chromium.launch(
                channel="chrome", **launch_kwargs
            )
        except Exception:
            print("[fetcher] Chrome 未找到，使用内置 Chromium")
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        # 加载登录状态（按优先级查找）
        state_file = _find_state_file()
        state_path = str(state_file) if state_file else None
        if not state_path:
            print("[fetcher] 未找到登录状态文件，将以未登录状态运行（部分数据可能缺失）")
            print("[fetcher] 提示：运行 python login.py 完成一次性登录")

        self._context = await self._browser.new_context(
            storage_state=state_path,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # 注入反检测脚本
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
            Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});
        """)

        self._page = await self._context.new_page()

        # 首页预热：模拟真实用户行为
        print("[fetcher] 首页预热...")
        await self._page.goto(
            "https://www.goofish.com/", wait_until="domcontentloaded", timeout=30000
        )
        await _random_sleep(1, 3)

        # 检查是否已登录
        if "passport.goofish.com" in self._page.url or "mini_login" in self._page.url:
            print("[fetcher] 未检测到登录状态，请先运行 python login.py")

    async def stop(self) -> None:
        """关闭浏览器。"""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def search(self, keyword: str, pages: int = 2) -> list[dict]:
        """
        搜索关键词，拦截 MTOP 商品列表 API，返回解析后的商品列表。

        pages：滚动翻页次数（每次约加载 20 条）。
        """
        all_items: list[dict] = []

        # 注册响应监听器，捕获 MTOP 商品列表 API
        async def on_response(response):
            if "mtop.idle.web.xyh.item.list" in response.url:
                try:
                    data = await response.json()
                    parsed = _parse_items_from_response(data)
                    all_items.extend(parsed)
                    print(f"  [API] 已捕获 {len(all_items)} 条（{keyword}）")
                except Exception as e:
                    print(f"  [API parse error] {e}")

        self._page.on("response", on_response)

        try:
            search_url = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"
            print(f"  [search] {search_url}")

            await self._page.goto(
                search_url, wait_until="domcontentloaded", timeout=60000
            )
            await _random_sleep(2, 4)

            # 登录重定向检测
            if ("passport.goofish.com" in self._page.url
                    or "mini_login" in self._page.url):
                print(f"  [ERROR] 登录已过期，请重新运行 python login.py")
                return []

            # 风控弹窗检测
            try:
                await self._page.wait_for_selector(
                    "div.baxia-dialog-mask", timeout=2000
                )
                print(f"  [RISK] 触发风控弹窗，跳过关键词「{keyword}」，稍后重试")
                return []
            except PlaywrightTimeoutError:
                pass  # 正常，继续

            # 滚动加载更多
            for i in range(pages - 1):
                await _random_sleep(3, 6)
                await self._page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await _random_sleep(2, 4)
                print(f"  [scroll] 第 {i + 2} 页，已累计 {len(all_items)} 条")

        except PlaywrightTimeoutError:
            print(f"  [timeout] 搜索「{keyword}」超时，使用已获取的 {len(all_items)} 条")
        except Exception as e:
            print(f"  [error] 搜索「{keyword}」出错：{e}")
        finally:
            self._page.remove_listener("response", on_response)

        return all_items

    async def count_demand(self, keyword: str) -> int:
        """
        搜索「{keyword} 求」，统计结果中的求购帖数量。

        注：此方法复用同一页面，与 search() 交替调用时需注意间隔。
        """
        demand_items = await self.search(f"{keyword} 求", pages=1)
        count = sum(1 for item in demand_items if item["is_demand"])
        # 补充：把含"有没有"等词的帖子也算入
        count += sum(
            1 for item in demand_items
            if not item["is_demand"] and any(kw in item["title"] for kw in ["有没有", "哪里有"])
        )
        return count
