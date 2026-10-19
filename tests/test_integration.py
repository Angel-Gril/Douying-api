import httpx
import pytest

import server


@pytest.mark.asyncio
async def test_bilibili_public_fallback_uses_bilibili_endpoint(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.delenv("BUGPK_API_KEY", raising=False)

    def handle(request):
        assert request.url.path == "/api/bilibili"
        return httpx.Response(200, json={"code": 200, "data": {"title": "Part", "url": "https://media.example/part.mp4", "duration": 2.5, "size": 100}})

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs))
    result = await server.strategy_public_api("https://www.bilibili.com/video/BV1sample/", "bilibili")
    assert result["duration"] == 2500
    assert result["data_size"] == 100


@pytest.mark.asyncio
async def test_bilibili_native_is_used_before_provider(monkeypatch):
    async def native(url):
        return {"type": "video", "url": "https://media.example/bili.mp4", "source": "bilibili_native"}

    async def no_provider(*args):
        raise AssertionError("usable native parsing must not call a provider")

    monkeypatch.setattr(server, "parse_bilibili", native, raising=False)
    monkeypatch.setattr(server, "strategy_public_api", no_provider)
    response = await server.parse_video("https://www.bilibili.com/video/BV1sample/")
    assert response["code"] == 200
    assert response["data"]["source"] == "bilibili_native"


@pytest.mark.asyncio
async def test_local_or_misleading_share_host_does_not_start_requests(monkeypatch):
    async def no_request(*args):
        raise AssertionError("unsupported input must not make a request")

    monkeypatch.setattr(server, "strategy_public_api", no_request)
    response = await server.parse_video("http://127.0.0.1/?url=www.bilibili.com")
    assert response["code"] == 400


def test_douyin_live_photo_keeps_still_motion_pairs_and_statistics():
    detail = {
        "desc": "live", "statistics": {"digg_count": 2, "comment_count": 3},
        "images": [{"url_list": ["https://media.example/still.jpg"], "video": {"play_addr": {"url_list": ["https://media.example/playwm/motion.mp4"]}}}],
    }
    result = server.parse_aweme_detail(detail)
    assert result["type"] == "live"
    assert result["live_photo"] == [{"image": "https://media.example/still.jpg", "video": "https://media.example/play/motion.mp4"}]
    assert result["statistics"]["digg_count"] == 2
