import httpx
import pytest
from fastapi.responses import StreamingResponse

import server
from server import _is_safe_download_url, detect_platform, extract_aweme_id


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.douyin.com/video/1234567890123456789", "douyin"),
        ("https://www.xiaohongshu.com/explore/abcdef", "xiaohongshu"),
        ("https://v.kuaishou.com/example", "kuaishou"),
        ("https://www.bilibili.com/video/BV1xx", "bilibili"),
    ],
)
def test_detect_platform(url, expected):
    assert detect_platform(url) == expected


def test_extract_aweme_id_from_video_url():
    assert extract_aweme_id("https://www.douyin.com/video/1234567890123456789") == "1234567890123456789"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/video.mp4",
        "http://127.0.0.1/video.mp4",
        "http://[::1]/video.mp4",
        "https://user:pass@example.com/video.mp4",
    ],
)
def test_download_url_rejects_unsafe_targets(url):
    assert _is_safe_download_url(url) is False


def test_download_url_accepts_public_https_host():
    # Use a literal public IP so the test does not depend on the runner's DNS.
    assert _is_safe_download_url("https://8.8.8.8/video.mp4") is True


@pytest.mark.parametrize("address", ["198.18.0.13", "2001:2::88"])
@pytest.mark.parametrize("host", ["aweme.snssdk.com", "v11-weba.douyinvod.com", "v5-dy-ov-experiment.zjcdn.com"])
def test_known_douyin_https_media_supports_proxy_fake_dns(monkeypatch, address, host):
    monkeypatch.setattr("server.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))])
    assert _is_safe_download_url(f"https://{host}/video.mp4") is True


@pytest.mark.parametrize(
    "url,address",
    [
        ("https://unknown.example/video.mp4", "198.18.0.13"),
        ("http://v11-weba.douyinvod.com/video.mp4", "198.18.0.13"),
        ("https://v11-weba.douyinvod.com:8443/video.mp4", "198.18.0.13"),
        ("https://v11-weba.douyinvod.com.evil.example/video.mp4", "198.18.0.13"),
        ("https://evilzjcdn.com/video.mp4", "198.18.0.13"),
        ("https://198.18.0.13/video.mp4", "198.18.0.13"),
        ("https://v11-weba.douyinvod.com/video.mp4", "127.0.0.1"),
        ("https://v11-weba.douyinvod.com/video.mp4", "10.0.0.1"),
        ("https://v11-weba.douyinvod.com/video.mp4", "2001:db8::1"),
    ],
)
def test_proxy_fake_dns_does_not_allow_arbitrary_or_private_targets(monkeypatch, url, address):
    monkeypatch.setattr("server.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))])
    assert _is_safe_download_url(url) is False


@pytest.mark.asyncio
async def test_download_proxy_returns_attachment_with_media_bytes(monkeypatch):
    real_client = httpx.AsyncClient
    content = b"\x00\x00\x00\x18ftypmp42"

    def handle(request):
        assert request.headers["referer"] == "https://www.douyin.com/"
        return httpx.Response(200, content=content, headers={"content-type": "video/mp4"})

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    response = await server.proxy_download("https://8.8.8.8/video.mp4", filename="sample_720p.mp4")
    assert isinstance(response, StreamingResponse)
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''sample_720p.mp4"
    assert response.media_type == "video/mp4"
    assert b"".join([chunk async for chunk in response.body_iterator]) == content


@pytest.mark.asyncio
async def test_download_proxy_rejects_redirect_to_private_host(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    response = await server.proxy_download("https://8.8.8.8/video.mp4", filename="sample.mp4")
    assert response["code"] == 400
    assert len(requests) == 1
