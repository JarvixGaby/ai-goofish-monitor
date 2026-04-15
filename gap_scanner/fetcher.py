"""
Xianyu 爬虫核心模块。

使用 Playwright 拦截 MTOP API 响应，提取搜索结果。
关键 API（搜索）：mtop.taobao.idlemtopsearch.pc.search（POST）
参考 API（卖家主页）：mtop.idle.web.xyh.item.list（本模块不使用）

数据来源参考：https://github.com/Usagi-org/ai-goofish-monitor
"""

import asyncio
import random
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

if TYPE_CHECKING:
    from vocabulary import Vocabulary

# 按优先级查找可用的 state 文件：
#   1. 我们自己的 state/login_state.json（login.py 保存的）
#   2. ai-goofish-monitor 的 state/acc_1.json（已有安装时可直接复用）
# 搜索 API 端点标识（与原始项目 src/services/search_pagination.py 一致）
SEARCH_API_FRAGMENT = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"

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


def _is_search_response(response: Response) -> bool:
    """判断是否为搜索 API 响应（URL 含搜索片段且为 POST）。"""
    url = response.url
    method = response.request.method
    return SEARCH_API_FRAGMENT in url and method == "POST"


def _parse_items_from_response(
    json_data: dict,
    vocabulary: "Vocabulary | None" = None,
) -> list[dict]:
    """
    从搜索 API (idlemtopsearch.pc.search) 的 JSON 响应中解析商品列表。

    数据路径：json_data.data.resultList[].data.item.main.{exContent, clickParam.args}

    若提供 vocabulary，则使用词库匹配对每条标题进行预分类；
    否则 classification 字段设为 "unknown"，由 scanner 或 ai_classifier 后续处理。
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

            # 使用 vocabulary 分类，或标记为待分类
            if vocabulary is not None:
                match_result = vocabulary.match(title)
                classification = match_result.classification
                matched_terms = match_result.matched_terms
            else:
                classification = "unknown"
                matched_terms = []

            items.append({
                "item_id": item_id,
                "title": title,
                "price": price,
                "want_num": want_num,
                "pub_ts": int(pub_ts) if pub_ts.isdigit() else 0,
                "classification": classification,
                "matched_terms": matched_terms,
                # 兼容旧版字段（scanner.py fallback 用）
                "is_virtual": classification == "virtual",
                "is_demand": classification == "demand",
            })
    except Exception as e:
        print(f"  [parse error] {e}")
    return items


async def _random_sleep(min_s: float = 2.0, max_s: float = 5.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


class XianyuFetcher:
    """
    Playwright-based Xianyu fetcher。

    用法：
        async with XianyuFetcher(vocabulary=vocab) as f:
            items = await f.search("Cursor教程")
    """

    def __init__(self, vocabulary: "Vocabulary | None" = None) -> None:
        self._vocabulary = vocabulary
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
        搜索关键词，拦截搜索 API 响应，返回解析后的商品列表。

        pages：翻页次数（第 1 页 = 首次加载，之后点击「下一页」按钮）。
        """
        all_items: list[dict] = []

        try:
            search_url = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"
            print(f"  [search] {search_url}")

            # 先注册 expect_response 再 goto，避免竞态丢失首次响应
            async with self._page.expect_response(
                _is_search_response, timeout=30000
            ) as first_resp_info:
                await self._page.goto(
                    search_url, wait_until="domcontentloaded", timeout=60000
                )

            # 登录重定向检测
            if ("passport.goofish.com" in self._page.url
                    or "mini_login" in self._page.url):
                print(f"  [ERROR] 登录已过期，请重新运行 python login.py")
                return []

            # 解析第 1 页
            first_resp = await first_resp_info.value
            try:
                data = await first_resp.json()
                parsed = _parse_items_from_response(data, self._vocabulary)
                all_items.extend(parsed)
                print(f"  [API] 第 1 页：{len(parsed)} 条（共 {len(all_items)}）")
            except Exception as e:
                print(f"  [API parse error] 第 1 页：{e}")

            await _random_sleep(1, 3)

            # 风控弹窗检测
            try:
                await self._page.wait_for_selector(
                    "div.baxia-dialog-mask", state="visible", timeout=2000
                )
                print(f"  [RISK] 触发风控弹窗，跳过关键词「{keyword}」")
                return all_items
            except PlaywrightTimeoutError:
                pass

            # 翻页（点击「下一页」按钮，与原始项目一致）
            NEXT_BTN = (
                "button[class*='search-pagination-arrow-container']"
                ":has([class*='search-pagination-arrow-right'])"
                ":not([disabled])"
            )
            for page_num in range(2, pages + 1):
                await _random_sleep(2, 5)
                next_btn = self._page.locator(NEXT_BTN).first
                if not await next_btn.count():
                    print(f"  [page] 没有下一页按钮，停止翻页")
                    break

                try:
                    await next_btn.scroll_into_view_if_needed()
                    async with self._page.expect_response(
                        _is_search_response, timeout=20000
                    ) as resp_info:
                        await next_btn.click(timeout=10000)

                    resp = await resp_info.value
                    data = await resp.json()
                    parsed = _parse_items_from_response(data, self._vocabulary)
                    all_items.extend(parsed)
                    print(f"  [API] 第 {page_num} 页：{len(parsed)} 条（共 {len(all_items)}）")
                    await _random_sleep(1, 3)
                except PlaywrightTimeoutError:
                    print(f"  [page] 第 {page_num} 页超时，停止翻页")
                    break
                except Exception as e:
                    print(f"  [page] 第 {page_num} 页出错：{e}")
                    break

        except PlaywrightTimeoutError:
            print(f"  [timeout] 搜索「{keyword}」首页超时，未捕获到数据")
        except Exception as e:
            print(f"  [error] 搜索「{keyword}」出错：{e}")

        return all_items

    async def count_demand(self, keyword: str) -> int:
        """
        搜索「{keyword} 求」，统计结果中的求购帖数量。

        注：此方法复用同一页面，与 search() 交替调用时需注意间隔。
        """
        demand_items = await self.search(f"{keyword} 求", pages=1)

        # 使用词库分类结果统计需求帖
        count = sum(
            1 for item in demand_items
            if item.get("classification") == "demand" or item.get("is_demand", False)
        )
        return count
