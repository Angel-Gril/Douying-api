import httpx
import pytest

import server
import media_downloads as downloads


@pytest.fixture
def media_transport(monkeypatch):
    real_client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
        monkeypatch.setattr(server.socket, "getaddrinfo", lambda host, *args, **kwargs: [(2, 1, 6, "", (host if host == "127.0.0.1" else "8.8.8.8", 443))])
        return real_client

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("url,referer", [
    ("https://upos.example.bilivideo.com/video.mp4", "https://www.bilibili.com/"),
    ("https://sns-video.example.xhscdn.com/video.mp4", "https://www.xiaohongshu.com/"),
])
async def test_download_uses_media_platform_referer(media_transport, url, referer):
    def handle(request):
        assert request.headers["referer"] == referer
        return httpx.Response(200, content=b"video", headers={"content-type": "video/mp4"})

    media_transport(handle)
    response = await server.proxy_download(url=url, filename="sample.mp4")
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"video"


@pytest.mark.asyncio
async def test_download_forwards_range_and_partial_response(media_transport):
    def handle(request):
        assert request.headers["range"] == "bytes=0-3"
        return httpx.Response(206, content=b"ftyp", headers={
            "content-type": "video/mp4", "content-range": "bytes 0-3/100", "accept-ranges": "bytes",
        })

    real_client = media_transport(handle)
    async with real_client(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/download", params={"url": "https://8.8.8.8/sample.mp4"}, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert response.content == b"ftyp"


@pytest.mark.asyncio
async def test_download_does_not_save_html_error_as_video(media_transport):
    media_transport(lambda request: httpx.Response(200, text="<html>Access denied</html>"))
    response = await server.proxy_download(url="https://8.8.8.8/sample.mp4", filename="sample.mp4")
    assert isinstance(response, dict)
    assert response["code"] == 502


@pytest.mark.asyncio
async def test_download_checks_actual_length(media_transport):
    media_transport(lambda request: httpx.Response(200, content=b"short", headers={"content-type": "video/mp4", "content-length": "100"}))
    response = await server.proxy_download(url="https://8.8.8.8/sample.mp4", filename="sample.mp4")
    with pytest.raises(httpx.RemoteProtocolError):
        _ = [chunk async for chunk in response.body_iterator]


def test_arbitrary_domain_cannot_use_dns_rebinding_to_reach_downloads(monkeypatch):
    lookups = []
    def dns(host, *args, **kwargs):
        lookups.append(host)
        return [(2, 1, 6, "", ("8.8.8.8", 443))]
    monkeypatch.setattr(downloads.socket, "getaddrinfo", dns)
    assert downloads.is_safe_media_url("https://rebind.example/secret.mp4") is False
    assert lookups == []
