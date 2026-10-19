"""Bilibili's public view/playurl APIs, with optional operator supplied cookies."""

import os
import re
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
API_ROOT = "https://api.bilibili.com"
QUALITY_NAMES = {
    6: "240P 极速", 16: "360P 流畅", 32: "480P 清晰", 64: "720P 高清",
    74: "720P60 高帧率", 80: "1080P 高清", 112: "1080P+ 高码率",
    116: "1080P60 高帧率", 120: "4K 超清", 125: "HDR 真彩", 126: "杜比视界", 127: "8K 超高清",
}


def _number(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _site_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 80, 443}
            and any(host == domain or host.endswith("." + domain) for domain in ("bilibili.com", "b23.tv"))
        )
    except ValueError:
        return False


def _media_urls(item: dict) -> list:
    primary = item.get("baseUrl") or item.get("base_url") or item.get("url")
    backup = item.get("backupUrl") or item.get("backup_url") or []
    if not isinstance(backup, list):
        backup = []
    urls = []
    for value in [primary, *backup]:
        if isinstance(value, str) and urlsplit(value).scheme in {"http", "https"} and urlsplit(value).hostname and value not in urls:
            urls.append(value)
    # 使用上游真实返回的普通 HTTPS CDN，避免默认落到易失效的高端口 P2P 节点。
    def priority(value):
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        stable = host.endswith((".bilivideo.com", ".bilivideo.cn", ".acgvideo.com")) and ".mcdn." not in host and parsed.port in (None, 443)
        return (not stable, parsed.scheme != "https", ".mcdn." in host)
    urls.sort(key=priority)
    return urls


async def _api(client: httpx.AsyncClient, path: str, params: dict, headers: dict) -> Optional[dict]:
    try:
        response = await client.get(API_ROOT + path, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        return None
    return payload["data"]


async def _resolve_video_url(client: httpx.AsyncClient, url: str) -> Optional[str]:
    for _ in range(6):
        if not _site_url(url):
            return None
        host = urlsplit(url).hostname or ""
        if host == "bilibili.com" or host.endswith(".bilibili.com"):
            return url
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
        if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("location"):
            return None
        url = urljoin(url, response.headers["location"])
    return None


def _progressive(data: dict) -> list:
    durl = data.get("durl") or []
    if not isinstance(durl, list) or not durl or not all(isinstance(item, dict) for item in durl):
        return []
    durl = sorted(durl, key=lambda item: _number(item.get("order")))
    segment_urls = [_media_urls(item) for item in durl]
    if not all(segment_urls):
        return []
    urls = segment_urls[0]
    quality_id = _number(data.get("quality"))
    upstream_format = str(data.get("format") or "")
    variant = {
        "url": urls[0], "url_list": urls,
        "quality": QUALITY_NAMES.get(quality_id, str(quality_id)),
        "quality_id": quality_id,
        "width": 0, "height": 0, "data_size": sum(_number(item.get("size")) for item in durl) if all(_number(item.get("size")) for item in durl) else 0,
        "fps": 0, "bit_rate": 0, "codec": "",
        "format": "mp4" if upstream_format.startswith("mp4") else "flv" if upstream_format.startswith("flv") else upstream_format,
    }
    if len(durl) > 1:
        variant.update({"segments": [urls[0] for urls in segment_urls], "requires_concat": True})
    return [variant]


def _fps(value) -> float:
    try:
        numerator, separator, denominator = str(value).partition("/")
        rate = float(numerator) / float(denominator) if separator else float(numerator)
        return rate if 0 < rate < 1000 else 0
    except (ValueError, TypeError, ZeroDivisionError):
        return 0


def _dash(data: dict) -> tuple:
    dash = data.get("dash") or {}
    if not isinstance(dash, dict):
        return [], []
    audios = []
    audio_items = list(dash.get("audio") or [])
    for kind in ("dolby", "flac"):
        extra = dash.get(kind)
        extra = extra.get("audio") if isinstance(extra, dict) else None
        if isinstance(extra, dict):
            audio_items.append(extra)
        elif isinstance(extra, list):
            audio_items.extend(extra)
    for item in audio_items:
        if not isinstance(item, dict):
            continue
        urls = _media_urls(item)
        if urls and not any(audio["url"] == urls[0] for audio in audios):
            audios.append({
                "url": urls[0], "url_list": urls, "quality": str(item.get("id") or ""),
                "data_size": _number(item.get("size")), "bit_rate": _number(item.get("bandwidth")),
                "codec": str(item.get("codecs") or ""), "format": "m4a",
            })
    audios.sort(key=lambda item: (item["codec"].startswith("mp4a"), item["bit_rate"]), reverse=True)
    if not audios:
        return [], []
    videos = []
    for item in dash.get("video") or []:
        if not isinstance(item, dict):
            continue
        urls = _media_urls(item)
        if not urls:
            continue
        quality_id = _number(item.get("id"))
        videos.append({
            "url": urls[0], "url_list": urls,
            "quality": QUALITY_NAMES.get(quality_id, str(quality_id)), "quality_id": quality_id,
            "width": _number(item.get("width")), "height": _number(item.get("height")),
            "data_size": _number(item.get("size")), "fps": _fps(item.get("frameRate") or item.get("frame_rate")),
            "bit_rate": _number(item.get("bandwidth")), "codec": str(item.get("codecs") or ""),
            "format": "mp4", "audio_url": audios[0]["url"], "requires_mux": True,
        })
    videos.sort(key=lambda item: (item["quality_id"], item["width"] * item["height"], item["codec"].startswith("avc"), item["bit_rate"]), reverse=True)
    return videos, audios


async def parse_bilibili(url: str) -> Optional[dict]:
    """Return only streams present in successful upstream payloads; failures are None."""
    if not isinstance(url, str) or not _site_url(url):
        return None
    initial_part = parse_qs(urlsplit(url).query, keep_blank_values=True).get("p")
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/", "Accept": "application/json"}
    cookie = os.environ.get("BILIBILI_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            url = await _resolve_video_url(client, url)
            if not url:
                return None
            match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|$)", urlsplit(url).path)
            if not match:
                return None
            video_id = match.group(1)
            identity = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
            requested_part = (initial_part or parse_qs(urlsplit(url).query, keep_blank_values=True).get("p", ["1"]))[0]
            if not requested_part.isdigit() or int(requested_part) < 1:
                return None
            part_index = int(requested_part)
            view = await _api(client, "/x/web-interface/view", identity, headers)
            if not view:
                return None
            pages = view.get("pages") or []
            if not isinstance(pages, list) or not pages:
                return None
            page = next((page for page in pages if isinstance(page, dict) and _number(page.get("page")) == part_index), None)
            if not page or not _number(page.get("cid")):
                return None
            params = {**identity, "cid": page.get("cid"), "qn": 127, "fnver": 0, "fnval": 0, "fourk": 1}
            playback = await _api(client, "/x/player/playurl", params, headers)
            variants = _progressive(playback or {})
            dash_playback = await _api(client, "/x/player/playurl", {**params, "fnval": 4048}, headers)
            dash_variants, audios = _dash(dash_playback or {})
            if not variants:
                playback = dash_playback
            variants.extend(dash_variants)
            if not variants:
                return None
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
    result = {
        "type": "video", "title": view.get("title") or "", "cover": view.get("pic") or "",
        "duration": _number(playback.get("timelength")) or _number(page.get("duration")) * 1000,
        "author": {"name": owner.get("name") or "", "id": str(owner.get("mid") or ""), "avatar": owner.get("face") or ""},
        "source": "bilibili_native", "video_backup": variants, **variants[0],
    }
    canonical_id = view.get("bvid") or video_id
    canonical_url = f"https://www.bilibili.com/video/{canonical_id}/"
    result["parts"] = [{
        "index": _number(item.get("page")), "title": item.get("part") or "",
        "url": canonical_url + f"?p={_number(item.get('page'))}",
        "cid": _number(item.get("cid")), "duration": _number(item.get("duration")) * 1000,
    } for item in pages if isinstance(item, dict) and _number(item.get("page")) and _number(item.get("cid"))]
    if audios:
        result["audio_backup"] = audios
    statistics = view.get("stat") if isinstance(view.get("stat"), dict) else {}
    result["statistics"] = {key: value for key, value in statistics.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
    if "view" in result["statistics"]:
        result["play_count"] = result["statistics"]["view"]
    return result
