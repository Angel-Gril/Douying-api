"""有资源上限的 MP4 元数据解析；网络由调用方提供，不读取完整媒体。"""

from __future__ import annotations

import struct
import time
from collections import OrderedDict


MAX_MOOV_BYTES = 4 * 1024 * 1024
CACHE_TTL = 300
_cache = OrderedDict()
_CODECS = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "h265", b"hev1": "h265", b"av01": "av1", b"vp09": "vp9", b"mp4v": "mpeg4"}


class MetadataError(ValueError):
    pass


def _header(data, offset, boundary):
    if len(data) < 8:
        raise MetadataError("incomplete box header")
    size, kind = struct.unpack_from(">I4s", data)
    header_size = 8
    if size == 1:
        if len(data) < 16:
            raise MetadataError("incomplete extended box header")
        size, header_size = struct.unpack_from(">Q", data, 8)[0], 16
    elif size == 0:
        size = boundary - offset
    if size < header_size or offset + size > boundary:
        raise MetadataError("invalid box size")
    return kind, size, header_size


def _boxes(data, start=0, end=None, *, budget=None):
    end = len(data) if end is None else end
    offset, count = start, 0
    while offset < end:
        count += 1
        if count > 512:
            raise MetadataError("too many boxes")
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                raise MetadataError("metadata box budget exhausted")
        kind, size, header_size = _header(data[offset:offset + 16], offset, end)
        yield kind, offset + header_size, offset + size
        offset += size


def _children(data, start, end, budget):
    return {kind: (begin, finish) for kind, begin, finish in _boxes(data, start, end, budget=budget)}


def _times(data, bounds):
    if not bounds:
        return None
    start, end = bounds
    if end - start < 20:
        return None
    version = data[start]
    if version == 0:
        scale, duration = struct.unpack_from(">II", data, start + 12)
        invalid_duration = 0xFFFFFFFF
    elif version == 1 and end - start >= 32:
        scale, duration = struct.unpack_from(">IQ", data, start + 20)
        invalid_duration = 0xFFFFFFFFFFFFFFFF
    else:
        return None
    return (scale, duration) if scale > 0 and 0 < duration < invalid_duration else None


def _rotation(data, bounds):
    if not bounds:
        return None
    start, end = bounds
    offset = 40 if data[start] == 0 else 52 if data[start] == 1 else -1
    if offset < 0 or end - start < offset + 36:
        return None
    matrix = struct.unpack_from(">9i", data, start + offset)
    axes = (matrix[0], matrix[1], matrix[3], matrix[4])
    return {(65536, 0, 0, 65536): 0, (0, 65536, -65536, 0): 90,
            (-65536, 0, 0, -65536): 180, (0, -65536, 65536, 0): 270}.get(axes)


def _sample_description(data, bounds, budget):
    if not bounds:
        return {}
    start, end = bounds
    if end - start < 8:
        return {}
    count = struct.unpack_from(">I", data, start + 4)[0]
    if not 0 < count <= 16:
        return {}
    entries = list(_boxes(data, start + 8, end, budget=budget))
    if len(entries) != count:
        return {}
    # Mixed sample descriptions can represent changing encodings or dimensions.
    descriptions = []
    for kind, begin, finish in entries:
        if kind not in _CODECS or finish - begin < 78:
            return {}
        width, height = struct.unpack_from(">HH", data, begin + 24)
        if not width or not height:
            return {}
        descriptions.append((kind, width, height))
    if len(set(descriptions)) != 1:
        return {}
    kind, width, height = descriptions[0]
    return {"codec": _CODECS[kind], "codec_tag": kind.decode("ascii"), "width": width, "height": height}


def _timing(data, bounds, maximum):
    if not bounds:
        return None
    start, end = bounds
    if end - start < 8:
        return None
    count = struct.unpack_from(">I", data, start + 4)[0]
    if not 0 < count <= maximum or end - start != 8 + count * 8:
        return None
    samples, duration = 0, 0
    for number, delta in struct.iter_unpack(">II", data[start + 8:end]):
        if not number or not delta:
            return None
        samples += number
        duration += number * delta
    return (samples, duration) if samples and duration else None


def _sample_bytes(data, bounds, expected_samples, maximum):
    if not bounds:
        return None
    start, end = bounds
    if end - start < 12:
        return None
    size, count = struct.unpack_from(">II", data, start + 4)
    if count != expected_samples or not count:
        return None
    if size:
        return size * count if end - start == 12 else None
    if count > maximum or end - start != 12 + count * 4:
        return None
    return sum(item[0] for item in struct.iter_unpack(">I", data[start + 12:end]))


def parse_moov(content, file_size, *, max_timing_entries=16384, max_sample_entries=200000, max_boxes=4096):
    """返回可确认字段；缺失或超限的时间表不影响已经确认的尺寸和编码。"""
    try:
        data = memoryview(content)
        budget = [max_boxes]
        outer = list(_boxes(data, budget=budget))
        if len(outer) != 1 or outer[0][0] != b"moov":
            return {}
        _, start, end = outer[0]
        children = list(_boxes(data, start, end, budget=budget))
        movie = _times(data, next(((begin, finish) for kind, begin, finish in children if kind == b"mvhd"), None))
        fragmented = any(kind == b"mvex" for kind, _, _ in children)
        for kind, begin, finish in children:
            if kind != b"trak":
                continue
            track = _children(data, begin, finish, budget)
            tkhd = track.get(b"tkhd")
            if tkhd:
                if tkhd[1] - tkhd[0] < 4 or not (int.from_bytes(data[tkhd[0] + 1:tkhd[0] + 4], "big") & 1):
                    continue
            if b"mdia" not in track:
                continue
            media = _children(data, *track[b"mdia"], budget)
            handler = media.get(b"hdlr")
            if not handler or handler[1] - handler[0] < 12 or data[handler[0] + 8:handler[0] + 12] != b"vide":
                continue
            if b"minf" not in media:
                continue
            information = _children(data, *media[b"minf"], budget)
            if b"stbl" not in information:
                continue
            tables = _children(data, *information[b"stbl"], budget)
            result = _sample_description(data, tables.get(b"stsd"), budget)
            if not result:
                continue
            rotation = _rotation(data, tkhd)
            if rotation is not None:
                result["rotation"] = rotation
            media_time = _times(data, media.get(b"mdhd"))
            if media_time:
                result["video_duration"] = round(media_time[1] * 1000 / media_time[0])
            presentation = movie or media_time
            if presentation:
                scale, duration = presentation
                result["duration"] = round(duration * 1000 / scale)
                result["duration_source"] = "mp4_movie" if movie else "mp4_video_track"
                if movie and type(file_size) is int and file_size > 0:
                    result["overall_bitrate"] = round(file_size * 8 * scale / duration)
            timing = None if fragmented else _timing(data, tables.get(b"stts"), max_timing_entries)
            if timing and media_time:
                count, ticks = timing
                fps = count * media_time[0] / ticks
                if 0 < fps <= 1000:
                    result.update(fps=round(fps, 6), fps_kind="average")
                sizes = _sample_bytes(data, tables.get(b"stsz"), count, max_sample_entries)
                if sizes and type(file_size) is int and 0 < sizes <= file_size:
                    result.update(bitrate=round(sizes * 8 * media_time[0] / ticks), bitrate_kind="video_average")
            result["metadata_source"] = "mp4_moov"
            return result
        return {}
    except (MetadataError, struct.error, TypeError, ValueError, OverflowError):
        return {}


def validate_range_headers(status, headers, start, end, total):
    """必须在读取响应体前调用；忽略 Range 的 200 响应不能被完整下载。"""
    if status != 206:
        raise MetadataError("range not honored")
    expected = f"bytes {start}-{end}/{total}"
    if (headers.get("content-range") or "").strip() != expected:
        raise MetadataError("inconsistent content range")
    length = headers.get("content-length")
    if length and (len(str(length)) > 20 or not str(length).isdecimal() or int(length) != end - start + 1):
        raise MetadataError("inconsistent content length")
    encoding = (headers.get("content-encoding") or "identity").lower()
    if encoding != "identity":
        raise MetadataError("encoded range response")


def media_identity(headers):
    etag = headers.get("etag") or ""
    if etag and not etag.startswith("W/"):
        return ("etag", etag)
    modified = headers.get("last-modified") or ""
    return ("last-modified", modified) if modified else None


async def probe_metadata(read_range, file_size, *, cache_key=None, window_size=65536,
                         max_requests=4, max_bytes=MAX_MOOV_BYTES + 65536,
                         max_moov_bytes=MAX_MOOV_BYTES, max_timing_entries=16384,
                         max_sample_entries=200000, max_boxes=4096):
    """按绝对 box 偏移补读，跳过 mdat；read_range 必须执行有界网络读取。"""
    if type(file_size) is not int or file_size < 8:
        return {}
    key = (cache_key, file_size, max_moov_bytes, max_timing_entries, max_sample_entries, max_boxes)
    if cache_key is not None and key in _cache:
        expires, value = _cache[key]
        if expires > time.monotonic():
            _cache.move_to_end(key)
            return dict(value)
        del _cache[key]
    requests, received = 0, 0
    chunks = []

    async def read(start, count):
        nonlocal requests, received
        if count <= 0 or start < 0 or start + count > file_size:
            raise MetadataError("invalid metadata read bounds")
        for offset, data in chunks:
            if offset <= start and start + count <= offset + len(data):
                return data[start - offset:start - offset + count]
        if requests >= max_requests:
            raise MetadataError("metadata request budget exhausted")
        length = min(max(window_size, count), file_size - start, max_bytes - received)
        if length < count:
            raise MetadataError("metadata byte budget exhausted")
        requests += 1
        data = await read_range(start, start + length - 1)
        if not isinstance(data, (bytes, bytearray)) or len(data) != length:
            raise MetadataError("incomplete metadata range")
        received += len(data)
        chunks.append((start, data))
        return data[:count]

    try:
        offset = 0
        container_format = ""
        for _ in range(64):
            if file_size - offset < 8:
                return {}
            prefix = await read(offset, min(16, file_size - offset))
            kind, size, header_size = _header(prefix, offset, file_size)
            if kind == b"ftyp" and size >= header_size + 4:
                brand = await read(offset + header_size, 4)
                if brand == b"qt  ":
                    container_format = "mov"
                elif brand in {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V "}:
                    container_format = "mp4"
            if kind == b"moov":
                if size > max_moov_bytes:
                    return {}
                content = await read(offset, size)
                result = parse_moov(content, file_size, max_timing_entries=max_timing_entries, max_sample_entries=max_sample_entries, max_boxes=max_boxes)
                if result and container_format:
                    result["format"] = container_format
                if result and cache_key is not None:
                    _cache[key] = (time.monotonic() + CACHE_TTL, dict(result))
                    _cache.move_to_end(key)
                    while len(_cache) > 64:
                        _cache.popitem(last=False)
                return result
            offset += size
            if offset >= file_size:
                return {}
        return {}
    except (MetadataError, TimeoutError, OSError, ValueError, TypeError):
        return {}


def apply_original_metadata(result, metadata=None):
    """切换原画时先清理转码参数，再仅应用媒体自身确认的字段。"""
    result.update(quality="original", width=0, height=0, bitrate=0, bit_rate=0, fps=0, codec="", format="", overall_bitrate=0)
    for name in ("metadata_source", "codec_tag", "rotation", "fps_kind", "bitrate_kind", "video_duration", "duration_source"):
        result.pop(name, None)
    allowed = {"width", "height", "codec", "codec_tag", "rotation", "fps", "fps_kind", "duration", "video_duration", "duration_source", "bitrate", "bitrate_kind", "overall_bitrate", "metadata_source", "format"}
    for name, value in (metadata or {}).items():
        if name in allowed:
            result[name] = value
    result["bit_rate"] = result["bitrate"]
