import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


class Body:
    def __init__(self, content):
        self.content = content
        self.cancelled = False

    def getReader(self):
        body = self
        class Reader:
            sent = False
            async def read(self):
                if self.sent:
                    return types.SimpleNamespace(done=True)
                self.sent = True
                return types.SimpleNamespace(done=False, value=types.SimpleNamespace(to_py=lambda: memoryview(body.content)))
            async def cancel(self):
                body.cancelled = True
            def releaseLock(self):
                pass
        return Reader()

    async def cancel(self):
        self.cancelled = True


class Response:
    def __init__(self, body=None, status=200, headers=None, **kwargs):
        self.body, self.status = body.content if isinstance(body, Body) else body, status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        stream = body if isinstance(body, Body) else Body(body) if isinstance(body, bytes) else None
        self.js_object = types.SimpleNamespace(body=stream)

    @classmethod
    def json(cls, body, **kwargs):
        return cls(body, **kwargs)

    async def text(self):
        return self.body if isinstance(self.body, str) else json.dumps(self.body)


@pytest.fixture
def edge(monkeypatch):
    async def unconfigured(*args, **kwargs):
        raise AssertionError("Unexpected network request")

    sdk = types.SimpleNamespace(WorkerEntrypoint=type("WorkerEntrypoint", (), {}), Response=Response, fetch=unconfigured)
    monkeypatch.setitem(sys.modules, "workers", sdk)
    spec = importlib.util.spec_from_file_location("edge_worker_test", Path(__file__).resolve().parents[1] / "cloudflare/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker = module.Default()
    worker.env = types.SimpleNamespace(CORS_ORIGINS="https://demo.github.io")
    return module, worker


async def call(worker, path, *, method="GET", headers=None):
    request = types.SimpleNamespace(url="https://parser.example" + path, method=method, headers=headers or {})
    return await worker.fetch(request)


@pytest.mark.asyncio
async def test_edge_status_truthfully_describes_limited_capabilities(edge):
    _, worker = edge
    result = await call(worker, "/api/status")
    assert result.body["status"] == "running"
    assert result.body["supported_platforms"] == ["douyin"]
    assert result.body["download_features"]["merge"] is False
    assert result.body["download_features"]["zip"] is False
    assert result.body["browser_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "https://douyin.com.evil.example/video/7345492945006595379", "https://www.douyin.com:8443/video/7345492945006595379", "https://user:password@www.douyin.com/video/7345492945006595379", "https://www.xiaohongshu.com/explore/123"])
async def test_edge_rejects_unsupported_share_hosts_before_fetch(edge, url):
    from urllib.parse import quote
    _, worker = edge
    result = await call(worker, "/api/parse?url=" + quote(url, safe=""))
    assert result.body["code"] == 400


@pytest.mark.asyncio
async def test_edge_reuses_native_detail_with_2k_4k_and_verified_original(edge, monkeypatch):
    from urllib.parse import quote
    module, worker = edge
    requests = []
    detail = {"aweme_id": "7345492945006595379", "desc": "edge sample", "video": {
        "play_addr": {"uri": "sample-video"},
        "bit_rate": [{"gear_name": f"normal_{height}_1", "bit_rate": bitrate,
                      "play_addr": {"url_list": [f"https://video.douyinvod.com/{height}.mp4"], "width": width, "height": height}}
                     for width, height, bitrate in [(2560, 1440, 6000000), (3840, 2160, 16000000)]]}}

    async def fetch(url, **kwargs):
        requests.append((url, kwargs))
        if "aweme/detail/" in url:
            return Response({"status_code": 0, "aweme_detail": detail})
        if "aweme.snssdk.com" in url:
            assert kwargs["method"] == "HEAD"
            return Response(status=302, headers={"location": "https://video.douyinvod.com/original.mp4"})
        assert url == "https://video.douyinvod.com/original.mp4"
        if kwargs.get("method") == "GET":
            return Response(status=200, headers={"content-type": "video/mp4", "content-length": "5000000"})
        return Response(status=200, headers={"content-type": "video/mp4", "content-length": "5000000"})

    monkeypatch.setattr(module, "fetch", fetch)
    result = await call(worker, "/api/parse?url=" + quote("https://www.douyin.com/video/7345492945006595379", safe=""))
    assert result.body["code"] == 200
    data = result.body["data"]
    assert data["url"].endswith("/original.mp4") and data["quality"] == "original"
    assert data["width"] == data["height"] == 0
    assert [(item["width"], item["height"]) for item in data["video_backup"]] == [(3840, 2160), (2560, 1440)]
    assert not any("Cookie" in options.get("headers", {}) for _, options in requests)


@pytest.mark.asyncio
async def test_edge_reads_original_metadata_through_bounded_sdk_streams(edge, monkeypatch):
    from test_original_metadata import box, moov
    module, worker = edge
    content = box(b"ftyp", b"isom") + box(b"mdat", b"x" * 40000) + moov(scale=28000, entries=((196, 1000),), duration=7000)
    bodies = []
    async def fetch(url, **kwargs):
        if "aweme/detail/" in url:
            return Response({"status_code": 0, "aweme_detail": {"aweme_id": "7345492945006595379", "video": {"play_addr": {"uri": "meta-original"}}}})
        if "aweme.snssdk.com" in url:
            return Response(status=302, headers={"location": "https://video.douyinvod.com/meta-original.mp4"})
        if kwargs.get("method") == "HEAD":
            return Response(headers={"content-type": "video/mp4", "content-length": str(len(content))})
        if bodies:
            assert kwargs["headers"]["If-Range"] == '"sample-version"'
        start, end = map(int, kwargs["headers"]["Range"][6:].split("-"))
        response = Response(content[start:end + 1], status=206, headers={"content-range": f"bytes {start}-{end}/{len(content)}", "content-length": str(end - start + 1), "etag": '"sample-version"'})
        bodies.append(response.js_object.body)
        return response
    monkeypatch.setattr(module, "fetch", fetch)
    result = await call(worker, "/api/parse?url=https%3A%2F%2Fwww.douyin.com%2Fvideo%2F7345492945006595379")
    data = result.body["data"]
    assert (data["width"], data["height"], data["codec"], data["fps"]) == (2160, 3840, "h265", 28)
    assert data["duration"] == 7000
    assert len(bodies) == 2 and all(body.cancelled for body in bodies)


@pytest.mark.asyncio
async def test_edge_short_link_never_follows_external_redirect(edge, monkeypatch):
    module, worker = edge
    requests = []
    async def fetch(url, **kwargs):
        requests.append(url)
        return Response(status=302, headers={"location": "http://127.0.0.1/private"})
    monkeypatch.setattr(module, "fetch", fetch)
    result = await call(worker, "/api/parse?url=https%3A%2F%2Fv.douyin.com%2Fsample%2F")
    assert result.body["code"] == 400
    assert requests == ["https://v.douyin.com/sample/"]


@pytest.mark.asyncio
async def test_edge_upstream_block_is_a_failure_not_a_fake_result(edge, monkeypatch):
    module, worker = edge
    async def fetch(url, **kwargs):
        return Response("Access denied", status=403)
    monkeypatch.setattr(module, "fetch", fetch)
    result = await call(worker, "/api/parse?url=https%3A%2F%2Fwww.douyin.com%2Fvideo%2F7345492945006595379")
    assert result.body["code"] == 500 and result.body["data"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://evil.example/video.mp4", "http://127.0.0.1/private", "https://user:password@video.douyinvod.com/video.mp4", "https://video.douyinvod.com:8443/video.mp4"])
async def test_edge_proxy_rejects_untrusted_media_before_fetch(edge, url):
    from urllib.parse import quote
    _, worker = edge
    result = await call(worker, "/api/download?url=" + quote(url, safe=""))
    assert result.status == 400


@pytest.mark.asyncio
async def test_edge_proxy_streams_media_with_range_without_forwarding_credentials(edge, monkeypatch):
    module, worker = edge
    async def fetch(url, **kwargs):
        headers = {name.lower(): value for name, value in kwargs["headers"].items()}
        assert headers["range"] == "bytes=0-3"
        assert headers["referer"] == "https://www.douyin.com/"
        assert "cookie" not in headers and "authorization" not in headers
        return Response(b"part", status=206, headers={"content-type": "video/mp4", "content-length": "4", "content-range": "bytes 0-3/100"})
    monkeypatch.setattr(module, "fetch", fetch)
    result = await call(worker, "/api/download?url=https%3A%2F%2Fvideo.douyinvod.com%2Fvideo.mp4", headers={"range": "bytes=0-3", "cookie": "private", "authorization": "private"})
    assert result.status == 206 and result.body == b"part"
    assert result.headers["content-range"] == "bytes 0-3/100"


@pytest.mark.asyncio
async def test_edge_proxy_rejects_html_and_does_not_follow_private_media_redirect(edge, monkeypatch):
    module, worker = edge
    for response in [Response("<html>error</html>", headers={"content-type": "text/html", "content-length": "18"}), Response(status=302, headers={"location": "http://127.0.0.1/private"})]:
        requests = []
        async def fetch(url, **kwargs):
            requests.append(url)
            return response
        monkeypatch.setattr(module, "fetch", fetch)
        result = await call(worker, "/api/download?url=https%3A%2F%2Fvideo.douyinvod.com%2Fvideo.mp4")
        assert result.status in {400, 502}
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_edge_media_follows_first_party_douyin_redirect_to_verified_cdn(edge, monkeypatch):
    module, worker = edge
    requests = []
    async def fetch(url, **kwargs):
        requests.append(url)
        if url.startswith("https://www.douyin.com/"):
            return Response(status=302, headers={"location": "https://video.douyinvod.com/redirected.mp4"})
        return Response(b"part", headers={"content-type": "video/mp4", "content-length": "4"})
    monkeypatch.setattr(module, "fetch", fetch)
    result = await call(worker, "/api/download?url=https%3A%2F%2Fwww.douyin.com%2Faweme%2Fv1%2Fplay%2F%3Fvideo_id%3Dsample")
    assert result.status == 200 and result.body == b"part"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_edge_cors_and_unsupported_jobs_are_explicit(edge):
    _, worker = edge
    blocked = await call(worker, "/api/status", headers={"origin": "https://evil.example"})
    assert blocked.status == 403
    allowed = await call(worker, "/api/status", method="OPTIONS", headers={"origin": "https://demo.github.io"})
    assert allowed.status == 204
    assert allowed.headers["access-control-allow-origin"] == "https://demo.github.io"
    unsupported = await call(worker, "/api/download/jobs", method="POST")
    assert unsupported.status == 501 and unsupported.body["code"] == 501
