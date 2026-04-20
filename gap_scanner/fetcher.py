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
DETAIL_API_FRAGMENT = "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail"

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


def _parse_fish_tags(ex: dict, args: dict) -> list[str]:
    """解析 fishTags（包邮、验货宝等），与主项目 src/parsers.py 一致。"""
    tags: list[str] = []
    if args.get("tag") == "freeship":
        tags.append("包邮")
    r1 = (ex.get("fishTags") or {}).get("r1") or {}
    for tag_item in r1.get("tagList") or []:
        if not isinstance(tag_item, dict):
            continue
        content = (tag_item.get("data") or {}).get("content", "")
        if content and content not in tags:
            tags.append(content)
    return tags


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

            if not items and args:
                print(f"  [debug] clickParam.args keys: {list(args.keys())}")

            title = ex.get("title", "")
            price = _parse_price(ex.get("price", []))

            raw_want = args.get("wantNum")
            if raw_want is None or raw_want == "":
                raw_want = args.get("want_num", "0")
            try:
                want_num = int(str(raw_want).strip())
            except (ValueError, TypeError):
                want_num = 0

            pub_ts = str(args.get("publishTime", "0"))
            item_id = ex.get("itemId", "") or args.get("itemId", "")

            area = ex.get("area", "") or ""
            pic_url = ex.get("picUrl", "") or ""
            raw_link = main.get("targetUrl", "") or ""
            item_url = raw_link.replace("fleamarket://", "https://www.goofish.com/")
            seller_name = ex.get("userNickName", "") or ""
            ori_price = ex.get("oriPrice", "")
            if ori_price is None:
                ori_price = ""
            fish_tags = _parse_fish_tags(ex, args)

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
                "area": area,
                "pic_url": pic_url,
                "item_url": item_url,
                "seller_name": seller_name,
                "ori_price": ori_price,
                "fish_tags": fish_tags,
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

    async def fetch_detail(self, item_url: str) -> dict | None:
        """
        访问商品详情页，拦截详情 API 响应，提取 itemDO + sellerDO。

        反风控改造（2026-04-20）：
        - 复用搜索同一个 page（不再频繁 new_page/close，避免"开关页签"机器人信号）
        - 加入"人类行为"：页面加载后随机滚动 + 停留
        - 访问完不立即跳走，保持在页面 3-8s（模拟阅读）

        返回增强字段 dict，失败返回 None；
        触发风控时返回 {"_risk_control": True}。
        """
        if not item_url:
            return None

        # 关键改动：复用搜索时用的 self._page，不再开新页签
        # 新页签 + 立刻 goto + 立刻 close 是机器人的典型行为
        page = self._page

        try:
            async with page.expect_response(
                lambda r: DETAIL_API_FRAGMENT in r.url, timeout=25000
            ) as detail_info:
                await page.goto(
                    item_url, wait_until="domcontentloaded", timeout=25000
                )

            resp = await detail_info.value
            if not resp.ok:
                print(f"    [detail] HTTP {resp.status}，跳过")
                return None

            json_data = await resp.json()

            ret_val = json_data.get("ret", [])
            if "FAIL_SYS_USER_VALIDATE" in str(ret_val):
                print("    [detail] 触发风控验证 (FAIL_SYS_USER_VALIDATE)")
                # 关键：保留页面给用户手动过验证
                # 上层决定是 stop 还是等用户干预
                return {"_risk_control": True}

            item_do = json_data.get("data", {}).get("itemDO", {})
            seller_do = json_data.get("data", {}).get("sellerDO", {})

            image_infos = item_do.get("imageInfos", []) or []
            image_urls = [
                img.get("url") for img in image_infos
                if isinstance(img, dict) and img.get("url")
            ]

            desc_text = item_do.get("desc", "") or ""

            zhima_info = seller_do.get("zhimaLevelInfo", {}) or {}
            zhima_level = zhima_info.get("levelName", "") or ""

            # 人类行为模拟：随机滚动 + 停留
            # 真人进详情页会滑到图片区、描述区再返回
            try:
                await page.mouse.wheel(0, random.randint(200, 600))
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await page.mouse.wheel(0, random.randint(300, 800))
                await asyncio.sleep(random.uniform(1.5, 4.0))
                # 偶尔往回滑一下（更真实）
                if random.random() < 0.4:
                    await page.mouse.wheel(0, -random.randint(100, 400))
                    await asyncio.sleep(random.uniform(0.5, 1.5))
            except Exception:
                pass

            return {
                "want_count": int(item_do.get("wantCnt", 0) or 0),
                "browse_count": int(item_do.get("browseCnt", 0) or 0),
                "description": desc_text[:2000],
                "image_urls": image_urls,
                "seller_reg_days": int(seller_do.get("userRegDay", 0) or 0),
                "zhima_level": zhima_level,
                "item_status": item_do.get("status", ""),
                "category_name": item_do.get("categoryName", ""),
            }

        except PlaywrightTimeoutError:
            print(f"    [detail] 详情页超时")
            return None
        except Exception as e:
            print(f"    [detail] 详情页出错：{e}")
            return None

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
