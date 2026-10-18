"""抖音详情的统一响应格式；不发起网络请求。"""

import json
import re
from typing import Optional
from urllib.parse import urlencode


def build_original_video_url(video_id: str) -> str:
    if not isinstance(video_id, str) or not video_id:
        return ""
    query = urlencode({"video_id": video_id, "ratio": "default", "line": "0"})
    return f"https://aweme.snssdk.com/aweme/v1/play/?{query}"


def _play_urls(address: dict) -> list:
    return [
        url.replace("playwm", "play")
        for url in address.get("url_list") or []
        if isinstance(url, str) and url.startswith(("https://", "http://"))
    ]


def parse_aweme_detail(detail: dict) -> dict:
    """保留全部可用转码档，原画是否可用由调用方探测。"""
    video = detail.get("video") or {}
    author = detail.get("author") or {}
    music = detail.get("music") or {}
    music_address = music.get("play_url") or {}
    music_url = (
        (music_address.get("url_list") or [""])[0]
        if isinstance(music_address, dict) else music_address
    )
    result = {
        "type": "video",
        "title": detail.get("desc", ""),
        "cover": ((video.get("cover") or {}).get("url_list") or [""])[0],
        "author": {
            "name": author.get("nickname", ""),
            "id": author.get("unique_id") or author.get("short_id", ""),
            "avatar": ((author.get("avatar_thumb") or {}).get("url_list") or [""])[0],
        },
        "music": {"title": music.get("title", ""), "url": music_url},
        "source": "ssr_detail",
    }
    images = detail.get("images") or []
    if images:
        result.update({
            "type": "image",
            "images": [img["url_list"][-1] for img in images if img.get("url_list")],
        })
        return result

    backups = []
    for rate in video.get("bit_rate") or []:
        address = rate.get("play_addr") or {}
        urls = _play_urls(address)
        if not urls:
            continue
        backups.append({
            "url": urls[0],
            "url_list": urls,
            "quality": rate.get("gear_name") or "",
            "bit_rate": rate.get("bit_rate") or 0,
            "width": address.get("width") or 0,
            "height": address.get("height") or 0,
            "data_size": address.get("data_size") or 0,
            "fps": rate.get("FPS") or 0,
            "codec": "h265" if rate.get("is_h265") or rate.get("is_bytevc1") else "h264",
            "is_h265": rate.get("is_h265") or 0,
            "is_bytevc1": rate.get("is_bytevc1") or 0,
            "format": rate.get("format") or video.get("format") or "mp4",
        })
    backups.sort(key=lambda rate: rate["bit_rate"], reverse=True)

    address = video.get("play_addr") or {}
    video_id = address.get("uri") or ""
    original_url = build_original_video_url(video_id)
    urls = _play_urls(address)
    best = backups[0] if backups else {}
    result.update({
        "url": best.get("url") or (urls[0] if urls else original_url),
        "video_id": video_id,
        "original_url": original_url,
        "duration": video.get("duration") or 0,
        "width": best.get("width", address.get("width") or video.get("width") or 0),
        "height": best.get("height", address.get("height") or video.get("height") or 0),
        "bitrate": best.get("bit_rate", 0),
        "quality": best.get("quality", ""),
        "data_size": best.get("data_size", address.get("data_size") or 0),
        "fps": best.get("fps", 0),
        "video_backup": backups,
    })
    return result


def parse_iesdouyin_item(item: dict) -> dict:
    result = parse_aweme_detail(item)
    result["source"] = "iesdouyin_api"
    return result


def extract_play_count(html: str, aweme_id: str) -> Optional[int]:
    """只读取指定作品的精选页 SSR 计数，不匹配推荐列表。"""
    match = re.search(
        r'<script\b[^>]*>\s*window\._SSR_DATA\s*=\s*(.*?)\s*</script>',
        html, re.DOTALL,
    )
    if not match:
        return None
    try:
        payload = json.loads(match.group(1).strip().rstrip(";"))
        result = payload["data"]["storeState"]["detail"]["videoData"]["result"]
        if str(result.get("gid")) != aweme_id:
            return None
        count = result.get("play_count")
        if type(count) is int and count >= 0:
            return count
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None
