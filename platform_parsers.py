"""
小红书 & 快手 原生解析模块
- 小红书: 页面HTML提取 __INITIAL_STATE__ (无需签名)
- 快手: 移动端REST API (最小化认证)
"""

import re
import json
import random
import urllib.parse
from typing import Optional

import httpx

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"


# ============================================================
# 小红书 (Xiaohongshu / XHS)
# ============================================================

def _extract_xhs_note_id(url: str) -> Optional[str]:
    patterns = [
        r'/explore/([a-f0-9]{24})',
        r'/discovery/item/([a-f0-9]{24})',
        r'/note/([a-f0-9]{24})',
        r'source_note_id=([a-f0-9]{24})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _extract_xsec_token(url: str) -> Optional[str]:
    m = re.search(r'xsec_token=([^&]+)', url)
    return urllib.parse.unquote(m.group(1)) if m else None


async def parse_xiaohongshu(url: str) -> Optional[dict]:
    """
    小红书解析: 解析短链接 -> 获取页面HTML -> 提取__INITIAL_STATE__
    支持图文笔记和视频笔记
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        # 解析短链接
        if "xhslink.com" in url:
            try:
                resp = await client.get(url, headers=headers)
                url = str(resp.url)
            except Exception:
                pass

        note_id = _extract_xhs_note_id(url)
        if not note_id:
            return None

        xsec_token = _extract_xsec_token(url)
        page_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec_token:
            page_url += f"?xsec_token={urllib.parse.quote(xsec_token)}"

        try:
            resp = await client.get(page_url, headers=headers)
            html = resp.text
        except Exception:
            return None

    return _parse_xhs_html(html, note_id)


def _parse_xhs_html(html: str, note_id: str) -> Optional[dict]:
    """从HTML中提取__INITIAL_STATE__数据"""
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', html, re.DOTALL)
    if not match:
        return None

    try:
        raw = match.group(1)
        raw = raw.replace("undefined", "null")
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    note_map = data.get("note", {}).get("noteDetailMap", {})
    note_data = note_map.get(note_id, {}).get("note")
    if not note_data:
        for key, val in note_map.items():
            if isinstance(val, dict) and val.get("note"):
                note_data = val["note"]
                break

    if not note_data:
        return None

    note_type = note_data.get("type", "normal")
    title = note_data.get("title", "")
    desc = note_data.get("desc", "")
    user = note_data.get("user", {})

    result = {
        "title": title or desc,
        "desc": desc,
        "author": {
            "name": user.get("nickname", ""),
            "id": user.get("userId", ""),
            "avatar": user.get("avatar", ""),
        },
        "source": "xhs_native",
    }

    if note_type == "video":
        video = note_data.get("video", {})
        media = video.get("media", {})
        stream = media.get("stream", {})
        h264 = stream.get("h264", [])
        video_url = ""
        if h264:
            video_url = h264[0].get("masterUrl", "") or h264[0].get("master_url", "")
        if not video_url:
            h265 = stream.get("h265", [])
            if h265:
                video_url = h265[0].get("masterUrl", "") or h265[0].get("master_url", "")
        # 备用: consumer字段
        if not video_url:
            consumer = video.get("consumer", {})
            origin = consumer.get("originVideoKey", "")
            if origin:
                video_url = f"https://sns-video-bd.xhscdn.com/{origin}"

        cover = ""
        image_list = note_data.get("imageList", [])
        if image_list:
            cover = image_list[0].get("urlDefault", "") or image_list[0].get("url", "")

        result["type"] = "video"
        result["url"] = video_url
        result["cover"] = cover
    else:
        image_list = note_data.get("imageList", [])
        images = []
        for img in image_list:
            img_url = img.get("urlDefault", "") or img.get("url", "")
            if img_url:
                if not img_url.startswith("http"):
                    img_url = "https://sns-webpic-qc.xhscdn.com/" + img_url
                images.append(img_url)

        result["type"] = "image"
        result["images"] = images
        result["cover"] = images[0] if images else ""

    return result


# ============================================================
# 快手 (Kuaishou)
# ============================================================

def _extract_kuaishou_photo_id(url: str) -> Optional[str]:
    patterns = [
        r'/short-video/([a-zA-Z0-9]+)',
        r'/fw/photo/([a-zA-Z0-9]+)',
        r'/fw/long-video/([a-zA-Z0-9]+)',
        r'photoId=([a-zA-Z0-9]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


async def parse_kuaishou(url: str) -> Optional[dict]:
    """
    快手解析: 解析短链接 -> 移动端REST API获取视频信息
    """
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        # 解析短链接
        if "v.kuaishou.com" in url or "kuaishou.com/f/" in url:
            try:
                resp = await client.get(url, headers=headers)
                url = str(resp.url)
            except Exception:
                pass

        photo_id = _extract_kuaishou_photo_id(url)
        if not photo_id:
            return None

        # 策略1: 移动端REST API
        result = await _kuaishou_mobile_api(client, photo_id)
        if result:
            return result

        # 策略2: 页面HTML提取
        result = await _kuaishou_page_extract(client, photo_id)
        if result:
            return result

    return None


async def _kuaishou_mobile_api(client: httpx.AsyncClient, photo_id: str) -> Optional[dict]:
    """快手移动端REST API"""
    did = f"web_{random.randint(100000000, 999999999)}"
    api_url = "https://v.m.chenzhongtech.com/rest/wd/photo/info"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}",
        "Cookie": f"did={did}",
        "User-Agent": MOBILE_UA,
    }
    body = {"photoId": photo_id, "isLongVideo": False}

    try:
        resp = await client.post(api_url, json=body, headers=headers)
        data = resp.json()
    except Exception:
        return None

    if data.get("result") != 1:
        return None

    photo = data.get("photo", {})
    if not photo:
        return None

    caption = photo.get("caption", "")
    cover = photo.get("coverUrl", "")
    author_name = photo.get("userName", "")
    author_id = photo.get("userId", "")
    head_url = photo.get("headUrl", "")

    # 视频
    main_urls = photo.get("mainMvUrls", [])
    video_url = ""
    if main_urls:
        video_url = main_urls[0].get("url", "")

    # 图集
    atlas = data.get("atlas", {})
    if atlas and atlas.get("list"):
        cdn_list = atlas.get("cdn", ["p1.kwaicdn.com"])
        cdn = cdn_list[0] if cdn_list else "p1.kwaicdn.com"
        images = [f"https://{cdn}{path}" for path in atlas["list"]]
        return {
            "type": "image",
            "title": caption,
            "images": images,
            "cover": cover,
            "author": {"name": author_name, "id": author_id, "avatar": head_url},
            "source": "kuaishou_native",
        }

    if video_url:
        return {
            "type": "video",
            "title": caption,
            "url": video_url,
            "cover": cover,
            "author": {"name": author_name, "id": author_id, "avatar": head_url},
            "source": "kuaishou_native",
        }

    return None


async def _kuaishou_page_extract(client: httpx.AsyncClient, photo_id: str) -> Optional[dict]:
    """从快手网页提取数据"""
    page_url = f"https://www.kuaishou.com/short-video/{photo_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        resp = await client.get(page_url, headers=headers)
        html = resp.text
    except Exception:
        return None

    # __NEXT_DATA__ 提取
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if match:
        try:
            next_data = json.loads(match.group(1))
            video_detail = next_data.get("props", {}).get("pageProps", {}).get("videoDetail", {})
            if video_detail:
                return _parse_kuaishou_detail(video_detail)
        except (json.JSONDecodeError, ValueError):
            pass

    # __APOLLO_STATE__ 提取
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*({.*?});?\s*</script>', html, re.DOTALL)
    if match:
        try:
            apollo = json.loads(match.group(1))
            for key, val in apollo.items():
                if isinstance(val, dict) and val.get("caption") and val.get("photoUrl"):
                    return {
                        "type": "video",
                        "title": val.get("caption", ""),
                        "url": val.get("photoUrl", ""),
                        "cover": val.get("coverUrl", ""),
                        "author": {"name": val.get("name", ""), "id": "", "avatar": ""},
                        "source": "kuaishou_native",
                    }
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _parse_kuaishou_detail(detail: dict) -> Optional[dict]:
    """解析快手videoDetail数据"""
    photo = detail.get("photo", {})
    author = detail.get("author", {})

    video_url = photo.get("photoUrl", "")
    if not video_url:
        video_resource = photo.get("videoResource", {})
        h264 = video_resource.get("h264", {})
        adapt = h264.get("adaptationSet", [])
        if adapt:
            reps = adapt[0].get("representation", [])
            if reps:
                video_url = reps[0].get("url", "")

    if not video_url:
        return None

    return {
        "type": "video",
        "title": photo.get("caption", ""),
        "url": video_url,
        "cover": photo.get("coverUrl", ""),
        "author": {
            "name": author.get("name", ""),
            "id": author.get("id", ""),
            "avatar": author.get("headerUrl", ""),
        },
        "source": "kuaishou_native",
    }
