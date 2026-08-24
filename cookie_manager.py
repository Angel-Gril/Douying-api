"""
Cookie管理器 - 混合策略 + 设备指纹随机化
1. 首次调用: Playwright获取完整Cookie(含msToken)，随机化浏览器指纹
2. 后续调用: 直接读缓存，配合curl_cffi+签名实现毫秒级响应
3. Cookie过期: 自动重新获取
"""

import json
import time
import random
import string
from pathlib import Path
from typing import Optional

COOKIE_FILE = Path(__file__).parent / ".douyin_cookies.json"
COOKIE_TTL = 1800  # 30分钟有效期

_cached: Optional[dict] = None

_CHROME_VERSIONS = ["120.0.0.0", "121.0.0.0", "122.0.0.0", "123.0.0.0", "124.0.0.0", "125.0.0.0", "126.0.0.0"]
_SCREEN_SIZES = [(1920, 1080), (2560, 1440), (1366, 768), (1536, 864), (1440, 900)]
_LANGUAGES = ["zh-CN", "zh-CN,zh", "zh-CN,zh;q=0.9,en;q=0.8"]
_TIMEZONES = ["Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin"]


def _random_browser_fingerprint() -> dict:
    """生成随机浏览器指纹，模拟不同真实设备"""
    chrome_ver = random.choice(_CHROME_VERSIONS)
    screen = random.choice(_SCREEN_SIZES)
    return {
        "user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36",
        "viewport": {"width": screen[0], "height": screen[1]},
        "locale": "zh-CN",
        "timezone_id": random.choice(_TIMEZONES),
        "color_scheme": "no-preference",
    }


async def get_cookies() -> dict:
    """返回 {cookie_str: "k=v; ...", names: [...], time: ...}"""
    global _cached

    if _cached and (time.time() - _cached["time"]) < COOKIE_TTL:
        return _cached

    if COOKIE_FILE.exists():
        try:
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if time.time() - data.get("time", 0) < COOKIE_TTL:
                _cached = data
                return _cached
        except Exception:
            pass

    result = await _fetch_cookies_via_browser()
    if result:
        _cached = result
        COOKIE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result or {"cookie_str": "", "names": [], "time": 0}


async def _fetch_cookies_via_browser() -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    fp = _random_browser_fingerprint()
    print(f"[Cookie] 通过浏览器获取Cookie(指纹: Chrome/{fp['user_agent'].split('Chrome/')[1].split(' ')[0]}, {fp['viewport']['width']}x{fp['viewport']['height']})...")

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            user_agent=fp["user_agent"],
            viewport=fp["viewport"],
            locale=fp["locale"],
            timezone_id=fp["timezone_id"],
            color_scheme=fp["color_scheme"],
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            window.chrome = {runtime: {}};
        """)
        page = await context.new_page()

        await page.goto(
            "https://www.douyin.com/video/7639577678311443775",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        # 模拟真实用户行为触发安全SDK生成msToken
        await page.wait_for_timeout(3000)
        await page.mouse.move(random.randint(100, 800), random.randint(200, 600))
        await page.wait_for_timeout(500)
        await page.mouse.wheel(0, random.randint(200, 500))
        await page.wait_for_timeout(2000)
        await page.mouse.move(random.randint(300, 900), random.randint(100, 400))
        await page.wait_for_timeout(1000)

        # 轮询等待msToken生成（安全SDK需要时间验证环境）
        for _ in range(12):
            await page.wait_for_timeout(1000)
            cookies = await context.cookies()
            names = [c["name"] for c in cookies]
            if "msToken" in names and "ttwid" in names:
                break

        cookies = await context.cookies()
        await page.close()
        await browser.close()
        await pw.stop()

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        names = [c["name"] for c in cookies]
        key_cookies = [n for n in names if n in ('ttwid', 'msToken', '__ac_nonce', '__ac_signature', 's_v_web_id', 'odin_tt')]
        print(f"[Cookie] 获取 {len(cookies)} 个, 关键: {key_cookies}")

        if "msToken" not in names:
            print("[Cookie] 警告: 未获取到msToken，签名API可能不稳定")

        return {"cookie_str": cookie_str, "names": names, "time": time.time()}

    except Exception as e:
        print(f"[Cookie] 获取失败: {e}")
        return None


def invalidate():
    global _cached
    _cached = None
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
