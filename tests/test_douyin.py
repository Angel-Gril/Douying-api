import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from curl_cffi.requests import Session

import server
import media_downloads
from browser_parser import _format_result


@pytest.fixture
def detail():
    return {
        "aweme_id": "1234567890123456789",
        "desc": "Sample video",
        "author": {"nickname": "Creator", "unique_id": "creator"},
        "video": {
            "duration": 10000,
            "width": 1280,
            "height": 720,
            "play_addr": {
                "uri": "video_sample",
                "url_list": ["https://media.example/playwm/default.mp4"],
                "width": 1280,
                "height": 720,
            },
            "download_addr": {"url_list": ["https://media.example/watermarked.mp4"]},
            "bit_rate": [
                {
                    "gear_name": "normal_720_0",
                    "bit_rate": 1000000,
                    "FPS": 30,
                    "format": "mp4",
                    "is_h265": 0,
                    "is_bytevc1": 0,
                    "play_addr": {
                        "url_list": ["https://media.example/720.mp4"],
                        "width": 1280,
                        "height": 720,
                        "data_size": 1000000,
                    },
                },
                {
                    "gear_name": "normal_1080_1",
                    "bit_rate": 2000000,
                    "FPS": 60,
                    "format": "mp4",
                    "is_h265": 1,
                    "is_bytevc1": 1,
                    "play_addr": {
                        "url_list": [
                            "https://media.example/1080.mp4",
                            "https://backup.example/1080.mp4",
                        ],
                        "width": 1920,
                        "height": 1080,
                        "data_size": 2000000,
                    },
                },
                {"gear_name": "unavailable", "bit_rate": 9000000, "play_addr": {}},
            ],
        },
    }


def test_detail_returns_all_usable_transcodes_with_matching_metadata(detail):
    before = copy.deepcopy(detail)
    result = server.parse_aweme_detail(detail)
    assert result["url"] == "https://media.example/1080.mp4"
    assert (result["width"], result["height"]) == (1920, 1080)
    assert result["bitrate"] == 2000000
    assert result["quality"] == "normal_1080_1"
    assert len(result["video_backup"]) == 2
    best = result["video_backup"][0]
    assert best["url_list"] == detail["video"]["bit_rate"][1]["play_addr"]["url_list"]
    assert best["data_size"] == 2000000
    assert best["fps"] == 60
    assert best["codec"] == "h265"
    assert best["format"] == "mp4"
    assert detail == before


def test_original_entry_uses_default_ratio_and_media_id(detail):
    result = server.parse_aweme_detail(detail)
    assert result["original_url"] == (
        "https://aweme.snssdk.com/aweme/v1/play/"
        "?video_id=video_sample&ratio=default&line=0"
    )
    detail["video"]["bit_rate"] = []
    detail["video"]["play_addr"]["url_list"] = []
    assert server.parse_aweme_detail(detail)["url"] == result["original_url"]


def test_browser_uses_same_unwatermarked_quality_selection(detail):
    result = _format_result(detail)
    assert result["url"] == "https://media.example/1080.mp4"
    assert result["video_backup"] == server.parse_aweme_detail(detail)["video_backup"]
    assert result["original_url"] == server.parse_aweme_detail(detail)["original_url"]
    assert result["source"] == "browser_intercept"


def test_image_detail_remains_an_image(detail):
    detail["images"] = [{"url_list": ["https://media.example/thumb.jpg", "https://media.example/full.jpg"]}]
    result = server.parse_aweme_detail(detail)
    assert result["type"] == "image"
    assert result["images"] == ["https://media.example/full.jpg"]
    assert "original_url" not in result
    assert "video_backup" not in result


@pytest.fixture
def upstream(monkeypatch, detail):
    state = {"detail": detail, "original_status": 302, "count": 12345, "count_gid": detail["aweme_id"], "requests": []}
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_downloads, "TRUSTED_MEDIA_DOMAINS", media_downloads.TRUSTED_MEDIA_DOMAINS + ("example",))

    def handle(request):
        state["requests"].append(request)
        if request.url.path == "/aweme/v1/web/aweme/detail/":
            if "detail_error" in state:
                raise httpx.ReadTimeout("upstream timeout", request=request)
            if "detail_response" in state:
                return state["detail_response"]
            return httpx.Response(200, json={"status_code": 0, "aweme_detail": state["detail"]})
        if request.url.host == "aweme.snssdk.com":
            assert request.method == "HEAD"
            assert request.url.params["video_id"] == "video_sample"
            assert request.url.params["ratio"] == "default"
            return httpx.Response(state["original_status"], headers={"location": "https://cdn.example/original.mp4"})
        if request.url.host == "cdn.example":
            assert request.headers["referer"] == "https://www.douyin.com/"
            content = state.get("original_bytes")
            size = len(content) if content is not None else 3000000
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-type": state.get("media_type", "video/mp4"), "content-length": str(size)})
            assert request.method == "GET"
            if content is not None:
                start, end = map(int, request.headers["range"][6:].split("-"))
                return httpx.Response(206, content=content[start:end + 1], headers={"content-type": "video/mp4", "content-range": f"bytes {start}-{end}/{size}"})
            return httpx.Response(200, headers={"content-type": "video/mp4", "content-length": str(size)})
        raise AssertionError(f"Unexpected upstream: {request.method} {request.url}")

    def get_count(_session, url, **kwargs):
        state["count_requests"] = state.get("count_requests", 0) + 1
        if state.get("count_error"):
            raise TimeoutError("upstream timeout")
        assert url == f"https://jingxuan.douyin.com/m/video/{detail['aweme_id']}?app=yumme"
        assert "iPhone" in kwargs["headers"]["User-Agent"]
        assert "Cookie" not in kwargs["headers"]
        payload = {"data": {"storeState": {"detail": {
            "relatedVideoForSeo": {"gid": "different", "play_count": 999999},
            "videoData": {"result": {"gid": state["count_gid"], "play_count": state["count"]}},
        }}}}
        return SimpleNamespace(status_code=200, text=f"<script>window._SSR_DATA = {json.dumps(payload)};</script>")

    async def signed(_aweme_id):
        return {"type": "video", "url": "https://legacy.example/video.mp4", "source": "legacy"}

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(server.socket, "getaddrinfo", lambda host, *args, **kwargs: [(2, 1, 6, "", (host if host == "127.0.0.1" else "8.8.8.8", 443))])
    monkeypatch.setattr(Session, "get", get_count)
    monkeypatch.setattr(server, "strategy_web_api_with_signature", signed)
    monkeypatch.setattr(server, "BROWSER_AVAILABLE", False)
    monkeypatch.setattr(server, "DOUYIN_COOKIE", "private-cookie-must-not-be-forwarded")
    state["client"] = real_client
    return state


async def request_parse(state):
    async with state["client"](transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/parse", params={"url": "https://www.douyin.com/video/1234567890123456789"})
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_parse_prefers_cookie_free_detail_and_verified_original(upstream):
    response = await request_parse(upstream)
    result = response["data"]
    assert response["code"] == 200
    assert result["source"] == "web_api_open"
    assert result["url"] == "https://cdn.example/original.mp4"
    assert result["quality"] == "original"
    assert result["data_size"] == 3000000
    assert (result["width"], result["height"], result["bitrate"], result["fps"]) == (0, 0, 0, 0)
    assert result["play_count"] == 12345
    assert len(result["video_backup"]) == 2
    request = upstream["requests"][0]
    assert request.url.params["aweme_id"] == "1234567890123456789"
    assert request.headers["origin"] == "https://open.douyin.com"
    assert request.headers["referer"] == "https://open.douyin.com/"
    assert "cookie" not in request.headers


@pytest.mark.asyncio
async def test_original_failure_preserves_best_transcode_and_real_zero_count(upstream):
    upstream.update(original_status=403, count=0)
    result = (await request_parse(upstream))["data"]
    assert result["url"] == "https://media.example/1080.mp4"
    assert result["quality"] == "normal_1080_1"
    assert result["data_size"] == 2000000
    assert result["play_count"] == 0


@pytest.mark.asyncio
async def test_original_metadata_comes_from_original_mp4_not_the_60fps_transcode(upstream):
    from test_original_metadata import box, moov
    upstream["original_bytes"] = box(b"ftyp", b"isom") + box(b"mdat", b"x" * 200000) + moov(codec=b"hvc1", scale=28000, entries=((196, 1000),), duration=7000)
    payload = await request_parse(upstream)
    result = payload["data"]
    assert result["quality"] == "original"
    assert (result["width"], result["height"], result["codec"], result["fps"]) == (2160, 3840, "h265", 28)
    assert result["duration"] == 7000 and result["metadata_source"] == "mp4_moov"
    assert result["video_backup"][0]["fps"] == 60
    assert result["bitrate_kind"] == "video_average" and result["overall_bitrate"] > 0


@pytest.mark.asyncio
async def test_uri_only_detail_and_failed_original_continue_to_existing_strategy(upstream):
    upstream["detail"]["video"]["bit_rate"] = []
    upstream["detail"]["video"]["play_addr"]["url_list"] = []
    upstream["original_status"] = 403
    response = await request_parse(upstream)
    assert response["code"] == 200
    assert response["data"]["source"] == "legacy"
    assert response["data"]["url"] == "https://legacy.example/video.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize("count,gid", [(None, "1234567890123456789"), (123, "other-video"), (-1, "1234567890123456789")])
async def test_missing_or_unrelated_count_is_not_reported_as_zero(upstream, count, gid):
    upstream.update(count=count, count_gid=gid)
    result = (await request_parse(upstream))["data"]
    assert result["source"] == "web_api_open"
    assert "play_count" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_detail", [None, {}, {"aweme_id": "different", "video": {"play_addr": {"uri": "other"}}}, {"aweme_id": "1234567890123456789", "video": {}}])
async def test_unusable_detail_continues_to_existing_strategy(upstream, bad_detail):
    upstream["detail"] = bad_detail
    response = await request_parse(upstream)
    assert response["code"] == 200
    assert response["data"]["source"] == "legacy"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(403), httpx.Response(200, text=""), httpx.Response(200, json=[]), httpx.Response(200, json={"status_code": 1})])
async def test_detail_http_and_body_failures_use_existing_strategy(upstream, response):
    upstream["detail_response"] = response
    result = (await request_parse(upstream))["data"]
    assert result["source"] == "legacy"


@pytest.mark.asyncio
async def test_detail_timeout_uses_existing_strategy(upstream):
    upstream["detail_error"] = True
    assert (await request_parse(upstream))["data"]["source"] == "legacy"


@pytest.mark.asyncio
async def test_original_html_and_count_timeout_do_not_break_video(upstream):
    upstream.update(media_type="text/html", count_error=True)
    result = (await request_parse(upstream))["data"]
    assert result["url"] == "https://media.example/1080.mp4"
    assert result["quality"] == "normal_1080_1"
    assert "play_count" not in result


@pytest.mark.asyncio
async def test_images_skip_original_probe_and_count_request(upstream):
    upstream["detail"]["images"] = [{"url_list": ["https://media.example/image.jpg"]}]
    result = (await request_parse(upstream))["data"]
    assert result["type"] == "image"
    assert result["images"] == ["https://media.example/image.jpg"]
    assert len(upstream["requests"]) == 1
    assert upstream.get("count_requests", 0) == 0


@pytest.mark.asyncio
async def test_redirect_probe_rejects_private_destinations(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(server.socket, "getaddrinfo", lambda host, *args, **kwargs: [(2, 1, 6, "", (host if host == "127.0.0.1" else "8.8.8.8", 443))])
    assert await server.resolve_video_redirect("https://aweme.snssdk.com/aweme/v1/play/?video_id=sample") is None
    assert len(requests) == 1
