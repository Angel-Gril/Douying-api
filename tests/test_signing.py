import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from curl_cffi.requests import Session

import cookie_manager
import server
from abogus import ABogus


def test_abogus_ua_digest_depends_on_request_ua():
    first = ABogus(user_agent="Chrome/request-a", platform="Win32", browser_info="1920|1080|Win32")
    second = ABogus(user_agent="Chrome/request-b", platform="Win32", browser_info="1920|1080|Win32")
    assert first.ua_code != second.ua_code
    assert first.browser == "1920|1080|Win32"


@pytest.mark.asyncio
async def test_manual_cookie_does_not_launch_browser_and_preserves_identity(monkeypatch):
    monkeypatch.setattr(server, "DOUYIN_COOKIE", "UIFID=device; s_v_web_id=fp_device; msToken=token")

    async def no_browser():
        raise AssertionError("manual Cookie must not bootstrap a browser")

    captured = {}

    def fake_get(_session, url, **kwargs):
        captured.update(url=url, headers=kwargs["headers"])
        return SimpleNamespace(text=json.dumps({"status_code": 1}))

    monkeypatch.setattr(cookie_manager, "get_cookies", no_browser)
    monkeypatch.setattr(Session, "get", fake_get)
    await server.strategy_web_api_with_signature("1234567890123456789")
    from urllib.parse import parse_qs, urlparse
    params = parse_qs(urlparse(captured["url"]).query)
    assert params["uifid"] == ["device"]
    assert params["verifyFp"] == ["fp_device"]
    assert params["msToken"] == ["token"]
    assert params["request_source"] == ["600"]


@pytest.mark.asyncio
async def test_cookie_refresh_is_shared_between_concurrent_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(cookie_manager, "_cached", None)
    monkeypatch.setattr(cookie_manager, "COOKIE_FILE", tmp_path / "test-cookies.json")
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0)
        return {"cookie_str": "synthetic=sample", "names": ["synthetic"], "time": time.time(), "fingerprint": {"user_agent": "consistent"}}

    monkeypatch.setattr(cookie_manager, "_fetch_cookies_via_browser", fetch)
    results = await asyncio.gather(cookie_manager.get_cookies(), cookie_manager.get_cookies())
    assert len(calls) == 1
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_status_does_not_refresh_expired_cookie(monkeypatch, tmp_path):
    cache = tmp_path / "cookie-status.json"
    cache.write_text(json.dumps({"time": 0, "names": ["ttwid"]}), encoding="utf-8")
    monkeypatch.setattr(cookie_manager, "COOKIE_FILE", cache)
    monkeypatch.setattr(server, "DOUYIN_COOKIE", "")
    async def no_refresh():
        raise AssertionError("status checks must not start a browser")
    monkeypatch.setattr(cookie_manager, "get_cookies", no_refresh)
    result = await server.status()
    assert result["cookie_status"] == "expired"
    assert result["cookie_configured"] is False


@pytest.mark.asyncio
async def test_debug_uses_same_manual_cookie_request_builder(monkeypatch):
    monkeypatch.setattr(server, "ENABLE_DEBUG_ENDPOINTS", True)
    monkeypatch.setattr(server, "DOUYIN_COOKIE", "UIFID=debug-device; s_v_web_id=debug-fp")
    async def no_browser():
        raise AssertionError("manual debug requests must not start a browser")
    captured = {}
    def fetch(_session, url, **kwargs):
        captured["url"] = url
        return SimpleNamespace(text=json.dumps({"status_code": 0, "aweme_detail": {"video": {"play_addr": {"uri": "video"}}}}))
    monkeypatch.setattr(cookie_manager, "get_cookies", no_browser)
    monkeypatch.setattr(Session, "get", fetch)
    result = await server.debug_video_data("1234567890123456789")
    assert result["aweme_id"] == "1234567890123456789"
    from urllib.parse import parse_qs, urlparse
    params = parse_qs(urlparse(captured["url"]).query)
    assert params["uifid"] == ["debug-device"]
    assert params["request_source"] == ["600"]


@pytest.mark.asyncio
async def test_manual_cookie_failure_does_not_invalidate_unrelated_auto_cache(monkeypatch):
    monkeypatch.setattr(server, "DOUYIN_COOKIE", "UIFID=manual-device")
    invalidations = []
    monkeypatch.setattr(cookie_manager, "invalidate", lambda: invalidations.append(1))
    monkeypatch.setattr(Session, "get", lambda *args, **kwargs: SimpleNamespace(text=""))
    assert await server.strategy_web_api_with_signature("1234567890123456789") is None
    assert invalidations == []
