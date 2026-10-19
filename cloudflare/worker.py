"""免费测试入口：复用纯抖音解析器，不加载浏览器、签名库或媒体子进程。"""

import asyncio
import json
import re
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from workers import WorkerEntrypoint, Response, fetch

from douyin_parser import build_original_video_url, extract_aweme_id, parse_aweme_detail
from original_metadata import MetadataError, apply_original_metadata, media_identity, probe_metadata, validate_range_headers


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
SHARE_DOMAINS = ("douyin.com", "iesdouyin.com")
MEDIA_DOMAINS = ("douyinvod.com", "zjcdn.com", "douyinpic.com", "douyinstatic.com", "byteimg.com", "ibyteimg.com", "bytecdn.cn")
MAX_PROXY_BYTES = 256 * 1024 * 1024


class EdgeError(Exception):
    def __init__(self, message, code=502):
        self.message, self.code = message, code


def allowed_url(value, *, media=False):
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        domains = MEDIA_DOMAINS if media else SHARE_DOMAINS
        valid_host = any(host == domain or host.endswith("." + domain) for domain in domains)
        if media and host in {"aweme.snssdk.com", "www.douyin.com"}:
            valid_host = True
        return (url.scheme == "https" and url.port in (None, 443) and not url.username
                and not url.password and valid_host)
    except (TypeError, ValueError):
        return False


async def request_upstream(url, *, method="GET", range_header=None, if_range=None):
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/", "Accept": "*/*", "Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
        if if_range:
            headers["If-Range"] = if_range
    try:
        return await asyncio.wait_for(fetch(url, method=method, headers=headers, redirect="manual"), timeout=8)
    except Exception:
        raise EdgeError("上游请求失败，请稍后重新解析") from None


async def follow_media(url, *, method="GET", range_header=None, if_range=None):
    for _ in range(5):
        if not allowed_url(url, media=True):
            raise EdgeError("仅支持已验证的平台 HTTPS 媒体地址", 400)
        response = await request_upstream(url, method=method, range_header=range_header, if_range=if_range)
        if response.status not in (301, 302, 303, 307, 308):
            return url, response
        location = response.headers.get("location")
        if not location:
            raise EdgeError("媒体跳转缺少目标地址")
        url = urljoin(url, location)
    raise EdgeError("媒体跳转次数超过上限")


async def original_video_metadata(url, file_size, validator=None):
    """小窗口和表项预算，避免在免费运行时遍历长视频的逐帧大小表。"""
    identity = validator

    async def read_range(start, end):
        nonlocal identity
        _, response = await follow_media(url, range_header=f"bytes={start}-{end}", if_range=identity[1] if identity else None)
        body, reader = response.js_object.body, None
        try:
            validate_range_headers(response.status, response.headers, start, end, file_size)
            marker = media_identity(response.headers)
            if identity and response.headers.get(identity[0]) not in (None, identity[1]):
                raise MetadataError("media changed during metadata reads")
            if not identity and marker:
                identity = marker
            if body is None:
                raise MetadataError("empty metadata body")
            reader = body.getReader()
            content = bytearray()
            while True:
                item = await reader.read()
                if item.done:
                    break
                chunk = item.value.to_py()
                if len(content) + len(chunk) > end - start + 1:
                    raise MetadataError("metadata response exceeds requested range")
                content.extend(chunk)
            return bytes(content)
        finally:
            if reader is not None:
                try:
                    await reader.cancel()
                finally:
                    reader.releaseLock()
            elif body is not None:
                await body.cancel()

    try:
        return await asyncio.wait_for(probe_metadata(read_range, file_size, cache_key=(url, validator),
            window_size=32768, max_requests=3, max_bytes=524288, max_moov_bytes=262144,
            max_timing_entries=256, max_sample_entries=0, max_boxes=256), timeout=4)
    except Exception:
        return {}


async def parse_share(value):
    if not value or len(value) > 8192:
        raise EdgeError("请提供有效的抖音分享链接", 400)
    match = re.search(r'https?://[^\s<>"{}|\\^`\[\]]+', value.strip())
    url = match.group(0) if match else value.strip()
    if not allowed_url(url):
        raise EdgeError("测试后端只支持抖音 HTTPS 分享链接", 400)
    original = url
    if (urlsplit(url).hostname or "") in {"v.douyin.com", "www.iesdouyin.com", "iesdouyin.com"}:
        for _ in range(5):
            if not allowed_url(url):
                raise EdgeError("分享链接跳转到了不支持的站点", 400)
            response = await request_upstream(url)
            if response.status not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            if not location:
                break
            url = urljoin(url, location)
        if not allowed_url(url):
            raise EdgeError("分享链接跳转到了不支持的站点", 400)
    aweme_id = extract_aweme_id(url) or extract_aweme_id(original)
    if not aweme_id:
        raise EdgeError("无法从分享链接提取作品 ID", 400)
    headers = {"User-Agent": USER_AGENT, "Origin": "https://open.douyin.com", "Referer": "https://open.douyin.com/", "Accept": "application/json"}
    endpoint = "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urlencode({"aweme_id": aweme_id, "aid": "6383", "device_platform": "webapp"})
    try:
        response = await asyncio.wait_for(fetch(endpoint, headers=headers, redirect="manual"), timeout=8)
        if response.status != 200:
            return None
        declared = response.headers.get("content-length") or "0"
        if declared.isdecimal() and int(declared) > 2 * 1024 * 1024:
            return None
        text = await response.text()
        if len(text) > 2 * 1024 * 1024:
            return None
        payload = json.loads(text)
        detail = payload.get("aweme_detail") if payload.get("status_code") == 0 else None
        if not isinstance(detail, dict) or str(detail.get("aweme_id")) != aweme_id:
            return None
        result = parse_aweme_detail(detail)
        if result.get("type") == "video":
            original_url = build_original_video_url(result.get("video_id"))
            if original_url:
                try:
                    target, probe = await follow_media(original_url, method="HEAD")
                    content_type = (probe.headers.get("content-type") or "").split(";", 1)[0]
                    length = probe.headers.get("content-length") or "0"
                    if probe.status in (200, 206) and (content_type.startswith("video/") or content_type == "application/octet-stream") and length.isdecimal() and int(length) > 0:
                        result.update(url=target, data_size=int(length))
                        metadata = await original_video_metadata(target, int(length), media_identity(probe.headers))
                        apply_original_metadata(result, metadata)
                    elif result.get("url") == original_url:
                        return None
                except EdgeError:
                    if result.get("url") == original_url:
                        return None
        if not (result.get("url") or result.get("images")):
            return None
        result["source"] = "web_api_open"
        return result
    except (ValueError, TypeError, AttributeError, asyncio.TimeoutError):
        return None


def validate_range(value, response):
    if not value:
        return
    wanted = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not wanted or not any(wanted.groups()):
        raise EdgeError("不支持的 Range 请求", 416)
    start, end = wanted.groups()
    if start and end and int(start) > int(end):
        raise EdgeError("不支持的 Range 请求", 416)
    if response is None or response.status == 200:
        return
    actual = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range") or "")
    if not actual:
        raise EdgeError("上游返回了无效的字节范围")
    first, last, total = map(int, actual.groups())
    if last < first or last >= total or (start and first != int(start)) or (end and start and last != min(int(end), total - 1)):
        raise EdgeError("上游返回的字节范围与请求不一致")
    if not start and (first != max(0, total - int(end)) or last != total - 1):
        raise EdgeError("上游返回的尾部字节范围不一致")
    length = response.headers.get("content-length") or ""
    if not length.isdecimal() or int(length) != last - first + 1:
        raise EdgeError("上游分段字节范围和文件长度不一致")


async def proxy_media(query, request, cors):
    url = query.get("url", [""])[0]
    if len(url) > 8192 or not allowed_url(url, media=True):
        raise EdgeError("仅支持已验证的平台 HTTPS 媒体地址", 400)
    range_header = request.headers.get("range") or None
    validate_range(range_header, None)
    target, upstream = await follow_media(url, range_header=range_header)
    if upstream.status not in (200, 206):
        raise EdgeError("媒体下载失败，请重新解析链接", 502)
    content_type = (upstream.headers.get("content-type") or "").split(";", 1)[0].lower()
    if urlsplit(target).path.lower().endswith(".m3u8") or "mpegurl" in content_type:
        raise EdgeError("测试后端不支持 HLS 合并，请使用完整后端", 400)
    if not content_type.startswith(("video/", "audio/", "image/")) and content_type != "application/octet-stream":
        raise EdgeError("上游返回非媒体内容，未下载为文件")
    size = upstream.headers.get("content-length") or ""
    if not size.isdecimal() or not int(size):
        raise EdgeError("测试代理需要有效的媒体文件长度")
    if int(size) > MAX_PROXY_BYTES:
        raise EdgeError("测试代理单次最多处理 256 MiB，请使用完整后端", 413)
    validate_range(range_header, upstream)
    if upstream.status == 206 and not range_header:
        raise EdgeError("上游仅返回部分媒体，请重新解析")
    filename = re.sub(r'[\r\n\\/:*?"<>|]+', "_", query.get("filename", ["video.mp4"])[0])[:180] or "video.mp4"
    headers = {**cors, "Content-Type": content_type, "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""), "Content-Length": size, "Cache-Control": "no-store"}
    for name in ("content-range", "accept-ranges", "etag", "last-modified"):
        value = upstream.headers.get(name)
        if value:
            headers[name] = value
    return Response(upstream.js_object.body, status=upstream.status, headers=headers)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        incoming = urlsplit(str(request.url))
        origin = request.headers.get("origin")
        own_origin = f"{incoming.scheme}://{incoming.netloc}"
        configured = str(getattr(self.env, "CORS_ORIGINS", "") or "")
        allowed = {value.strip() for value in configured.split(",") if value.strip()} | {own_origin}
        cors = {"Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "Content-Type, Range", "Access-Control-Expose-Headers": "Content-Disposition, Content-Length, Content-Range", "Vary": "Origin"}
        if origin and origin not in allowed:
            return Response.json({"code": 403, "msg": "此网页来源未被允许", "data": None}, status=403)
        if origin:
            cors["Access-Control-Allow-Origin"] = origin
        if request.method == "OPTIONS":
            return Response(status=204, headers=cors)

        def reply(data, status=200):
            return Response.json(data, status=status, headers={**cors, "Cache-Control": "no-store"})

        path = incoming.path
        if path.startswith("/api/download/jobs"):
            return reply({"code": 501, "msg": "免费测试后端暂不支持打包或媒体合并任务", "data": None}, 501)
        if request.method != "GET":
            return reply({"code": 405, "msg": "不支持的请求方法", "data": None}, 405)
        if path == "/api/status":
            return reply({"status": "running", "runtime": "cloudflare_workers", "supported_platforms": ["douyin"], "browser_available": False, "cookie_status": "none", "cookie_configured": False, "strategies": ["web_api_open"], "notice": "抖音直链测试：暂不支持浏览器兜底、ZIP 或音视频合并。", "download_features": {"range": True, "progress": True, "cancel": True, "zip": False, "merge": False, "hls": False}})
        query = parse_qs(incoming.query, keep_blank_values=True)
        try:
            if path == "/api/parse":
                data = await parse_share(query.get("url", [""])[0])
                return reply({"code": 200, "msg": "解析成功", "platform": "douyin", "data": data} if data else {"code": 500, "msg": "当前测试节点未取得媒体，请稍后重试或使用完整后端", "data": None})
            if path == "/api/download":
                return await proxy_media(query, request, cors)
        except EdgeError as error:
            return reply({"code": error.code, "msg": error.message, "data": None}, error.code)
        except Exception:
            return reply({"code": 502, "msg": "解析服务暂时不可用，请稍后重试", "data": None}, 502)
        return reply({"code": 404, "msg": "接口不存在", "data": None}, 404)
