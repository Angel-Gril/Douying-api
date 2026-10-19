"""
短视频无水印解析 - 本地服务器
多策略解析：自建解析优先，公开API兜底
"""

import re
import asyncio
import json
import ipaddress
import os
import socket
import time
import random
import urllib.parse
from pathlib import Path
from typing import Optional, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    from browser_parser import parse_douyin_by_browser
    BROWSER_AVAILABLE = True
except ImportError:
    BROWSER_AVAILABLE = False

from platform_parsers import parse_xiaohongshu, parse_kuaishou
from media_downloads import MediaError, download_response, is_safe_media_url
from media_downloads import create_download_job, download_job_status, cancel_download_job, remove_download_job, DOWNLOAD_JOBS, ffmpeg_path
from bilibili_parser import parse_bilibili
from public_api import parse_public_url, LEGACY_PATHS
from douyin_parser import (
    build_original_video_url, extract_aweme_id, extract_play_count, parse_aweme_detail, parse_iesdouyin_item,
)

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

# 抖音 Cookie 仅供签名策略使用；优先尝试免 Cookie 详情。
DOUYIN_COOKIE = os.getenv("DOUYIN_COOKIE", "").strip()
ENABLE_DEBUG_ENDPOINTS = os.getenv("ENABLE_DEBUG_ENDPOINTS", "0").lower() in {"1", "true", "yes"}

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
    from curl_cffi.requests.impersonate import BrowserType
    supported = {profile.value for profile in BrowserType}
    candidates = [(version, profile) for version, profile in _CHROME_VERSIONS if profile in supported]
    chrome_ver, impersonate_key = random.choice(candidates) if candidates else ("124.0.0.0", "chrome")
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

async def probe_video_url(url: str) -> Optional[dict]:
    """只用 HEAD 验证媒体和大小；每一跳检查公网地址，总耗时至多 8 秒。"""
    if not url:
        return None

    async def probe():
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}
        current = url
        async with httpx.AsyncClient(follow_redirects=False, timeout=5) as client:
            for _ in range(5):
                if not await asyncio.to_thread(_is_safe_download_url, current):
                    return None
                resp = await client.head(current, headers=headers)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    current = urllib.parse.urljoin(current, location)
                    continue
                content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if resp.status_code not in (200, 206) or not (
                    content_type.startswith("video/") or content_type == "application/octet-stream"
                ):
                    return None
                size = int(resp.headers.get("content-length", "0"))
                if size < 0 or resp.headers.get("content-length") == "0":
                    return None
                return {"url": current, "data_size": size}
        return None

    try:
        return await asyncio.wait_for(probe(), timeout=8)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
        return None


async def resolve_video_redirect(url: str) -> Optional[str]:
    media = await probe_video_url(url)
    return media["url"] if media else None


async def fetch_douyin_play_count(aweme_id: str) -> Optional[int]:
    """精选移动页的尽力查询；不获取 Cookie，失败不阻断解析。"""
    def fetch():
        from curl_cffi.requests import Session

        url = f"https://jingxuan.douyin.com/m/video/{aweme_id}?app=yumme"
        headers = {
            "User-Agent": MOBILE_UA,
            "Origin": "https://open.douyin.com",
            "Referer": "https://open.douyin.com/",
        }
        with Session(impersonate="chrome124") as session:
            resp = session.get(url, headers=headers, timeout=8, allow_redirects=False)
        return extract_play_count(resp.text, aweme_id) if resp.status_code == 200 else None

    try:
        return await asyncio.to_thread(fetch)
    except Exception:
        return None


async def enrich_douyin_video(result: dict, aweme_id: str) -> bool:
    """补充原画和播放量；只有未验证的构造入口时返回 False，继续后备策略。"""
    if result.get("type") in {"image", "live"}:
        return True
    original_url = build_original_video_url(result.get("video_id"))
    probe_url = original_url
    if not probe_url:
        host = urllib.parse.urlparse(result.get("url") or "").hostname or ""
        if host == "snssdk.com" or host.endswith(".snssdk.com"):
            probe_url = result["url"]
    media, count = await asyncio.gather(
        probe_video_url(probe_url), fetch_douyin_play_count(aweme_id),
    )
    if original_url:
        result["original_url"] = original_url
    if media:
        result.update(media)
        if original_url:
            # HEAD 没有宽高/码率，不能把转码档的参数冒充原画参数。
            result.update(quality="original", width=0, height=0, bitrate=0, fps=0)
    if count is not None:
        result["play_count"] = count
    if original_url and result.get("url") == original_url and not media:
        return False
    return True


async def resolve_short_url(url: str) -> str:
    """只在抖音站点之间跟随分享跳转，不把分享地址变成任意请求入口。"""
    original = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=8) as client:
        try:
            for _ in range(6):
                if detect_platform(url) != "douyin":
                    return original
                response = await client.get(url, headers={"User-Agent": MOBILE_UA})
                if response.status_code not in (301, 302, 303, 307, 308):
                    return url
                location = response.headers.get("location")
                if not location:
                    return original
                url = urllib.parse.urljoin(url, location)
        except httpx.HTTPError:
            pass
    return original


# === 解析策略 ===

async def strategy_web_api_open(aweme_id: str) -> Optional[dict]:
    """带开放平台来源头请求详情，不发送 Cookie 或签名。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://open.douyin.com",
        "Referer": "https://open.douyin.com/",
        "Accept": "application/json, text/plain, */*",
    }
    params = {"aweme_id": aweme_id, "aid": "6383", "device_platform": "webapp"}
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        try:
            resp = await client.get(
                "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                params=params, headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            detail = data.get("aweme_detail") if data.get("status_code") == 0 else None
            if not isinstance(detail, dict) or str(detail.get("aweme_id")) != aweme_id:
                return None
            result = parse_aweme_detail(detail)
            if not (result.get("url") or result.get("images")):
                return None
            result["source"] = "web_api_open"
            return result
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None


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


async def _request_signed_douyin_detail(aweme_id: str) -> Optional[dict]:
    """
    策略4: Web API + a_bogus签名 + curl_cffi TLS指纹模拟
    签名生成和HTTP请求全部在独立线程中执行，避免与uvicorn事件循环冲突
    """
    import asyncio
    from cookie_manager import get_cookies, invalidate

    cookie_data = {} if DOUYIN_COOKIE else await get_cookies()
    cookie_str = DOUYIN_COOKIE or cookie_data.get("cookie_str", "")
    if not cookie_str:
        return None

    def _do_signed_request():
        from abogus import ABogus
        from xbogus import XBogus
        from urllib.parse import quote, urlencode
        from curl_cffi.requests import Session

        fp = generate_device_fingerprint()
        captured = cookie_data.get("fingerprint") or {}
        if captured.get("user_agent"):
            fp["user_agent"] = captured["user_agent"]
            captured_version = re.search(r"Chrome/([\d.]+)", fp["user_agent"])
            if captured_version:
                fp["chrome_version"] = captured_version.group(1)
                from curl_cffi.requests.impersonate import BrowserType
                profiles = [p.value for p in BrowserType if re.fullmatch(r"chrome\d+", p.value)]
                if profiles:
                    major = int(captured_version.group(1).split(".")[0])
                    fp["impersonate_key"] = min(profiles, key=lambda value: abs(int(value[6:]) - major))
        if isinstance(captured.get("viewport"), dict):
            fp["screen_width"] = str(captured["viewport"].get("width") or fp["screen_width"])
            fp["screen_height"] = str(captured["viewport"].get("height") or fp["screen_height"])
        ua = fp["user_agent"]
        chrome_ver = fp["chrome_version"]
        impersonate_ver = fp["impersonate_key"]

        params = {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "aweme_id": aweme_id, "request_source": "600", "origin_type": "video_page",
            "pc_client_type": "1", "version_code": "190500", "support_h265": "1", "support_dash": "0",
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
        cookie_values = dict(part.strip().split("=", 1) for part in cookie_str.split(";") if "=" in part)
        for cookie_name, param_names in (("UIFID", ("uifid",)), ("s_v_web_id", ("verifyFp", "fp")), ("msToken", ("msToken",))):
            if cookie_values.get(cookie_name):
                for param_name in param_names:
                    params[param_name] = cookie_values[cookie_name]
        qs = urlencode(params)
        width, height = fp["screen_width"], fp["screen_height"]
        browser_info = f"{width}|{height}|{width}|{height}|0|0|0|0|{width}|{height}|{width}|{height}|{width}|{height}|24|24|Win32"
        bogus = ABogus(platform="Win32", user_agent=ua, browser_info=browser_info)
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
            return s.get(url, headers=hdrs, timeout=15, allow_redirects=False)

    try:
        resp = await asyncio.to_thread(_do_signed_request)
        if not resp.text:
            if not DOUYIN_COOKIE:
                invalidate()
            return None
        data = json.loads(resp.text)
        if data.get("status_code") == 0 and data.get("aweme_detail"):
            return data["aweme_detail"]
    except ImportError:
        pass
    except Exception as e:
        print(f"[签名API] 请求失败: {type(e).__name__}")
    return None


async def strategy_web_api_with_signature(aweme_id: str) -> Optional[dict]:
    detail = await _request_signed_douyin_detail(aweme_id)
    if detail is None:
        return None
    result = parse_aweme_detail(detail)
    result["source"] = "web_api_signature"
    return result


async def strategy_public_api(original_url: str, platform: str = "douyin") -> Optional[dict]:
    return await parse_public_url(original_url, platform)


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
    if platform == "unknown":
        return {"code": 400, "msg": "链接格式无效或暂不支持该平台", "data": None}

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
        # 1. 免 Cookie 详情；2. 签名 API；3. 浏览器；4. 第三方 API。
        strategies = [
            ("免Cookie详情API", lambda: strategy_web_api_open(aweme_id)),
            ("签名API直连", lambda: strategy_web_api_with_signature(aweme_id)),
        ]
        if BROWSER_AVAILABLE:
            strategies.append(("浏览器渲染", lambda: parse_douyin_by_browser(aweme_id)))
        strategies.append(("公开API兜底", lambda: strategy_public_api(original_url, "douyin")))

        for name, fn in strategies:
            print(f"  尝试: {name}...")
            result = await fn()
            if result:
                if not await enrich_douyin_video(result, aweme_id):
                    continue
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

    if platform == "bilibili":
        result = await parse_bilibili(original_url)
        if result:
            return {"code": 200, "msg": "解析成功(B站原生接口)", "data": result, "platform": platform}

    # 其他已识别平台使用各自的公开供方路由。
    result = await strategy_public_api(original_url, platform)
    if result:
        return {"code": 200, "msg": "解析成功", "data": result, "platform": platform}
    return {"code": 500, "msg": f"{platform} 解析失败", "data": None}


@app.get("/api/status")
async def status():
    from cookie_manager import COOKIE_FILE, COOKIE_TTL
    strats = ["web_api_open", "signature_api"]
    if BROWSER_AVAILABLE:
        strats.append("browser_render")
    strats.append("public_api")

    cookie_status = "manual"
    if DOUYIN_COOKIE:
        cookie_status = "manual"
    elif COOKIE_FILE.exists():
        try:
            cookie_data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if time.time() - cookie_data.get("time", 0) >= COOKIE_TTL:
                cookie_status = "expired"
            else:
                has_ms = "msToken" in cookie_data.get("names", [])
                cookie_status = "auto_full" if has_ms else "auto_partial"
        except (OSError, ValueError, TypeError, AttributeError):
            cookie_status = "none"
    else:
        cookie_status = "none"

    return {
        "status": "running",
        "cookie_status": cookie_status,
        "cookie_configured": cookie_status not in {"none", "expired"},
        "browser_available": BROWSER_AVAILABLE,
        "strategies": strats,
        "public_apis": len(LEGACY_PATHS),
        "supported_platforms": list(LEGACY_PATHS),
        "download_features": {"range": True, "progress": True, "cancel": True, "zip": True, "merge": True, "hls": "unencrypted_vod"},
    }


@app.get("/api/test")
async def test_strategies(aweme_id: str = Query(...)):
    """测试所有策略对指定视频ID的解析结果"""
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(status_code=404, detail="调试接口未启用")
    results = {}

    strategies = [
        ("web_api_open", lambda: strategy_web_api_open(aweme_id)),
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
    detail = await _request_signed_douyin_detail(aweme_id)
    if detail is None:
        return {"error": "api_failed"}
    try:
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
    except (TypeError, AttributeError, ValueError):
        return {"error": "invalid_detail"}


def detect_platform(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return "unknown"
    except ValueError:
        return "unknown"
    domains = {
        "douyin": ("douyin.com", "iesdouyin.com"),
        "doubao": ("doubao.com", "doubao.cn", "dola.com"),
        "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhs.com"),
        "kuaishou": ("kuaishou.com", "chenzhongtech.com"),
        "bilibili": ("bilibili.com", "b23.tv"),
        "weibo": ("weibo.com", "weibo.cn"),
        "toutiao": ("toutiao.com",),
        "pipixia": ("pipix.com", "pipixia.com"),
        "pipigx": ("pipigx.com", "ippzone.com"),
        "zuiyou": ("xiaochuankeji.cn", "izuiyou.com"),
    }
    for platform, allowed in domains.items():
        if any(host == domain or host.endswith("." + domain) for domain in allowed):
            return platform
    return "unknown"


def _is_safe_download_url(url: str) -> bool:
    return is_safe_media_url(url)


class DownloadJobRequest(BaseModel):
    mode: Literal["zip", "mux", "concat", "hls"]
    url: str = Field("", max_length=8192)
    audio_url: str = Field("", max_length=8192)
    urls: list[str] = Field(default_factory=list, max_length=100)
    filename: str = Field("media", max_length=180)

    class Config:
        extra = "forbid"


def _download_error(error):
    return JSONResponse(status_code=error.code, content={"code": error.code, "msg": str(error), "data": None})


@app.post("/api/download/jobs")
async def start_download_job(payload: DownloadJobRequest):
    try:
        data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        return {"code": 200, "data": create_download_job(data)}
    except MediaError as exc:
        return _download_error(exc)


@app.get("/api/download/jobs/{job_id}")
async def get_download_job(job_id: str):
    try:
        return {"code": 200, "data": download_job_status(job_id)}
    except MediaError as exc:
        return _download_error(exc)


@app.delete("/api/download/jobs/{job_id}")
async def stop_download_job(job_id: str):
    try:
        return {"code": 200, "data": await cancel_download_job(job_id)}
    except MediaError as exc:
        return _download_error(exc)


@app.get("/api/download/jobs/{job_id}/file")
async def get_download_job_file(job_id: str):
    try:
        status = download_job_status(job_id)
        if status["status"] != "completed":
            raise MediaError("文件尚未准备好", 409)
        job = DOWNLOAD_JOBS[job_id]
        return FileResponse(job["output"], filename=job["filename"], media_type="application/zip" if job["output"].suffix == ".zip" else "video/mp4", headers={"Cache-Control": "no-store"})
    except MediaError as exc:
        return _download_error(exc)


@app.on_event("shutdown")
async def close_download_jobs():
    for job_id in list(DOWNLOAD_JOBS):
        await cancel_download_job(job_id)
        remove_download_job(job_id)


@app.get("/api/download")
async def proxy_download(url: str = Query(..., description="媒体直链"), filename: str = Query("video.mp4", description="下载文件名"), request: Request = None):
    """平台 Referer、Range、完整性与公网跳转校验。"""
    try:
        return await download_response(url, filename, request.headers.get("range") if request else None)
    except MediaError as exc:
        return {"code": exc.code, "msg": str(exc)}
    except httpx.HTTPError:
        return {"code": 502, "msg": "媒体下载请求失败"}


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
