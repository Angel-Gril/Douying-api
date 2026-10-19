"""媒体下载的地址校验、平台请求头和流完整性检查。"""

import asyncio
import ipaddress
import os
import re
import socket
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
FAKE_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
TRUSTED_MEDIA_DOMAINS = (
    "douyinvod.com", "zjcdn.com", "douyinpic.com", "douyinstatic.com",
    "bilivideo.com", "hdslb.com", "biliimg.com", "xhscdn.com",
    "kwaicdn.com", "kwimgs.com", "yximgs.com",
    "douyin.com", "iesdouyin.com", "snssdk.com", "byteimg.com", "ibyteimg.com",
    "bytedance.com", "bytecdn.cn", "ixigua.com", "toutiaovod.com", "pstatp.com",
    "bilibili.com", "bilivideo.cn", "acgvideo.com", "xiaohongshu.com",
    "kuaishou.com", "weibocdn.com", "sinaimg.cn", "sina.com.cn",
    "doubao.com", "doubaocdn.com", "volces.com", "ippzone.com", "pipix.com",
    "xiaochuankeji.cn", "izuiyou.com",
)
PLAYLIST_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"}


class MediaError(Exception):
    def __init__(self, message, code=502):
        super().__init__(message)
        self.code = code


def is_safe_media_url(url):
    try:
        if not isinstance(url, str) or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
            return False
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        # 任意用户域名可在两次 DNS 查询之间重绑定。仅接受平台控制的媒体
        # 域名、操作者明确配置的域名或不会再次经 DNS 解析的公网 IP 字面量。
        extra_hosts = {value.strip().lower() for value in os.getenv("MEDIA_EXTRA_HOSTS", "").split(",") if value.strip()}
        trusted_host = host in extra_hosts or any(host == domain or host.endswith("." + domain) for domain in TRUSTED_MEDIA_DOMAINS)
        if not trusted_host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return False
        trusted_fake_dns = parsed.scheme == "https" and parsed.port in (None, 443) and (
            trusted_host
        )
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if trusted_fake_dns and any(ip in network for network in FAKE_NETWORKS):
                continue
            if not ip.is_global or ip.is_multicast:
                return False
        return bool(addresses)
    except (OSError, ValueError, TypeError):
        return False


def media_headers(url):
    host = urlparse(url).hostname or ""
    if any(host == d or host.endswith("." + d) for d in ("bilivideo.com", "bilivideo.cn", "acgvideo.com", "hdslb.com", "biliimg.com", "bilibili.com")):
        referer = "https://www.bilibili.com/"
    elif any(host == d or host.endswith("." + d) for d in ("xhscdn.com", "xiaohongshu.com")):
        referer = "https://www.xiaohongshu.com/"
    elif any(host == d or host.endswith("." + d) for d in ("kwaicdn.com", "kwimgs.com", "yximgs.com", "kuaishou.com")):
        referer = "https://www.kuaishou.com/"
    else:
        referer = "https://www.douyin.com/"
    return {"User-Agent": USER_AGENT, "Referer": referer, "Accept": "*/*", "Accept-Encoding": "identity"}


def safe_filename(filename):
    return re.sub(r'[\r\n\\/:*?"<>|]+', "_", str(filename)).strip()[:180] or "media.mp4"


def _range_valid(value):
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        return False
    start, end = match.groups()
    return not (start and end) or int(start) <= int(end)


async def open_media(url, range_header=None, allow_playlist=False):
    if range_header and not _range_valid(range_header):
        raise MediaError("不支持的 Range 请求", 416)
    client = httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10, read=300))
    response = None
    try:
        current = url
        for _ in range(5):
            if not await asyncio.to_thread(is_safe_media_url, current):
                raise MediaError("仅支持指向公网的 HTTP(S) 媒体链接", 400)
            headers = media_headers(current)
            if range_header:
                headers["Range"] = range_header
            response = await client.send(client.build_request("GET", current, headers=headers), stream=True)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise MediaError("媒体重定向缺少目标")
            current = urljoin(current, location)
        else:
            raise MediaError("媒体重定向次数过多")
        if response.status_code not in (200, 206):
            raise MediaError("媒体下载失败，链接可能已过期", response.status_code)
        content_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
        playlist = content_type in PLAYLIST_TYPES or urlparse(current).path.lower().endswith(".m3u8")
        if playlist and not allow_playlist:
            raise MediaError("该资源是 HLS 清单，请使用合并下载", 400)
        if not (content_type.startswith(("video/", "audio/", "image/")) or content_type == "application/octet-stream" or (allow_playlist and (playlist or content_type == "text/plain"))):
            raise MediaError("上游返回了错误页或非媒体内容")
        length = response.headers.get("content-length")
        if length and (not length.isdecimal() or len(length) > 20 or int(length) > MAX_DOWNLOAD_BYTES):
            raise MediaError("媒体体积超过单次下载上限", 413)
        if length == "0":
            raise MediaError("上游返回了空文件")
        if response.status_code == 206:
            content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("content-range", ""))
            if not content_range:
                raise MediaError("上游返回了无效的 Content-Range")
            start, end = int(content_range.group(1)), int(content_range.group(2))
            total = int(content_range.group(3)) if content_range.group(3) != "*" else None
            if end < start or (total is not None and end >= total) or (length and int(length) != end - start + 1):
                raise MediaError("上游分段字节范围或长度不一致")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                wanted_start, wanted_end = match.groups()
                if wanted_start:
                    if start != int(wanted_start) or (wanted_end and end != (min(int(wanted_end), total - 1) if total is not None else int(wanted_end))):
                        raise MediaError("上游返回的分段与请求不一致")
                elif total is None or start != max(0, total - int(wanted_end)) or end != total - 1:
                    raise MediaError("上游返回的尾部分段与请求不一致")
            elif total is None or start != 0 or end != total - 1:
                raise MediaError("上游仅返回了部分媒体内容")
        return client, response
    except BaseException:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise


def looks_like_error(data):
    prefix = data.lstrip()[:64].lower()
    return prefix.startswith((b"<", b"{", b"["))


async def checked_chunks(response, max_bytes=MAX_DOWNLOAD_BYTES):
    length = response.headers.get("content-length", "")
    expected = int(length) if length.isdecimal() and response.headers.get("content-encoding", "identity") == "identity" else None
    if response.status_code == 206:
        content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(?:\d+|\*)", response.headers.get("content-range", ""))
        if not content_range or int(content_range.group(2)) < int(content_range.group(1)):
            raise MediaError("上游返回了无效的 Content-Range")
        expected = int(content_range.group(2)) - int(content_range.group(1)) + 1
    received = 0
    async for chunk in response.aiter_bytes(chunk_size=65536):
        if not received and looks_like_error(chunk):
            raise MediaError("上游返回了错误页，未保存为媒体文件")
        received += len(chunk)
        if received > max_bytes:
            raise MediaError("媒体体积超过单次下载上限", 413)
        yield chunk
    if expected is not None and received != expected:
        raise httpx.RemoteProtocolError("媒体未完整传输，实际字节数与 Content-Length 不一致")
    if not received:
        raise MediaError("上游返回了空文件")


async def download_response(url, filename="video.mp4", range_header=None):
    client, response = await open_media(url, range_header=range_header)
    chunks = checked_chunks(response)
    try:
        first = await chunks.__anext__()
        if first.lstrip().startswith(b"#EXTM3U"):
            raise MediaError("该资源是 HLS 清单，请使用合并下载", 400)
    except BaseException:
        await chunks.aclose()
        await response.aclose()
        await client.aclose()
        raise

    async def close():
        await response.aclose()
        await client.aclose()

    async def body():
        try:
            yield first
            async for chunk in chunks:
                yield chunk
        finally:
            await chunks.aclose()
            await close()

    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe_filename(filename), safe="")}
    if response.headers.get("content-encoding", "identity") == "identity":
        for key in ("content-length", "content-range", "accept-ranges", "etag", "last-modified"):
            if key in response.headers:
                headers[key] = response.headers[key]
    return StreamingResponse(body(), status_code=response.status_code, media_type=response.headers.get("content-type", "application/octet-stream"), headers=headers, background=BackgroundTask(close))


# 任务只在当前服务进程存活；状态中不公开本地路径或上游签名 URL。
DOWNLOAD_JOBS = {}
MAX_ACTIVE_JOBS = 3
MAX_JOB_ITEMS = 100
MAX_HLS_SEGMENTS = 500
JOB_TTL = 600


def ffmpeg_path():
    configured = os.getenv("FFMPEG_PATH", "").strip()
    if configured:
        if not Path(configured).is_file():
            raise MediaError("配置的 FFmpeg 文件不存在", 503)
        return configured
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise MediaError("音视频合并需要 FFmpeg，请安装运行依赖或设置 FFMPEG_PATH", 503)


def download_job_status(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if job is None:
        raise MediaError("下载任务不存在或已过期", 404)
    result = {key: job[key] for key in ("id", "status", "stage", "filename", "downloaded_bytes", "completed_items", "total_items")}
    if job.get("error"):
        result["error"] = job["error"]
    if job["status"] == "completed":
        result["download_url"] = f"/api/download/jobs/{job_id}/file"
        result["data_size"] = job["output"].stat().st_size
    return result


def _clear_job_files(job, keep_output=False):
    # 只清理当前进程创建并登记的确切临时目录。
    directory = job["directory"].resolve()
    if directory.parent != Path(tempfile.gettempdir()).resolve() or directory.name != "video-api-" + job["id"]:
        raise RuntimeError("unexpected download directory")
    for path in directory.iterdir():
        if keep_output and path == job.get("output"):
            continue
        if path.is_file():
            path.unlink(missing_ok=True)


def remove_download_job(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if job is None:
        return
    if not job["task"].done():
        return
    _clear_job_files(job)
    try:
        job["directory"].rmdir()
    except OSError:
        return
    handle = job.get("cleanup")
    if handle:
        handle.cancel()
    DOWNLOAD_JOBS.pop(job_id, None)


async def save_media(url, destination, job, *, range_header=None, playlist=False, count_item=True):
    client, response = await open_media(url, range_header=range_header, allow_playlist=playlist)
    try:
        if range_header and response.status_code != 206:
            raise MediaError("上游不支持 HLS 分片的 Range 请求")
        if range_header:
            requested = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
            actual = re.fullmatch(r"bytes (\d+)-(\d+)/(?:\d+|\*)", response.headers.get("content-range", ""))
            if not requested or not actual or requested.groups() != actual.groups():
                raise MediaError("HLS 分片的 Content-Range 与请求不一致")
        with destination.open("wb") as output:
            limit = min(MAX_DOWNLOAD_BYTES - job["downloaded_bytes"], 2 * 1024 * 1024 if playlist else MAX_DOWNLOAD_BYTES)
            async for chunk in checked_chunks(response, max_bytes=limit):
                if output.tell() == 0 and not playlist and chunk.lstrip().lower().startswith((b"#extm3u", b"ffconcat version")):
                    raise MediaError("媒体分片中含有未验证的嵌套播放清单", 400)
                output.write(chunk)
                job["downloaded_bytes"] += len(chunk)
                if job["downloaded_bytes"] > MAX_DOWNLOAD_BYTES:
                    raise MediaError("下载任务超过体积上限", 413)
        if count_item:
            job["completed_items"] += 1
        return {"content_type": response.headers.get("content-type", "").split(";", 1)[0], "url": str(response.url)}
    finally:
        await response.aclose()
        await client.aclose()


async def _ffmpeg(arguments, job):
    job["status"], job["stage"] = "processing", "正在合并媒体"
    command = [ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + arguments
    options = {"creationflags": 0x08000000} if os.name == "nt" else {}
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **options)
    try:
        await process.communicate()
        if process.returncode:
            raise MediaError("媒体合并失败，可尝试分别下载原始视频和音轨")
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise


def _hls_attributes(value):
    return {key: quoted if quoted else plain for key, quoted, plain in re.findall(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', value)}


async def _prepare_hls(url, job, prefix="video", depth=0):
    if depth > 2:
        raise MediaError("HLS 子清单层级过深", 400)
    root = job["directory"]
    manifest = root / f"{prefix}-source-{depth}.txt"
    info = await save_media(url, manifest, job, playlist=True, count_item=False)
    url = info["url"]
    text = manifest.read_text(encoding="utf-8-sig")
    if not text.lstrip().startswith("#EXTM3U"):
        raise MediaError("不是有效的 HLS 清单", 400)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants, audio = [], {}
    for i, line in enumerate(lines):
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")) and _hls_attributes(line.split(":", 1)[1]).get("METHOD") != "NONE":
            raise MediaError("暂不处理加密或 DRM HLS", 400)
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _hls_attributes(line.split(":", 1)[1])
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                audio[attrs.get("GROUP-ID")] = urljoin(url, attrs["URI"])
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _hls_attributes(line.split(":", 1)[1])
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                variants.append((int(attrs.get("BANDWIDTH") or 0), urljoin(url, lines[i + 1]), attrs.get("AUDIO")))
    if variants:
        _, video_url, group = max(variants, key=lambda item: item[0])
        video, extra_audio = await _prepare_hls(video_url, job, prefix, depth + 1)
        if group in audio:
            extra_audio, _ = await _prepare_hls(audio[group], job, prefix + "-audio", depth + 1)
        return video, extra_audio
    if "#EXT-X-ENDLIST" not in lines:
        raise MediaError("仅支持有结束标记的点播 HLS，不下载无限直播流", 400)
    segment_count = sum(not line.startswith("#") for line in lines)
    if not segment_count or segment_count > MAX_HLS_SEGMENTS:
        raise MediaError("HLS 分片数量为空或超过上限", 400)
    local_lines, next_range, offsets, count = ["#EXTM3U"], None, {}, 0

    async def segment(uri, byte_range=None, init=False):
        nonlocal count
        source = urljoin(url, uri)
        range_header = None
        if byte_range:
            match = re.fullmatch(r"(\d+)(?:@(\d+))?", byte_range)
            if not match or int(match.group(1)) <= 0:
                raise MediaError("无效 HLS 字节范围", 400)
            size = int(match.group(1))
            if match.group(2) is None and source not in offsets:
                raise MediaError("HLS 分片缺少起始字节偏移", 400)
            start = int(match.group(2)) if match.group(2) else offsets[source]
            range_header = f"bytes={start}-{start + size - 1}"
            offsets[source] = start + size
        name = f"{prefix}-{depth}-{count:05d}." + ("mp4" if init else "ts")
        count += 1
        if count > MAX_HLS_SEGMENTS + 1:
            raise MediaError("HLS 分片数量超过上限", 400)
        job["total_items"] += 1
        await save_media(source, root / name, job, range_header=range_header)
        return name

    for line in lines[1:]:
        if line.startswith("#EXT-X-BYTERANGE:"):
            next_range = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _hls_attributes(line.split(":", 1)[1])
            if not attrs.get("URI"):
                raise MediaError("HLS 初始化分片缺少地址", 400)
            name = await segment(attrs["URI"], attrs.get("BYTERANGE"), init=True)
            local_lines.append(f'#EXT-X-MAP:URI="{name}"')
        elif line.startswith(("#EXTINF:", "#EXT-X-TARGETDURATION:", "#EXT-X-VERSION:", "#EXT-X-MEDIA-SEQUENCE:", "#EXT-X-DISCONTINUITY", "#EXT-X-ENDLIST", "#EXT-X-INDEPENDENT-SEGMENTS")):
            local_lines.append(line)
        elif not line.startswith("#"):
            local_lines.append(await segment(line, next_range))
            next_range = None
    target = root / f"{prefix}-{depth}-local.m3u8"
    target.write_text("\n".join(local_lines) + "\n", encoding="utf-8")
    return target, None


async def _perform_job(job, request):
    root, mode = job["directory"], request["mode"]
    job["status"], job["stage"] = "downloading", "正在下载媒体"
    if mode == "zip":
        suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/avif": ".avif", "video/mp4": ".mp4"}
        files = []
        for index, url in enumerate(request["urls"]):
            path = root / f"resource-{index:03d}"
            info = await save_media(url, path, job)
            media_type = info["content_type"]
            extension = suffixes.get(media_type) or Path(urlparse(url).path).suffix[:10] or ".bin"
            files.append((path, f"{index + 1:03d}{extension}"))
        job["status"], job["stage"] = "processing", "正在打包图集"
        with zipfile.ZipFile(job["output"], "w", compression=zipfile.ZIP_STORED) as archive:
            for path, name in files:
                with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as destination:
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        destination.write(chunk)
                        await asyncio.sleep(0)
    elif mode == "mux":
        video, audio = root / "video.mp4", root / "audio.m4a"
        await save_media(request["url"], video, job)
        await save_media(request["audio_url"], audio, job)
        await _ffmpeg(["-protocol_whitelist", "file,pipe", "-i", str(video), "-protocol_whitelist", "file,pipe", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(job["output"])], job)
    elif mode == "concat":
        paths = []
        for i, url in enumerate(request["urls"]):
            path = root / f"segment-{i:03d}.mp4"
            await save_media(url, path, job)
            paths.append(path)
        manifest = root / "concat.txt"
        manifest.write_text("\n".join(f"file '{p.name}'" for p in paths), encoding="utf-8")
        await _ffmpeg(["-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "1", "-i", str(manifest), "-c", "copy", "-movflags", "+faststart", str(job["output"])], job)
    else:
        video, audio = await _prepare_hls(request["url"], job)
        arguments = ["-protocol_whitelist", "file,pipe", "-allowed_extensions", "ALL", "-i", str(video)]
        if audio:
            arguments += ["-protocol_whitelist", "file,pipe", "-allowed_extensions", "ALL", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
        await _ffmpeg(arguments + ["-c", "copy", "-movflags", "+faststart", str(job["output"])], job)


async def _run_download_job(job, request):
    try:
        await asyncio.wait_for(_perform_job(job, request), timeout=900)
        if not job["output"].is_file() or not job["output"].stat().st_size:
            raise MediaError("媒体处理没有产生有效文件")
        job["status"], job["stage"] = "completed", "文件已准备好"
    except asyncio.CancelledError:
        job["status"], job["stage"] = "cancelled", "下载已取消"
    except MediaError as exc:
        job.update(status="failed", stage="下载失败", error=str(exc))
    except asyncio.TimeoutError:
        job.update(status="failed", stage="下载超时", error="任务超过 15 分钟，请减少资源后重试")
    except Exception:
        job.update(status="failed", stage="下载失败", error="媒体请求或合并失败，请重新解析后重试")
    finally:
        _clear_job_files(job, keep_output=job["status"] == "completed")
        job["cleanup"] = asyncio.get_running_loop().call_later(JOB_TTL, remove_download_job, job["id"])


def create_download_job(request):
    if request.get("mode") not in {"zip", "mux", "concat", "hls"}:
        raise MediaError("不支持的下载任务类型", 400)
    urls = request.get("urls") or []
    if not isinstance(urls, list) or len(urls) > MAX_JOB_ITEMS:
        raise MediaError("资源数量超过上限", 400)
    if request["mode"] in {"zip", "concat"} and not urls:
        raise MediaError("没有可下载的资源", 400)
    sources = urls if request["mode"] in {"zip", "concat"} else [request.get("url")]
    if request["mode"] == "mux":
        sources = sources + [request.get("audio_url")]
    for url in sources:
        try:
            parsed = urlparse(url or "")
            valid = isinstance(url, str) and len(url) <= 8192 and parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid:
            raise MediaError("媒体链接格式不合法", 400)
    if sum(job["status"] in {"queued", "downloading", "processing"} for job in DOWNLOAD_JOBS.values()) >= MAX_ACTIVE_JOBS or len(DOWNLOAD_JOBS) >= 20:
        raise MediaError("下载任务较多，请稍后重试", 429)
    job_id = uuid.uuid4().hex
    directory = Path(tempfile.gettempdir()) / ("video-api-" + job_id)
    directory.mkdir(mode=0o700)
    extension = ".zip" if request["mode"] == "zip" else ".mp4"
    filename = safe_filename(request.get("filename") or "media")
    if not filename.lower().endswith(extension):
        filename += extension
    job = {"id": job_id, "directory": directory, "output": directory / ("output" + extension), "filename": filename, "status": "queued", "stage": "等待下载", "downloaded_bytes": 0, "completed_items": 0, "total_items": len(sources), "created": time.monotonic()}
    DOWNLOAD_JOBS[job_id] = job
    job["task"] = asyncio.create_task(_run_download_job(job, dict(request)))
    return download_job_status(job_id)


async def cancel_download_job(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if job is None:
        raise MediaError("下载任务不存在或已过期", 404)
    if job["status"] in {"completed", "failed", "cancelled"}:
        return download_job_status(job_id)
    job["task"].cancel()
    try:
        await job["task"]
    except asyncio.CancelledError:
        job["status"], job["stage"] = "cancelled", "下载已取消"
        _clear_job_files(job)
        job["cleanup"] = asyncio.get_running_loop().call_later(JOB_TTL, remove_download_job, job_id)
    return download_job_status(job_id)
