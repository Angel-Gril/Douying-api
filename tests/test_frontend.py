import copy
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def quality_response():
    variants = []
    for i, (width, height, bitrate, codec) in enumerate([
        (3840, 2160, 16000000, "h265"),
        (3840, 2160, 12000000, "h265"),
        (2560, 1440, 8000000, "h264"),
        (1920, 1080, 6000000, "h264"),
        (1280, 720, 3000000, "h264"),
        (960, 540, 1500000, "h264"),
        (640, 360, 800000, "h264"),
    ]):
        variants.append({
            "quality": f"normal_{height}_0",
            "url": f"https://media.example/variant-{i}.mp4",
            "width": width,
            "height": height,
            "bit_rate": bitrate,
            "fps": 60 if i < 2 else 30,
            "data_size": 3000000 + i * 1000000,
            "codec": codec,
            "format": "mp4",
        })
    return {"code": 200, "platform": "douyin", "data": {
        "type": "video", "title": "画质示例", "quality": "original",
        "url": "https://media.example/original.mp4", "data_size": 12000000,
        "original_url": "https://aweme.snssdk.com/aweme/v1/play/?video_id=sample&ratio=default&line=0",
        "author": {"name": "示例作者"}, "source": "web_api_open",
        "video_backup": variants,
    }}


@pytest.fixture
def video_page(browser, request):
    contexts = []

    def open_page(response, width=1000):
        context = browser.new_context(viewport={"width": width, "height": 1000})
        contexts.append(context)
        page = context.new_page()
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")

        def route_request(route):
            path = urlparse(route.request.url).path
            if path == "/":
                route.fulfill(content_type="text/html; charset=utf-8", body=html)
            elif path == "/api/status":
                route.fulfill(json={"status": "running", "cookie_configured": False})
            elif path == "/api/parse":
                route.fulfill(content_type="application/json", body=json.dumps(response))
            elif path == "/api/download":
                route.fulfill(content_type="video/mp4", body=b"\x00\x00\x00\x18ftypmp42")
            else:
                route.fulfill(status=404)

        page.route("http://video.test/**", route_request)
        page.goto("http://video.test/")
        page.locator("#urlInput").fill("https://www.douyin.com/video/1234567890123456789")
        page.get_by_role("button", name="解析", exact=True).click()
        expect(page.locator("#resultBox")).to_contain_text("画质示例")
        evidence = os.getenv("VIDEO_UI_EVIDENCE")
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination / f"{request.node.name}.png"), full_page=True)
        return page

    yield open_page
    for context in contexts:
        context.close()


def test_all_quality_variants_are_visible_with_metadata(video_page, quality_response):
    page = video_page(quality_response)
    expect(page.locator("#resultBox button")).to_have_count(8)
    expect(page.locator("#resultBox")).to_contain_text("4K")
    expect(page.locator("#resultBox")).to_contain_text("2K")
    expect(page.locator("#resultBox")).to_contain_text("3840×2160")
    expect(page.locator("#resultBox")).to_contain_text("60 fps")
    expect(page.locator("#resultBox")).to_contain_text("H.265")
    expect(page.locator("#resultBox")).to_contain_text("3.00 MB")
    expect(page.get_by_role("button", name="下载 原画", exact=True)).to_be_visible()


def test_quality_download_uses_selected_variant_url(video_page, quality_response):
    page = video_page(quality_response)
    button = page.get_by_role("button", name="下载 720p", exact=True)
    with page.expect_request("**/api/download?**") as download:
        button.click()
    query = parse_qs(urlparse(download.value.url).query)
    assert query["url"] == ["https://media.example/variant-4.mp4"]
    assert "720p" in query["filename"][0]
    assert "画质示例" in query["filename"][0]
    expect(button).to_be_disabled()


def test_quality_list_fits_mobile_and_supports_keyboard(video_page, quality_response):
    page = video_page(quality_response, width=390)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    first = page.get_by_role("button", name="下载 原画", exact=True)
    first.focus()
    expect(first).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#resultBox button").nth(1)).to_be_focused()


def test_transcode_fallback_is_not_labelled_original(video_page, quality_response):
    response = copy.deepcopy(quality_response)
    response["data"].update(url="https://media.example/variant-3.mp4", quality="normal_1080_0", width=1920, height=1080)
    page = video_page(response)
    expect(page.get_by_role("button", name="下载 原画", exact=True)).to_have_count(0)
    expect(page.locator("#resultBox")).to_contain_text("1080p")
    expect(page.locator("#resultBox")).not_to_contain_text("NaN")


def test_upstream_quality_labels_cannot_inject_html(video_page, quality_response):
    response = copy.deepcopy(quality_response)
    response["data"]["video_backup"][0].update(
        quality='<img src=x onerror="window.qualityXss=1">', width=0, height=0,
    )
    page = video_page(response)
    expect(page.locator("#resultBox img")).to_have_count(0)
    assert page.evaluate("window.qualityXss || 0") == 0
