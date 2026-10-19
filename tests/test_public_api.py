import httpx
import pytest

import public_api


@pytest.fixture(autouse=True)
def clean_gateway_environment(monkeypatch):
    monkeypatch.delenv("BUGPK_API_KEY", raising=False)
    monkeypatch.delenv("BUGPK_API_BASE_URL", raising=False)


def transport(monkeypatch, handle):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(public_api.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,path", [
    ("douyin", "/api/douyin"), ("doubao", "/api/dbvideos"), ("xiaohongshu", "/api/xhsjx"),
    ("kuaishou", "/api/ksjx"), ("bilibili", "/api/bilibili"), ("weibo", "/api/weibo"),
    ("toutiao", "/api/toutiao"), ("pipixia", "/api/pipixia"), ("pipigx", "/api/pipigx"),
    ("zuiyou", "/api/zuiyou"), ("unknown", "/api/short_videos"),
])
async def test_platform_uses_correct_old_site_and_normalizes_response(monkeypatch, platform, path):
    requests = []
    share_url = "https://share.example/video?a=1&b=two"
    def handle(request):
        requests.append(request)
        assert request.url.host == "api.bugpk.com"
        assert request.url.path == path
        assert request.url.params["url"] == share_url
        assert "x-api-key" not in request.headers
        assert 0 < request.extensions["timeout"]["read"] <= 15
        return httpx.Response(200, json={"code": 200, "data": {"url": "https://cdn.example/v.mp4", "duration": 2}})
    transport(monkeypatch, handle)
    result = await public_api.parse_public_url(share_url, platform)
    assert result["duration"] == 2000
    assert result["source"] == "public_api"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,path", [("douyin", "/api/dyjx"), ("bilibili", "/api/svparse"), ("unknown", "/api/svparse")])
async def test_optional_key_uses_configured_gateway_header_only(monkeypatch, platform, path):
    monkeypatch.setenv("BUGPK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("BUGPK_API_BASE_URL", "https://api-new.bugpk.com/")
    def handle(request):
        assert request.url.host == "api-new.bugpk.com"
        assert request.url.path == path
        assert request.headers["X-API-Key"] == "test-key-not-real"
        assert "key" not in request.url.params
        return httpx.Response(200, json={"code": 200, "data": {"url": "https://cdn.example/v.mp4"}})
    transport(monkeypatch, handle)
    assert await public_api.parse_public_url("https://share.example/v", platform)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_gateway_failure_falls_back_once_without_forwarding_key(monkeypatch, status):
    monkeypatch.setenv("BUGPK_API_KEY", "test-key-not-real")
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.host == "api-new.ifphp.com":
            return httpx.Response(status)
        assert request.url.host == "api.bugpk.com"
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"code": 200, "data": {"url": "https://cdn.example/v.mp4"}})
    transport(monkeypatch, handle)
    assert await public_api.parse_public_url("https://share.example/v", "weibo")
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://evil.example", "http://api-new.ifphp.com", "https://api-new.ifphp.com.evil.example",
                                  "https://user:password@api-new.ifphp.com", "https://api-new.ifphp.com:8443",
                                  "https://api-new.ifphp.com/api", "https://api-new.ifphp.com?key=leak"])
async def test_bad_gateway_configuration_cannot_forward_credentials(monkeypatch, base):
    monkeypatch.setenv("BUGPK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("BUGPK_API_BASE_URL", base)
    def handle(request):
        assert request.url.host == "api.bugpk.com"
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"code": 400})
    transport(monkeypatch, handle)
    assert await public_api.parse_public_url("https://share.example/v", "douyin") is None


@pytest.mark.asyncio
async def test_gateway_redirect_is_never_followed_with_credentials(monkeypatch):
    monkeypatch.setenv("BUGPK_API_KEY", "test-key-not-real")
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.host == "api-new.ifphp.com":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, json={"code": 400})
    transport(monkeypatch, handle)
    assert await public_api.parse_public_url("https://share.example/v", "douyin") is None
    assert [request.url.host for request in requests] == ["api-new.ifphp.com", "api.bugpk.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "json", "null", "list", "empty", "business_error"])
async def test_invalid_provider_responses_fail_cleanly_without_logging_secrets(monkeypatch, capsys, failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-in-error-url", request=request)
        if failure == "json":
            return httpx.Response(200, text="not-json secret-response")
        payload = {"null": {"code": 200, "data": None}, "list": [], "empty": {"code": 200, "data": {}},
                   "business_error": {"code": 400, "data": {"url": "https://cdn.example/v.mp4"}}}[failure]
        return httpx.Response(200, json=payload)
    transport(monkeypatch, handle)
    assert await public_api.parse_public_url("https://share.example/v?secret=private", "douyin") is None
    output = capsys.readouterr()
    assert not output.out and not output.err


@pytest.mark.asyncio
async def test_bilibili_part_urls_can_use_requested_page_context(monkeypatch):
    def handle(request):
        return httpx.Response(200, json={"code": 200, "data": {"url": "https://cdn.example/v.mp4", "videos": [
            {"title": "first", "index": 1}, {"title": "second", "index": 2},
        ]}})
    transport(monkeypatch, handle)
    result = await public_api.parse_public_url("https://www.bilibili.com/video/BV1GJ411x7h7/?p=2", "bilibili")
    assert result["parts"][0]["url"] == "https://www.bilibili.com/video/BV1GJ411x7h7/?p=1"
    assert result["parts"][1]["url"].endswith("?p=2")
