"""Allowlisted public-provider fields in the application's media contract.

BugPK documents duration in seconds; native parsers use their own millisecond
contract and must not pass through this adapter.
"""

from decimal import Decimal, InvalidOperation
import math
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit


def _url(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or "\\" in value or any(char.isspace() or ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return ""
        # Accessing the port also validates malformed values.
        parsed.port
        return value
    except ValueError:
        return ""


def _urls(value) -> list:
    if isinstance(value, dict):
        values = []
        for name in ("url", "video_url", "image", "url_list", "backup_url", "backup_urls", "urls", "play_addr", "play_url"):
            if name in value:
                values.extend(_urls(value[name]))
        return list(dict.fromkeys(values))
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(url for item in value for url in _urls(item)))
    result = _url(value)
    return [result] if result else []


def _first_url(*values) -> str:
    for value in values:
        urls = _urls(value)
        if urls:
            return urls[0]
    return ""


def _number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return 0
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return 0
        return int(number) if integer else number
    except (ValueError, OverflowError):
        return 0


def _text(value) -> str:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def _duration(data: dict, provider: str) -> int:
    if "duration_ms" in data:
        return _number(data["duration_ms"], integer=True)
    if provider != "bugpk":
        return 0
    value = data.get("duration", 0)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return 0
    try:
        seconds = Decimal(str(value))
        return int(seconds * 1000) if seconds.is_finite() and 0 <= seconds <= Decimal("9223372036854775.807") else 0
    except (InvalidOperation, ValueError, OverflowError):
        return 0


def _variant(value, audio_url="", requires_mux=None) -> Optional[dict]:
    data = value if isinstance(value, dict) else {}
    urls = _urls(value)
    if not urls:
        return None
    size = _number(data.get("data_size", data.get("size", data.get("filesize", 0))), integer=True)
    bit_rate = _number(data.get("bit_rate", data.get("bitrate", 0)), integer=True)
    # Observed BugPK legacy responses put byte size into bit_rate. Keep real,
    # independently supplied bitrates; never turn a byte count into bit/s.
    if bit_rate == size:
        bit_rate = 0
    result = {
        "url": urls[0], "url_list": urls,
        "quality": _text(data.get("quality") or data.get("quality_label") or data.get("label") or data.get("name")),
        "width": _number(data.get("width"), integer=True),
        "height": _number(data.get("height"), integer=True),
        "data_size": size,
        "fps": _number(data.get("fps", data.get("FPS"))),
        "bit_rate": bit_rate,
        "codec": _text(data.get("codec") or data.get("vcodec")),
        "format": _text(data.get("format") or data.get("ext")),
    }
    audio = _first_url(data.get("audio_url"), audio_url)
    mux = data.get("requires_mux", requires_mux)
    if audio:
        result["audio_url"] = audio
    if isinstance(mux, bool):
        result["requires_mux"] = mux
    return result


def _author(data: dict) -> dict:
    author = {"name": "", "id": "", "avatar": ""}
    for value in (data.get("author"), data.get("auther"), data.get("user")):
        if isinstance(value, dict):
            name = _text(value.get("name") or value.get("nickname") or value.get("uname"))
            identity = _text(value.get("id") or value.get("uid") or value.get("mid") or value.get("unique_id") or value.get("short_id"))
            avatar = _first_url(value.get("avatar"), value.get("avatar_url"), value.get("avatar_thumb"), value.get("face"))
            author = {"name": author["name"] or name, "id": author["id"] or identity, "avatar": author["avatar"] or avatar}
        elif not author["name"]:
            author["name"] = _text(value)
    author["avatar"] = author["avatar"] or _first_url(data.get("avatar"), data.get("avatar_url"))
    return author


def _bilibili_page(value) -> str:
    value = _url(value)
    if not value:
        return ""
    parsed = urlsplit(value)
    return value if parsed.hostname in ("www.bilibili.com", "bilibili.com", "m.bilibili.com") and parsed.path.startswith("/video/") else ""


def _part_url(base: str, index: int) -> str:
    parsed = urlsplit(base)
    # A public part link needs only the content path and part selection. Drop
    # share/tracking/credentials in query parameters from upstream URLs.
    return urlunsplit(("https", "www.bilibili.com", parsed.path, urlencode({"p": index}), ""))


def _parts(data: dict, provider: str) -> list:
    items = data.get("parts") or data.get("videos") or data.get("pages") or []
    if not isinstance(items, list):
        return []
    base = next((page for field in ("webpage_url", "share_url", "link", "original_url", "url")
                 if (page := _bilibili_page(data.get(field)))), "")
    bvid = _text(data.get("bvid"))
    if not base and bvid.startswith("BV") and bvid.isalnum():
        base = f"https://www.bilibili.com/video/{bvid}/"
    if not base:
        base = next((_bilibili_page(item.get("url")) for item in items
                     if isinstance(item, dict) and _bilibili_page(item.get("url"))), "")
    result = []
    seen = set()
    for offset, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        index = _number(item.get("index", item.get("page", offset)), integer=True) or offset
        if index in seen:
            continue
        own_page = _bilibili_page(item.get("url"))
        page_url = _part_url(own_page or base, index) if own_page or base else ""
        if not page_url:
            continue
        seen.add(index)
        result.append({"index": index, "title": _text(item.get("title") or item.get("part")),
                       "url": page_url, "cid": _number(item.get("cid"), integer=True),
                       "duration": _duration(item, provider)})
    return result


def normalize_provider_result(data: dict, platform: str, provider: str = "bugpk") -> Optional[dict]:
    """Normalize a provider envelope or media object; reject empty successes.

    The output is an allowlist, so private credentials, raw debug payloads and
    supplier-specific fields cannot accidentally reach the public response.
    """
    if not isinstance(data, dict) or not data:
        return None
    payload = data
    for _ in range(3):
        if payload.get("success") is False or ("code" in payload and payload["code"] not in (0, "0", 200, "200")):
            return None
        if "data" not in payload:
            break
        if not isinstance(payload["data"], dict) or not payload["data"]:
            return None
        payload = payload["data"]
    if payload is not data:
        # Some Bilibili providers put metadata alongside data. Only read fields
        # that this adapter knows; the merged mapping is never returned raw.
        payload = {**data, **payload}

    audio_url = _first_url(payload.get("audio_url"))
    requires_mux = payload.get("requires_mux")
    backups = []
    seen = set()
    variants = payload.get("video_backup") or payload.get("formats") or []
    if isinstance(variants, (dict, str)):
        variants = [variants]
    durl = payload.get("durl") or []
    if isinstance(durl, dict):
        durl = [durl]
    if not isinstance(variants, list):
        variants = []
    if isinstance(durl, list):
        variants = variants + durl
    for item in variants:
        variant = _variant(item, audio_url, requires_mux)
        if not variant:
            continue
        distinct = [url for url in variant["url_list"] if url not in seen]
        if not distinct:
            continue
        variant["url"] = distinct[0]
        variant["url_list"] = distinct
        backups.append(variant)
        seen.update(distinct)

    images = _urls(payload.get("images") or payload.get("image_list") or [])
    live = []
    live_items = payload.get("live_photo") or []
    for item in live_items if isinstance(live_items, list) else []:
        if not isinstance(item, dict):
            continue
        image = _first_url(item.get("image"), item.get("image_url"))
        video = _first_url(item.get("video"), item.get("video_url"))
        if image and video and {"image": image, "video": video} not in live:
            live.append({"image": image, "video": video})
            if image not in images:
                images.append(image)

    main_url = _first_url(payload.get("video_url"), payload.get("url"), payload.get("play_url"))
    if platform == "bilibili" and _bilibili_page(main_url):
        main_url = ""
    main_url = main_url or (backups[0]["url"] if backups else "")
    if not main_url and not images and not live:
        return None
    media_type = "live" if live else "image" if images and (payload.get("type") == "image" or not main_url) else "video"
    root_variant = _variant({**payload, "url": main_url}, audio_url, requires_mux) if main_url else None
    root_variant = root_variant or {}
    music = payload.get("music") or {}
    if not isinstance(music, dict):
        music = {"url": music}
    result = {
        "type": media_type, "title": _text(payload.get("title") or payload.get("desc") or payload.get("description")),
        "cover": _first_url(payload.get("cover"), payload.get("cover_url"), payload.get("thumbnail")),
        "author": _author(payload),
        "music": {"title": _text(music.get("title") or music.get("name")), "url": _first_url(music.get("url"), music.get("play_url"))},
        "duration": _duration(payload, provider), "source": "public_api", "provider": provider,
        "data_size": root_variant.get("data_size", 0), "width": root_variant.get("width", 0),
        "height": root_variant.get("height", 0), "fps": root_variant.get("fps", 0),
        "bitrate": root_variant.get("bit_rate", 0), "quality": root_variant.get("quality", ""),
        "codec": root_variant.get("codec", ""), "format": root_variant.get("format", ""),
    }
    if main_url:
        result["url"] = main_url
        result["video_backup"] = backups
    if images:
        result["images"] = images
    if live:
        result["live_photo"] = live
    if audio_url:
        result["audio_url"] = audio_url
    if isinstance(requires_mux, bool):
        result["requires_mux"] = requires_mux
    original_url = _first_url(payload.get("original_url"))
    if original_url and not _bilibili_page(original_url):
        result["original_url"] = original_url
    if platform == "bilibili":
        parts = _parts(payload, provider)
        if parts:
            result["parts"] = parts
    statistics = payload.get("statistics") or {}
    numeric_stats = {}
    if isinstance(statistics, dict):
        for key in ("play_count", "digg_count", "like_count", "comment_count", "collect_count", "share_count", "favorite_count", "view_count",
                    "play", "view", "digg", "like", "comment", "collect", "share", "favorite", "coin", "danmaku", "reply"):
            value = statistics.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                numeric_stats[key] = value
    play_count = payload.get("play_count", numeric_stats.get("play_count"))
    if type(play_count) in (int, float) and math.isfinite(play_count) and play_count >= 0:
        result["play_count"] = play_count
        numeric_stats["play_count"] = play_count
    if numeric_stats:
        result["statistics"] = numeric_stats
    return result
