"""
Playwright 浏览器解析模块
通过无头浏览器执行抖音JS虚拟机，拦截API响应获取视频数据
"""

import json
import urllib.parse
from typing import Optional


async def parse_douyin_by_browser(aweme_id: str) -> Optional[dict]:
    """
    通过浏览器拦截API响应来解析抖音视频。
    每次调用创建新的浏览器实例，避免状态污染。
    """
    from playwright.async_api import async_playwright

    url = f"https://www.douyin.com/video/{aweme_id}"
    detail_result = None

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await context.new_page()

        async def on_response(resp):
            nonlocal detail_result
            if "aweme/v1/web/aweme/detail" in resp.url and resp.status == 200:
                try:
                    body = await resp.json()
                    if body.get("aweme_detail"):
                        detail_result = body["aweme_detail"]
                except Exception:
                    pass

        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(8000)

        if not detail_result:
            render_data = await page.evaluate("""() => {
                const el = document.querySelector('script#RENDER_DATA');
                return el ? el.textContent : null;
            }""")
            if render_data:
                try:
                    decoded = json.loads(urllib.parse.unquote(render_data))
                    detail_result = _find_aweme_detail(decoded)
                except Exception:
                    pass

        await page.close()
        await browser.close()
        await pw.stop()

        if detail_result:
            return _format_result(detail_result)
        return None

    except Exception as e:
        print(f"[浏览器解析] 异常: {e}")
        return None


def _find_aweme_detail(obj, depth=0):
    if depth > 6 or not isinstance(obj, dict):
        return None
    for key in ("awemeDetail", "aweme_detail"):
        if key in obj and obj[key]:
            return obj[key]
    for v in obj.values():
        if isinstance(v, dict):
            r = _find_aweme_detail(v, depth + 1)
            if r:
                return r
    return None


def _format_result(detail: dict) -> dict:
    video = detail.get("video", {})
    author = detail.get("author", {})
    images = detail.get("images") or []

    if images:
        image_urls = []
        for img in images:
            url_list = img.get("url_list", [])
            if url_list:
                image_urls.append(url_list[-1])
        return {
            "type": "image",
            "title": detail.get("desc", ""),
            "images": image_urls,
            "cover": (video.get("cover", {}).get("url_list") or [""])[0],
            "author": {
                "name": author.get("nickname", ""),
                "id": author.get("unique_id") or author.get("short_id", ""),
            },
            "source": "browser_intercept",
        }

    play_addr = video.get("play_addr", {})
    download_addr = video.get("download_addr", {})

    nwm_url = ""
    if download_addr.get("url_list"):
        nwm_url = download_addr["url_list"][0]
    elif play_addr.get("url_list"):
        nwm_url = play_addr["url_list"][0].replace("playwm", "play")
    elif play_addr.get("uri"):
        nwm_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={play_addr['uri']}&ratio=1080p&line=0"

    return {
        "type": "video",
        "title": detail.get("desc", ""),
        "url": nwm_url,
        "video_id": play_addr.get("uri", ""),
        "cover": (video.get("cover", {}).get("url_list") or [""])[0],
        "duration": video.get("duration", 0),
        "width": play_addr.get("width", 0),
        "height": play_addr.get("height", 0),
        "author": {
            "name": author.get("nickname", ""),
            "id": author.get("unique_id") or author.get("short_id", ""),
        },
        "music": {
            "title": detail.get("music", {}).get("title", ""),
            "url": (detail.get("music", {}).get("play_url", {}).get("url_list") or [""])[0]
            if isinstance(detail.get("music", {}).get("play_url"), dict)
            else "",
        },
        "source": "browser_intercept",
    }
