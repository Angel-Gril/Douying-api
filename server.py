"""
短视频无水印解析 - 本地服务器
多策略解析：自建解析优先，公开API兜底
"""

import re
import json
import ipaddress
import os
import socket
import time
import random
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

try:
    from browser_parser import parse_douyin_by_browser
    BROWSER_AVAILABLE = True
except ImportError:
    BROWSER_AVAILABLE = False

from platform_parsers import parse_xiaohongshu, parse_kuaishou

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="视频无水印解析", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:8848,http://127.0.0.1:8848"
    ).split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

# === 配置 ===
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

# 抖音Cookie - 从浏览器获取后填入，没有也能用但成功率低
DOUYIN_COOKIE = os.getenv("DOUYIN_COOKIE", "").strip()
ENABLE_DEBUG_ENDPOINTS = os.getenv("ENABLE_DEBUG_ENDPOINTS", "0").lower() in {"1", "true", "yes"}

# 公开API备用列表
PUBLIC_APIS = [
    "https://api.bugpk.com/api/douyin?url={url}",
]
DOUBAO_APIS = [
    "https://api.bugpk.com/api/dbvideos?url={url}",
]
XHS_APIS = [
    "https://api.bugpk.com/api/xhs?url={url}",
]
KUAISHOU_APIS = [
    "https://api.bugpk.com/api/kuaishou?url={url}",
]


# === 设备指纹随机化 ===

_CHROME_VERSIONS = [
    ("120.0.0.0", "chrome120"),
    ("123.0.0.0", "chrome123"),
    ("124.0.0.0", "chrome124"),
    ("131.0.0.0", "chrome131"),
    ("136.0.0.0", "chrome136"),
]
_SCREEN_SIZES = [(1920, 1080), (2560, 1440), (1366, 768), (1536, 864), (1440, 900)]
_CPU_CORES = [4, 6, 8, 12, 16]
_DEVICE_MEMORY = [4, 8, 16, 32]


def generate_device_fingerprint() -> dict:
    """生成随机但合理的设备指纹，模拟不同真实用户"""
    chrome_ver, impersonate_key = random.choice(_CHROME_VERSIONS)
    screen = random.choice(_SCREEN_SIZES)
    cores = random.choice(_CPU_CORES)
    memory = random.choice(_DEVICE_MEMORY)
    downlink = random.choice([1.5, 2.5, 5, 10, 20])
    rtt = random.choice([50, 100, 150, 200])

    ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"

    return {
        "user_agent": ua,
        "chrome_version": chrome_ver,
        "impersonate_key": impersonate_key,
        "screen_width": str(screen[0]),
        "screen_height": str(screen[1]),
        "cpu_core_num": str(cores),
        "device_memory": str(memory),
        "downlink": str(int(downlink)) if downlink == int(downlink) else str(downlink),
        "round_trip_time": str(rtt),
    }


# === 工具函数 ===

async def resolve_video_redirect(url: str) -> Optional[str]:
    """手动跟踪302重定向链，只读Location头不下载body"""
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}
    current = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        for _ in range(5):
            try:
                resp = await client.head(current, headers=headers)
                if resp.status_code in (301, 302, 303, 307, 308):
                    current = str(resp.headers.get("location", current))
                    if not current.startswith("http"):
                        break
                else:
                    break
            except Exception:
                break
    if current != url:
        return current
    return None


async def resolve_short_url(url: str) -> str:
    """解析短链接302重定向"""
    headers = {"User-Agent": MOBILE_UA}
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        try:
            resp = await client.get(url, headers=headers)
            return str(resp.url)
        except Exception:
            pass
    # 备用: 只跟一层
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                return str(resp.headers.get("location", url))
        except Exception:
            pass
    return url


def extract_aweme_id(url: str) -> Optional[str]:
    """从URL中提取视频ID"""
    patterns = [
        r'/video/(\d+)',
        r'/note/(\d+)',
        r'modal_id=(\d+)',
        r'aweme_id=(\d+)',
        r'/(\d{15,})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


# === 解析策略 ===

async def strategy_iesdouyin_api(aweme_id: str) -> Optional[dict]:
    """
    策略1: iesdouyin.com 旧版API
    这个接口不需要复杂签名，只需要基本的请求头
    历史上比较稳定，但可能随时被关闭
    """
    url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.iesdouyin.com/",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, headers=headers)
            data = resp.json()
            items = data.get("item_list", [])
            if items:
                return parse_iesdouyin_item(items[0])
        except Exception as e:
            print(f"[策略1-iesdouyin] 失败: {e}")
    return None


async def strategy_mobile_share_page(aweme_id: str) -> Optional[dict]:
    """
    策略2: 移动端分享页面抓取
    访问移动端分享页，从页面内嵌JSON中提取数据
    不需要签名，模拟手机浏览器即可
    """
    url = f"https://www.douyin.com/video/{aweme_id}"
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if DOUYIN_COOKIE:
        headers["Cookie"] = DOUYIN_COOKIE

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
            html = resp.text
            return extract_from_html(html, aweme_id)
        except Exception as e:
            print(f"[策略2-移动端页面] 失败: {e}")
    return None


async def strategy_desktop_ssr(aweme_id: str) -> Optional[dict]:
    """
    策略3: 桌面端SSR数据提取
    抖音页面会在<script id="RENDER_DATA">中嵌入完整JSON
    """
    url = f"https://www.douyin.com/video/{aweme_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if DOUYIN_COOKIE:
        headers["Cookie"] = DOUYIN_COOKIE

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
            html = resp.text
            return extract_from_html(html, aweme_id)
        except Exception as e:
            print(f"[策略3-桌面SSR] 失败: {e}")
    return None


async def strategy_web_api_with_signature(aweme_id: str) -> Optional[dict]:
    """
    策略4: Web API + a_bogus签名 + curl_cffi TLS指纹模拟
    签名生成和HTTP请求全部在独立线程中执行，避免与uvicorn事件循环冲突
    """
    import asyncio
    from cookie_manager import get_cookies, invalidate

    cookie_data = await get_cookies()
    cookie_str = DOUYIN_COOKIE or cookie_data.get("cookie_str", "")
    if not cookie_str:
        return None

    def _do_signed_request():
        from abogus import ABogus
        from xbogus import XBogus
        from urllib.parse import quote, urlencode
        from curl_cffi.requests import Session

        fp = generate_device_fingerprint()
        ua = fp["user_agent"]
        chrome_ver = fp["chrome_version"]
        impersonate_ver = fp["impersonate_key"]

        params = {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "aweme_id": aweme_id, "pc_client_type": "1", "version_code": "190500",
            "version_name": "19.5.0", "cookie_enabled": "true",
            "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": chrome_ver,
            "browser_online": "true", "engine_name": "Blink",
            "engine_version": chrome_ver, "os_name": "Windows", "os_version": "10",
            "cpu_core_num": fp["cpu_core_num"], "device_memory": fp["device_memory"],
            "platform": "PC",
            "screen_width": fp["screen_width"], "screen_height": fp["screen_height"],
            "downlink": fp["downlink"], "effective_type": "4g",
            "round_trip_time": fp["round_trip_time"],
        }
        qs = urlencode(params)
        bogus = ABogus()
        ab = bogus.get_value(qs)
        xb = XBogus(user_agent=ua)
        _, xb_val, _ = xb.getXBogus(qs)
        url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?{qs}&a_bogus={quote(ab, safe='')}&X-Bogus={xb_val}"
        hdrs = {
            "User-Agent": ua,
            "Referer": f"https://www.douyin.com/video/{aweme_id}",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie_str,
        }
        with Session(impersonate=impersonate_ver) as s:
            return s.get(url, headers=hdrs, timeout=15)

    try:
        resp = await asyncio.to_thread(_do_signed_request)
        if not resp.text:
            invalidate()
            return None
        data = json.loads(resp.text)
        if data.get("status_code") == 0 and data.get("aweme_detail"):
            result = parse_aweme_detail(data["aweme_detail"])
            result["source"] = "web_api_signature"
            return result
    except ImportError:
        pass
    except Exception as e:
        import traceback
        print(f"[策略4-签名API] {e}")
        traceback.print_exc()
    return None


async def strategy_public_api(original_url: str, platform: str = "douyin") -> Optional[dict]:
    """
    公开API兜底 - 支持多平台
    """
    api_map = {
        "doubao": DOUBAO_APIS,
        "xiaohongshu": XHS_APIS,
        "kuaishou": KUAISHOU_APIS,
    }
    apis = api_map.get(platform, PUBLIC_APIS)
    async with httpx.AsyncClient(timeout=15) as client:
        for api_template in apis:
            api_url = api_template.format(url=urllib.parse.quote(original_url, safe=''))
            try:
                resp = await client.get(api_url)
                data = resp.json()
                if data.get("code") in (200, "200"):
                    result = data.get("data", {})
                    result["source"] = "public_api"
                    return result
            except Exception as e:
                print(f"[公开API-{platform}] 失败: {e}")
                continue
    return None


# === 数据提取 ===

def extract_from_html(html: str, aweme_id: str) -> Optional[dict]:
    """从HTML页面中提取视频数据"""
    # 方法1: RENDER_DATA (SSR嵌入的完整JSON)
    ssr_match = re.search(
        r'<script id="RENDER_DATA"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if ssr_match:
        try:
            decoded = urllib.parse.unquote(ssr_match.group(1))
            ssr_data = json.loads(decoded)
            for key, value in ssr_data.items():
                if not isinstance(value, dict):
                    continue
                # 不同版本的字段名
                detail = (
                    value.get("awemeDetail") or
                    value.get("aweme_detail") or
                    value.get("aweme", {}).get("detail")
                )
                if detail:
                    return parse_aweme_detail(detail)
                # 嵌套在loaderData中
                loader = value.get("loaderData", {})
                for lk, lv in loader.items():
                    if isinstance(lv, dict):
                        d = lv.get("awemeDetail") or lv.get("aweme_detail")
                        if d:
                            return parse_aweme_detail(d)
        except (json.JSONDecodeError, Exception) as e:
            print(f"  SSR解析异常: {e}")

    # 方法2: __NEXT_DATA__ (Next.js格式)
    next_match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if next_match:
        try:
            next_data = json.loads(next_match.group(1))
            props = next_data.get("props", {}).get("pageProps", {})
            detail = props.get("awemeDetail") or props.get("aweme_detail")
            if detail:
                return parse_aweme_detail(detail)
        except Exception:
            pass

    # 方法3: 正则提取video_id (最后手段)
    vid_match = re.search(r'"uri"\s*:\s*"(v[0-9a-f]{20,})"', html)
    if vid_match:
        vid = vid_match.group(1)
        title_match = re.search(r'"desc"\s*:\s*"([^"]*)"', html)
        title = title_match.group(1) if title_match else ""
        return {
            "type": "video",
            "title": title,
            "url": f"https://aweme.snssdk.com/aweme/v1/play/?video_id={vid}&ratio=1080p&line=0",
            "video_id": vid,
            "cover": "",
            "author": {"name": "", "id": ""},
            "source": "html_regex",
        }

    # 方法4: 从play_addr中提取
    play_match = re.search(r'"play_addr".*?"url_list"\s*:\s*\["([^"]+)"', html, re.DOTALL)
    if play_match:
        wm_url = play_match.group(1).replace('\\u002F', '/').replace('\\/', '/')
        nwm_url = wm_url.replace("playwm", "play")
        return {
            "type": "video",
            "title": "",
            "url": nwm_url,
            "cover": "",
            "author": {"name": "", "id": ""},
            "source": "html_play_addr",
        }

    return None


def parse_iesdouyin_item(item: dict) -> dict:
    """解析iesdouyin API返回的item数据"""
    video = item.get("video", {})
    author = item.get("author", {})

    play_addr = video.get("play_addr", {})
    uri = play_addr.get("uri", "")
    url_list = play_addr.get("url_list", [])

    nwm_url = ""

    # bit_rate 取最高码率（无水印）
    bit_rate = video.get("bit_rate", [])
    if bit_rate:
        sorted_rates = sorted(bit_rate, key=lambda x: x.get("bit_rate", 0), reverse=True)
        best_urls = sorted_rates[0].get("play_addr", {}).get("url_list", [])
        if best_urls:
            nwm_url = best_urls[0]
            if "playwm" in nwm_url:
                nwm_url = nwm_url.replace("playwm", "play")

    # video_id 构造
    if not nwm_url and uri:
        nwm_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
    elif not nwm_url and url_list:
        nwm_url = url_list[0].replace("playwm", "play")

    return {
        "type": "video",
        "title": item.get("desc", ""),
        "url": nwm_url,
        "video_id": uri,
        "cover": (video.get("cover", {}).get("url_list") or [""])[0],
        "duration": video.get("duration", 0),
        "author": {
            "name": author.get("nickname", ""),
            "id": author.get("unique_id") or author.get("short_id", ""),
            "avatar": (author.get("avatar_thumb", {}).get("url_list") or [""])[0],
        },
        "music": {
            "title": item.get("music", {}).get("title", ""),
            "url": (item.get("music", {}).get("play_url", {}).get("url_list") or [""])[0] if isinstance(item.get("music", {}).get("play_url"), dict) else item.get("music", {}).get("play_url", ""),
        },
        "source": "iesdouyin_api",
    }


def parse_aweme_detail(detail: dict) -> dict:
    """解析aweme_detail完整数据"""
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
                "avatar": (author.get("avatar_thumb", {}).get("url_list") or [""])[0],
            },
            "music": {
                "title": detail.get("music", {}).get("title", ""),
                "url": (detail.get("music", {}).get("play_url", {}).get("url_list") or [""])[0] if isinstance(detail.get("music", {}).get("play_url"), dict) else "",
            },
            "source": "ssr_detail",
        }

    # 视频
    play_addr = video.get("play_addr", {})
    uri = play_addr.get("uri", "")

    # 优先级: bit_rate最高码率(无水印) > play_addr > video_id构造
    nwm_url = ""

    # 1. bit_rate: 按码率数值降序排列，取最高的无水印链接
    bit_rate = video.get("bit_rate", [])
    if bit_rate:
        sorted_rates = sorted(bit_rate, key=lambda x: x.get("bit_rate", 0), reverse=True)
        best = sorted_rates[0]
        best_urls = best.get("play_addr", {}).get("url_list", [])
        if best_urls:
            nwm_url = best_urls[0]
            if "playwm" in nwm_url:
                nwm_url = nwm_url.replace("playwm", "play")

    # 2. play_addr直接取
    if not nwm_url and play_addr.get("url_list"):
        nwm_url = play_addr["url_list"][0].replace("playwm", "play")

    # 3. video_id构造
    if not nwm_url and uri:
        nwm_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"

    # 码率信息
    selected_bitrate = 0
    selected_quality = ""
    if bit_rate:
        sorted_rates = sorted(bit_rate, key=lambda x: x.get("bit_rate", 0), reverse=True)
        selected_bitrate = sorted_rates[0].get("bit_rate", 0)
        selected_quality = sorted_rates[0].get("gear_name", "")

    return {
        "type": "video",
        "title": detail.get("desc", ""),
        "url": nwm_url,
        "video_id": play_addr.get("uri", ""),
        "cover": (video.get("cover", {}).get("url_list") or [""])[0],
        "duration": video.get("duration", 0),
        "width": play_addr.get("width", 0),
        "height": play_addr.get("height", 0),
        "bitrate": selected_bitrate,
        "quality": selected_quality,
        "author": {
            "name": author.get("nickname", ""),
            "id": author.get("unique_id") or author.get("short_id", ""),
            "avatar": (author.get("avatar_thumb", {}).get("url_list") or [""])[0],
        },
        "music": {
            "title": detail.get("music", {}).get("title", ""),
            "url": (detail.get("music", {}).get("play_url", {}).get("url_list") or [""])[0] if isinstance(detail.get("music", {}).get("play_url"), dict) else "",
        },
        "source": "ssr_detail",
    }


# === API路由 ===

@app.get("/api/parse")
async def parse_video(url: str = Query(..., description="视频分享链接")):
    """统一解析入口"""
    original_url = url.strip()

    # 从分享文案中提取URL
    url_match = re.search(r'https?://[^\s<>"{}|\\^`\[\]]+', original_url)
    if url_match:
        original_url = url_match.group(0)

    platform = detect_platform(original_url)

    if platform == "doubao":
        result = await strategy_public_api(original_url, "doubao")
        if result:
            return {"code": 200, "msg": "解析成功", "data": result, "platform": "doubao"}
        return {"code": 500, "msg": "豆包解析失败", "data": None}

    if platform == "douyin":
        # 解析短链接
        resolved_url = original_url
        if "v.douyin.com" in original_url or "iesdouyin.com" in original_url:
            resolved_url = await resolve_short_url(original_url)

        aweme_id = extract_aweme_id(resolved_url) or extract_aweme_id(original_url)
        if not aweme_id:
            return {"code": 400, "msg": f"无法提取视频ID, URL: {resolved_url}", "data": None}

        # 策略优先级:
        # 1. 签名API+curl_cffi (毫秒级，需首次浏览器获取Cookie)
        # 2. 浏览器渲染拦截 (可靠但慢~10秒)
        # 3. 公开API兜底 (依赖第三方)
        strategies = [
            ("签名API直连", lambda: strategy_web_api_with_signature(aweme_id)),
        ]
        if BROWSER_AVAILABLE:
            strategies.append(("浏览器渲染", lambda: parse_douyin_by_browser(aweme_id)))
        strategies.append(("公开API兜底", lambda: strategy_public_api(original_url, "douyin")))

        for name, fn in strategies:
            print(f"  尝试: {name}...")
            result = await fn()
            if result:
                # 预解析snssdk重定向为CDN直链，加速后续下载
                if result.get("url") and "snssdk.com" in result["url"]:
                    resolved = await resolve_video_redirect(result["url"])
                    if resolved:
                        result["url"] = resolved
                return {"code": 200, "msg": f"解析成功({name})", "data": result, "platform": "douyin"}

        return {"code": 500, "msg": "所有策略均失败", "data": {"aweme_id": aweme_id}}

    if platform == "xiaohongshu":
        # 自建解析需要Cookie，优先尝试；无Cookie时走公开API
        result = await parse_xiaohongshu(original_url)
        if result:
            return {"code": 200, "msg": "解析成功(小红书自建)", "data": result, "platform": "xiaohongshu"}
        result = await strategy_public_api(original_url, "xiaohongshu")
        if result:
            return {"code": 200, "msg": "解析成功(公开API)", "data": result, "platform": "xiaohongshu"}
        return {"code": 500, "msg": "小红书解析失败", "data": None}

    if platform == "kuaishou":
        # 自建解析需要Cookie，优先尝试；无Cookie时走公开API
        result = await parse_kuaishou(original_url)
        if result:
            return {"code": 200, "msg": "解析成功(快手自建)", "data": result, "platform": "kuaishou"}
        result = await strategy_public_api(original_url, "kuaishou")
        if result:
            return {"code": 200, "msg": "解析成功(公开API)", "data": result, "platform": "kuaishou"}
        return {"code": 500, "msg": "快手解析失败", "data": None}

    # 其他平台走公开API
    result = await strategy_public_api(original_url, platform)
    if result:
        return {"code": 200, "msg": "解析成功", "data": result, "platform": platform}
    return {"code": 500, "msg": f"{platform} 解析失败", "data": None}


@app.get("/api/status")
async def status():
    from cookie_manager import get_cookies, COOKIE_FILE
    strats = ["iesdouyin_api", "mobile_page", "desktop_ssr", "signature_api"]
    if BROWSER_AVAILABLE:
        strats.append("browser_render")
    strats.append("public_api")

    cookie_status = "manual"
    if DOUYIN_COOKIE:
        cookie_status = "manual"
    elif COOKIE_FILE.exists():
        cookie_data = await get_cookies()
        has_ms = "msToken" in cookie_data.get("names", [])
        cookie_status = "auto_full" if has_ms else "auto_partial"
    else:
        cookie_status = "none"

    return {
        "status": "running",
        "cookie_status": cookie_status,
        "cookie_configured": cookie_status != "none",
        "browser_available": BROWSER_AVAILABLE,
        "strategies": strats,
        "public_apis": len(PUBLIC_APIS),
        "supported_platforms": ["douyin", "doubao", "kuaishou", "xiaohongshu", "bilibili", "weibo"],
    }


@app.get("/api/test")
async def test_strategies(aweme_id: str = Query(...)):
    """测试所有策略对指定视频ID的解析结果"""
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(status_code=404, detail="调试接口未启用")
    results = {}

    strategies = [
        ("iesdouyin_api", lambda: strategy_iesdouyin_api(aweme_id)),
        ("mobile_page", lambda: strategy_mobile_share_page(aweme_id)),
        ("desktop_ssr", lambda: strategy_desktop_ssr(aweme_id)),
        ("web_api_signature", lambda: strategy_web_api_with_signature(aweme_id)),
    ]

    for name, fn in strategies:
        try:
            r = await fn()
            results[name] = {"success": r is not None, "data": r}
        except Exception as e:
            results[name] = {"success": False, "error": str(e)}

    return results


@app.get("/api/debug")
async def debug_video_data(aweme_id: str = Query(...)):
    """调试: 返回API原始video对象，用于分析可用画质"""
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(status_code=404, detail="调试接口未启用")
    import asyncio
    from cookie_manager import get_cookies, invalidate

    cookie_data = await get_cookies()
    cookie_str = DOUYIN_COOKIE or cookie_data.get("cookie_str", "")
    if not cookie_str:
        return {"error": "no cookie"}

    def _do_request():
        from abogus import ABogus
        from xbogus import XBogus
        from urllib.parse import quote, urlencode
        from curl_cffi.requests import Session

        fp = generate_device_fingerprint()
        ua = fp["user_agent"]
        chrome_ver = fp["chrome_version"]
        impersonate_ver = fp["impersonate_key"]

        params = {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "aweme_id": aweme_id, "pc_client_type": "1", "version_code": "190500",
            "version_name": "19.5.0", "cookie_enabled": "true",
            "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": chrome_ver,
            "browser_online": "true", "engine_name": "Blink",
            "engine_version": chrome_ver, "os_name": "Windows", "os_version": "10",
            "cpu_core_num": fp["cpu_core_num"], "device_memory": fp["device_memory"],
            "platform": "PC",
            "screen_width": fp["screen_width"], "screen_height": fp["screen_height"],
            "downlink": fp["downlink"], "effective_type": "4g",
            "round_trip_time": fp["round_trip_time"],
        }
        qs = urlencode(params)
        bogus = ABogus()
        ab = bogus.get_value(qs)
        xb = XBogus(user_agent=ua)
        _, xb_val, _ = xb.getXBogus(qs)
        url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?{qs}&a_bogus={quote(ab, safe='')}&X-Bogus={xb_val}"
        hdrs = {
            "User-Agent": ua,
            "Referer": f"https://www.douyin.com/video/{aweme_id}",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie_str,
        }
        with Session(impersonate=impersonate_ver) as s:
            return s.get(url, headers=hdrs, timeout=15)

    try:
        resp = await asyncio.to_thread(_do_request)
        data = json.loads(resp.text)
        if data.get("status_code") != 0 or not data.get("aweme_detail"):
            return {"error": "api_failed", "status_code": data.get("status_code"), "raw": data}

        detail = data["aweme_detail"]
        video = detail.get("video", {})

        # 提取关键字段用于分析
        bit_rate = video.get("bit_rate", [])
        bit_rate_summary = []
        for br in bit_rate:
            urls = br.get("play_addr", {}).get("url_list", [])
            bit_rate_summary.append({
                "bit_rate": br.get("bit_rate"),
                "quality_type": br.get("quality_type"),
                "gear_name": br.get("gear_name"),
                "codec_type": br.get("codec_type"),
                "format": br.get("format"),
                "HDR_type": br.get("HDR_type", ""),
                "url_count": len(urls),
                "first_url_preview": urls[0][:120] + "..." if urls else "",
            })

        play_addr = video.get("play_addr", {})
        download_addr = video.get("download_addr", {})
        play_addr_h265 = video.get("play_addr_h265", {})

        return {
            "aweme_id": aweme_id,
            "desc": detail.get("desc", "")[:50],
            "duration": video.get("duration"),
            "video_keys": list(video.keys()),
            "play_addr": {
                "uri": play_addr.get("uri"),
                "width": play_addr.get("width"),
                "height": play_addr.get("height"),
                "url_count": len(play_addr.get("url_list", [])),
                "first_url_preview": (play_addr.get("url_list") or [""])[0][:120],
                "data_size": play_addr.get("data_size"),
            },
            "download_addr": {
                "url_count": len(download_addr.get("url_list", [])),
                "first_url_preview": (download_addr.get("url_list") or [""])[0][:120],
                "data_size": download_addr.get("data_size"),
            } if download_addr else None,
            "play_addr_h265": {
                "uri": play_addr_h265.get("uri"),
                "url_count": len(play_addr_h265.get("url_list", [])),
                "first_url_preview": (play_addr_h265.get("url_list") or [""])[0][:120],
                "data_size": play_addr_h265.get("data_size"),
                "width": play_addr_h265.get("width"),
                "height": play_addr_h265.get("height"),
            } if play_addr_h265 else None,
            "bit_rate_count": len(bit_rate),
            "bit_rate_details": bit_rate_summary,
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}


def detect_platform(url: str) -> str:
    lower = url.lower()
    if "doubao" in lower:
        return "doubao"
    if "douyin" in lower or "iesdouyin" in lower:
        return "douyin"
    if "kuaishou" in lower:
        return "kuaishou"
    if "xhs" in lower or "xiaohongshu" in lower:
        return "xiaohongshu"
    if "bilibili" in lower or "b23.tv" in lower:
        return "bilibili"
    if "weibo" in lower:
        return "weibo"
    if "toutiao" in lower:
        return "toutiao"
    return "unknown"


def _is_safe_download_url(url: str) -> bool:
    """Reject non-HTTP targets and hosts that resolve to private networks."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
            return False

        addresses = {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified)):
                return False
        return True
    except (OSError, ValueError):
        return False


@app.get("/api/download")
async def proxy_download(url: str = Query(..., description="视频/音频直链"), filename: str = Query("video.mp4", description="下载文件名")):
    """代理下载 - 流式转发，解决CDN防盗链"""
    if not _is_safe_download_url(url):
        return {"code": 400, "msg": "仅支持指向公网的 HTTP(S) 下载链接"}

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    client = httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10, read=300))
    resp = None
    current_url = url
    try:
        for _ in range(5):
            req = client.build_request("GET", current_url, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = resp.headers.get("location")
            await resp.aclose()
            if not location:
                break
            current_url = urllib.parse.urljoin(current_url, location)
            if not _is_safe_download_url(current_url):
                await client.aclose()
                return {"code": 400, "msg": "下载链接重定向到不允许的地址"}
        if resp is None:
            await client.aclose()
            return {"code": 502, "msg": "下载请求失败"}
    except httpx.HTTPError:
        if resp is not None:
            await resp.aclose()
        await client.aclose()
        return {"code": 502, "msg": "下载请求失败"}

    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        return {"code": resp.status_code, "msg": "下载失败，链接可能已过期"}

    content_type = resp.headers.get("content-type", "video/mp4")
    content_length = resp.headers.get("content-length", "")

    safe_filename = re.sub(r'[\r\n\\/:*?"<>|]+', "_", filename).strip()[:200] or "video.mp4"
    encoded_filename = urllib.parse.quote(safe_filename, safe="")
    resp_headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
    }
    if content_length:
        resp_headers["Content-Length"] = content_length

    async def stream_body():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(stream_body(), media_type=content_type, headers=resp_headers)


@app.get("/", response_class=HTMLResponse)
async def index():
    with (Path(__file__).resolve().parent / "index.html").open("r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("  短视频无水印解析服务 v1.1")
    print("  访问: http://localhost:8848")
    print("  API文档: http://localhost:8848/docs")
    print("  策略测试: http://localhost:8848/api/test?aweme_id=xxx")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8848)
