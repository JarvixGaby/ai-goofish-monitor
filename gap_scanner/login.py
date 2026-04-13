"""
一次性登录脚本。首次使用前执行，保存闲鱼登录状态到 state/login_state.json。
用法：python login.py
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "login_state.json"


async def main():
    STATE_DIR.mkdir(exist_ok=True)

    print("=" * 50)
    print("闲鱼登录状态保存")
    print("=" * 50)
    print("1. 浏览器将自动打开 goofish.com")
    print("2. 请手动完成登录（扫码 / 账号密码均可）")
    print("3. 看到主页加载完成后，回到此终端按 Enter")
    print()

    async with async_playwright() as p:
        # 尝试使用本机 Chrome，若不存在则降级到 Chromium
        try:
            browser = await p.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            print("[提示] 未找到 Chrome，使用内置 Chromium")
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # 注入反检测脚本
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = await context.new_page()
        await page.goto("https://www.goofish.com/", wait_until="domcontentloaded")

        input("\n登录完成后按 Enter 保存状态...")

        # 保存 Playwright browser state（cookies + localStorage）
        await context.storage_state(path=str(STATE_FILE))
        print(f"\n✓ 登录状态已保存至 {STATE_FILE}")
        print("现在可以运行 python scan.py 开始扫描。")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
