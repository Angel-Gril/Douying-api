"""Native, read-only Xiaohongshu and Kuaishou page/API extraction."""

import json
import math
import os
import random
import re
import urllib.parse
from typing import Optional

import httpx

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
XHS_HOSTS = ("xiaohongshu.com", "xhslink.com", "xhs.com")
KUAISHOU_HOSTS = ("kuaishou.com", "chenzhongtech.com")
_MEDIA_FIELDS = ("quality", "width", "height", "data_size", "fps", "bit_rate", "codec", "format")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _first(data, *keys):
    return next((data[key] for key in keys if data.get(key) not in (None, "")), None)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def _integer(value):
    return int(_number(value) or 0)


def _fps(value):
    if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", value):
        numerator, denominator = map(float, value.split("/"))
        return _number(numerator / denominator) if denominator else 0
    return _number(value) or 0


def _http_url(value):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value) or "\\" in value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return ""
        parsed.port
    except ValueError:
        return ""
    return value


def _platform_url(url, hosts):
    if not _http_url(url):
        return False
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname.lower()
    return parsed.scheme in ("http", "https") and parsed.port in (None, 80 if parsed.scheme == "http" else 443) and any(host == root or host.endswith("." + root) for root in hosts)


async def _page_get(client, url, hosts, headers, xhs_cookie=""):
    """Validate every hop before sending a request, including cookie recipients."""
    for _ in range(6):
        if not _platform_url(url, hosts):
            return None
        request_headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
        parsed = urllib.parse.urlsplit(url)
        if xhs_cookie and parsed.scheme == "https" and _platform_url(url, ("xiaohongshu.com",)):
            request_headers["Cookie"] = xhs_cookie
        try:
            response = await client.get(url, headers=request_headers, follow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return None
                url = urllib.parse.urljoin(url, location)
                continue
            response.raise_for_status()
            return response if 200 <= response.status_code < 300 else None
        except (httpx.HTTPError, ValueError):
            return None
    return None


def _decode_state(raw):
    """Decode JSON/serialized JSON without executing JavaScript or altering text."""
    raw = raw.lstrip()
    if raw.startswith("JSON.parse("):
        raw = raw[len("JSON.parse("):].lstrip()
    raw = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b', lambda match: "null" if match.group() == "undefined" else match.group(), raw)
    try:
        state, _ = json.JSONDecoder().raw_decode(raw)
        for _ in range(2):
            if not isinstance(state, str):
                break
            state = json.loads(state)
        return state
    except (ValueError, TypeError, RecursionError):
        return None


def _states(html, names):
    for body in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.DOTALL | re.IGNORECASE):
        for name in names:
            for match in re.finditer(r"(?:\bwindow\s*\.\s*)?\b" + re.escape(name) + r"\s*=\s*", body):
                state = _decode_state(body[match.end():])
                if isinstance(state, dict):
                    yield state


def _urls(value):
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if isinstance(item, dict):
            item = _first(item, "url", "masterUrl", "master_url")
        url = _http_url(item)
        if url and url not in result:
            result.append(url)
    return result


def _codec(value):
    value = _text(value).lower()
    if value.startswith(("avc", "h264", "h.264")):
        return "h264"
    if value.startswith(("hev", "hvc", "h265", "h.265")):
        return "h265"
    if value.startswith(("av1", "av01")):
        return "av1"
    return value


def _variant(data, codec=""):
    data = _dict(data)
    urls = []
    for key in ("masterUrl", "master_url", "url", "photoUrl", "playUrl", "videoUrl", "urlList", "url_list", "backupUrls", "backup_urls", "backupUrl"):
        for url in _urls(data.get(key)):
            if url not in urls:
                urls.append(url)
    if not urls:
        return None
    width = _integer(_first(data, "width", "videoWidth"))
    height = _integer(_first(data, "height", "videoHeight"))
    return {
        "url": urls[0], "url_list": urls,
        "quality": _text(_first(data, "qualityType", "quality", "definition", "qualityLabel", "name")) or (f"{min(width, height)}p" if width and height else ""),
        "width": width, "height": height,
        "data_size": _integer(_first(data, "data_size", "size", "fileSize", "file_size")),
        "fps": _fps(_first(data, "fps", "frameRate", "frame_rate")),
        "bit_rate": _integer(_first(data, "avgBitrate", "videoBitrate", "bit_rate", "bitrate", "bitRate")),
        "codec": _codec(_first(data, "codec", "codecType", "codec_type", "codecs") or codec),
        "format": _text(_first(data, "format", "container")),
        "duration": _integer(_first(data, "durationMs", "duration_ms", "duration")),
    }


def _unique_variants(variants):
    result = []
    by_url = {}
    for item in variants:
        if not item:
            continue
        if item["url"] in by_url:
            old = by_url[item["url"]]
            old["url_list"] = list(dict.fromkeys(old["url_list"] + item["url_list"]))
            for key in (*_MEDIA_FIELDS, "duration"):
                if not old.get(key) and item.get(key):
                    old[key] = item[key]
        else:
            by_url[item["url"]] = item
            result.append(item)
    return result


def _select_stream(variants):
    if not variants:
        return None
    area = max(item["width"] * item["height"] for item in variants)
    same_resolution = [item for item in variants if item["width"] * item["height"] == area]
    best = max(same_resolution, key=lambda item: item["bit_rate"])
    # Prefer widely playable H264 only when its measured quality is comparable.
    compatible = [item for item in same_resolution if item["codec"] == "h264" and item["bit_rate"] >= best["bit_rate"] * .9]
    return max(compatible, key=lambda item: item["bit_rate"]) if compatible else best


def _set_video(result, variants, duration=0):
    variants = _unique_variants(variants)
    selected = _select_stream(variants)
    if not selected:
        return None
    remaining = sorted((item for item in variants if item is not selected), key=lambda item: (item["width"] * item["height"], item["bit_rate"]), reverse=True)
    result.update({"type": "video", "url": selected["url"], "video_backup": [selected, *remaining]})
    result.update({key: selected[key] for key in _MEDIA_FIELDS})
    result["duration"] = selected["duration"] or _integer(duration)
    return result


def _music(data):
    data = _dict(data)
    urls = _urls(_first(data, "url", "musicUrl", "audioUrl", "audioUrls", "playUrl"))
    return {"title": _text(_first(data, "title", "name", "musicName")), "url": urls[0] if urls else ""}


def _statistics(data):
    aliases = {
        "play_count": ("playCount", "viewCount", "play_count"),
        "like_count": ("likedCount", "likeCount", "like_count"),
        "comment_count": ("commentCount", "comment_count"),
        "collect_count": ("collectedCount", "collectCount", "collect_count"),
        "share_count": ("shareCount", "share_count"),
    }
    result = {}
    for name, keys in aliases.items():
        for key in keys:
            number = _number(data.get(key))
            if number is not None:
                result[name] = number
                break
    return result


def _base_result(title, desc, author, music, source, statistics):
    result = {"title": _text(title), "desc": _text(desc), "author": author, "music": _music(music), "source": source, "duration": 0, "statistics": _statistics(statistics)}
    if "play_count" in result["statistics"]:
        result["play_count"] = result["statistics"]["play_count"]
    return result


def _extract_xhs_note_id(url: str) -> Optional[str]:
    parsed = urllib.parse.urlsplit(url)
    match = re.search(r"/(?:explore|discovery/item|note|item)/([a-fA-F0-9]{24})(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    value = urllib.parse.parse_qs(parsed.query).get("source_note_id", [""])[0]
    return value if re.fullmatch(r"[a-fA-F0-9]{24}", value) else None


def _extract_xsec_token(url: str) -> Optional[str]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("xsec_token", [None])[0]


async def parse_xiaohongshu(url: str) -> Optional[dict]:
    if not _platform_url(url, XHS_HOSTS):
        return None
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    cookie = os.getenv("XHS_COOKIE", "") or os.getenv("XIAOHONGSHU_COOKIE", "")
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
        note_id = _extract_xhs_note_id(url)
        if not note_id:
            response = await _page_get(client, url, XHS_HOSTS, headers, cookie)
            if response is None:
                return None
            url = str(response.url)
            note_id = _extract_xhs_note_id(url)
            if not note_id:
                return None
            result = _parse_xhs_html(response.text, note_id)
            if result:
                return result
        token = _extract_xsec_token(url)
        page_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if token:
            page_url += "?" + urllib.parse.urlencode({"xsec_token": token})
        response = await _page_get(client, page_url, XHS_HOSTS, headers, cookie)
        return _parse_xhs_html(response.text, note_id) if response is not None else None


def _xhs_streams(container, depth=0):
    if depth > 5:
        return []
    if isinstance(container, list):
        return [variant for value in container for variant in _xhs_streams(value, depth + 1)]
    container = _dict(container)
    variants = []
    for codec in ("h264", "h265", "av1"):
        value = container.get(codec)
        for stream in value if isinstance(value, list) else [value]:
            variant = _variant(stream, codec)
            if variant:
                variants.append(variant)
    for key in ("stream", "media", "mediaV2", "videoInfo", "video_info", "video"):
        if key in container:
            nested = _xhs_streams(container[key], depth + 1)
            if not nested and key in ("videoInfo", "video_info"):
                direct = _variant(container[key])
                if direct:
                    nested.append(direct)
            variants.extend(nested)
    return _unique_variants(variants)


def _xhs_image(image):
    if isinstance(image, str):
        value = image
    else:
        image = _dict(image)
        value = _first(image, "urlDefault", "url_default", "url", "urlPre")
        if not value:
            values = _urls(image.get("infoList"))
            value = values[0] if values else ""
    url = _http_url(value)
    if not url and isinstance(value, str) and value and ":" not in value:
        url = _http_url("https://sns-webpic-qc.xhscdn.com/" + value.lstrip("/"))
    return url


def _parse_xhs_html(html: str, note_id: str) -> Optional[dict]:
    for state in _states(html, ("__INITIAL_STATE__", "__INITIAL_SSR_STATE__")):
        note_map = _dict(_dict(state.get("note")).get("noteDetailMap"))
        note = _dict(_dict(note_map.get(note_id)).get("note"))
        if not note:
            note = _dict(_dict(_dict(state.get("noteData")).get("data")).get("noteData"))
        if not note:
            continue
        user = _dict(note.get("user"))
        result = _base_result(note.get("title") or note.get("desc"), note.get("desc"), {
            "name": _text(_first(user, "nickname", "nickName", "name")), "id": _text(_first(user, "userId", "user_id", "id")), "avatar": _text(user.get("avatar")),
        }, note.get("music"), "xiaohongshu_native", _dict(note.get("interactInfo")))
        images, live = [], []
        for item in _list(note.get("imageList")):
            image = _xhs_image(item)
            if image:
                images.append(image)
                selected = _select_stream(_xhs_streams(item))
                if selected:
                    live.append({"image": image, "video": selected["url"]})
        cover = images[0] if images else _xhs_image(note.get("cover"))
        result["cover"] = cover
        if note.get("type") == "video":
            video = _dict(note.get("video"))
            variants = _xhs_streams(video) + _xhs_streams(note.get("mediaV2"))
            if not variants:
                origin = _dict(video.get("consumer")).get("originVideoKey")
                if isinstance(origin, str) and origin:
                    url = _http_url(origin) or _http_url("https://sns-video-bd.xhscdn.com/" + origin.lstrip("/"))
                    variants = [_variant({"url": url})]
            if not cover:
                thumbnail = _dict(video.get("image")).get("thumbnailFileid")
                result["cover"] = _xhs_image(thumbnail)
            media_video = _dict(_dict(video.get("media")).get("video"))
            return _set_video(result, variants, _first(video, "durationMs", "duration") or media_video.get("duration"))
        if images:
            result.update({"type": "live" if live else "image", "images": images})
            if live:
                result["live_photo"] = live
            return result
    return None


def _extract_kuaishou_photo_id(url: str) -> Optional[str]:
    parsed = urllib.parse.urlsplit(url)
    match = re.search(r"/(?:short-video|(?:fw/)?photo|(?:fw/)?long-video)/([a-zA-Z0-9_-]+)(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    value = urllib.parse.parse_qs(parsed.query).get("photoId", [""])[0]
    return value if re.fullmatch(r"[a-zA-Z0-9_-]+", value) else None


async def parse_kuaishou(url: str) -> Optional[dict]:
    if not _platform_url(url, KUAISHOU_HOSTS):
        return None
    headers = {"User-Agent": MOBILE_UA, "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
        photo_id = _extract_kuaishou_photo_id(url)
        resolved_html = None
        if not photo_id:
            response = await _page_get(client, url, KUAISHOU_HOSTS, headers)
            if response is None:
                return None
            url, resolved_html = str(response.url), response.text
            photo_id = _extract_kuaishou_photo_id(url)
        if not photo_id:
            return None
        result = await _kuaishou_mobile_api(client, photo_id)
        if result:
            return result
        if resolved_html:
            result = _parse_kuaishou_html(resolved_html, photo_id)
            if result:
                return result
        result = await _kuaishou_page_extract(client, photo_id, url)
        if result:
            return result
        canonical = f"https://www.kuaishou.com/short-video/{photo_id}"
        return await _kuaishou_page_extract(client, photo_id) if url != canonical else None


async def _kuaishou_mobile_api(client: httpx.AsyncClient, photo_id: str) -> Optional[dict]:
    headers = {"Content-Type": "application/json; charset=UTF-8", "Referer": f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}", "Cookie": f"did=web_{random.randint(100000000, 999999999)}", "User-Agent": MOBILE_UA}
    try:
        response = await client.post("https://v.m.chenzhongtech.com/rest/wd/photo/info", json={"photoId": photo_id, "isLongVideo": False}, headers=headers, follow_redirects=False)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            return None
        data = _dict(response.json())
    except (httpx.HTTPError, ValueError):
        return None
    if data.get("result") not in (1, "1"):
        return None
    return _parse_kuaishou_detail(data)


async def _kuaishou_page_extract(client: httpx.AsyncClient, photo_id: str, page_url=None) -> Optional[dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    response = await _page_get(client, page_url or f"https://www.kuaishou.com/short-video/{photo_id}", KUAISHOU_HOSTS, headers)
    return _parse_kuaishou_html(response.text, photo_id) if response is not None else None


def _apollo_resolve(value, cache, depth=0):
    if depth > 20:
        return None
    if isinstance(value, dict):
        if isinstance(value.get("__ref"), str):
            return _apollo_resolve(cache.get(value["__ref"]), cache, depth + 1)
        return {key: _apollo_resolve(item, cache, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_apollo_resolve(item, cache, depth + 1) for item in value]
    return value


def _parse_kuaishou_html(html, photo_id):
    for state in _states(html, ("INIT_STATE",)):
        candidates = [state] if isinstance(state.get("photo"), dict) else []
        candidates.extend(value for key, value in state.items() if key.startswith("tusjoh") and isinstance(value, dict))
        for detail in candidates:
            photo = _dict(detail.get("photo"))
            known_id = _first(photo, "id", "photoId") or detail.get("fid")
            if known_id and str(known_id) != photo_id:
                continue
            result = _parse_kuaishou_detail(detail)
            if result:
                return result
    for match in re.finditer(r'<script\b(?=[^>]*\bid=["\']__NEXT_DATA__["\'])[^>]*>(.*?)</script\s*>', html, re.DOTALL | re.IGNORECASE):
        data = _dict(_decode_state(match.group(1)))
        detail = _dict(_dict(data.get("props")).get("pageProps")).get("videoDetail")
        result = _parse_kuaishou_detail(_dict(detail))
        if result:
            return result
    for state in _states(html, ("__APOLLO_STATE__",)):
        cache = _dict(state.get("defaultClient")) or state
        photo = _dict(cache.get(f"VisionVideoDetailPhoto:{photo_id}"))
        if photo:
            photo = _dict(_apollo_resolve(photo, cache))
            author = _dict(photo.get("author"))
            if not author:
                authors = [value for key, value in cache.items() if key.startswith("VisionVideoDetailAuthor:") and isinstance(value, dict)]
                if len(authors) == 1:
                    author = authors[0]
            result = _parse_kuaishou_detail({"photo": photo, "author": author})
            if result:
                return result
        for value in cache.values():
            if isinstance(value, dict) and value.get("photoUrl") and not value.get("__typename") and not photo:
                known_id = _first(value, "id", "photoId")
                if not known_id or str(known_id) == photo_id:
                    result = _parse_kuaishou_detail({"photo": value})
                    if result:
                        return result
    return None


def _kuaishou_streams(resource, codec="", depth=0):
    if depth > 7:
        return []
    if isinstance(resource, str):
        resource = _decode_state(resource)
    if isinstance(resource, list):
        return [item for value in resource for item in _kuaishou_streams(value, codec, depth + 1)]
    resource = _dict(resource)
    variants = []
    variant = _variant(resource, codec)
    if variant:
        variants.append(variant)
    for name in ("h264", "h265", "av1"):
        if name in resource:
            variants.extend(_kuaishou_streams(resource[name], name, depth + 1))
    for name in ("adaptationSet", "representation", "json"):
        if name in resource:
            variants.extend(_kuaishou_streams(resource[name], codec, depth + 1))
    return variants


def _atlas_images(atlas):
    atlas = _dict(atlas)
    cdns = _list(atlas.get("cdn"))
    cdn = _text(cdns[0]) if cdns else "tx2.a.yximgs.com"
    base = cdn if cdn.startswith(("http://", "https://")) else "https://" + cdn
    result = []
    for item in _list(atlas.get("list")):
        path = _first(item, "url", "path") if isinstance(item, dict) else item
        url = _http_url(path)
        if not url and isinstance(path, str) and path and ":" not in path:
            url = _http_url(base.rstrip("/") + "/" + path.lstrip("/"))
        if url:
            result.append(url)
    return result


def _parse_kuaishou_detail(detail: dict) -> Optional[dict]:
    detail = _dict(detail)
    photo = _dict(detail.get("photo"))
    if not photo:
        return None
    author = _dict(detail.get("author")) or _dict(photo.get("author"))
    result = _base_result(photo.get("caption"), photo.get("caption"), {
        "name": _text(_first(author, "name", "userName") or photo.get("userName") or photo.get("name")),
        "id": _text(_first(author, "id", "userId") or photo.get("userId")),
        "avatar": _text(_first(author, "headerUrl", "headUrl", "avatar") or photo.get("headUrl")),
    }, photo.get("music") or photo.get("soundTrack"), "kuaishou_native", photo)
    covers = _urls(photo.get("coverUrl")) or _urls(photo.get("coverUrls"))
    result["cover"] = covers[0] if covers else ""
    atlas = _dict(detail.get("atlas")) or _dict(_dict(photo.get("ext_params")).get("atlas")) or _dict(photo.get("atlas"))
    images = _atlas_images(atlas)
    if not images and (photo.get("photoType") == "SINGLE_PICTURE" or photo.get("singlePicture") is True):
        images = covers
    if images:
        result.update({"type": "image", "images": images, "cover": result["cover"] or images[0]})
        if not result["music"]["url"] and isinstance(atlas.get("music"), str):
            path = atlas["music"]
            result["music"]["url"] = _http_url(path) or (_http_url("https://txmov2.a.kwimgs.com/" + path.lstrip("/")) if ":" not in path else "")
        return result
    variants = []
    for key, codec in (("videoResource", ""), ("manifest", "h264"), ("manifestH264", "h264"), ("manifestH265", "h265")):
        variants.extend(_kuaishou_streams(photo.get(key), codec))
    direct_urls = _urls(photo.get("photoUrl")) + _urls(photo.get("mainMvUrls"))
    known_urls = {url for item in variants for url in item["url_list"]}
    for url in direct_urls:
        if url not in known_urls:
            variants.append(_variant({**photo, "url": url, "photoUrl": None}))
            known_urls.add(url)
    return _set_video(result, variants, _first(photo, "durationMs", "duration_ms", "duration"))
