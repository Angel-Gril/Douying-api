import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


class PlaywrightMockTransport:
    """Replace only HTTP responses while exercising the actual page and byte reader."""

    def __init__(self, response, **options):
        self.response, self.options, self.requests = response, options, []
        self.release = threading.Event()
        transport = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, content_type="application/json", status=200, **headers):
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key.replace("_", "-"), value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def record(self):
                parsed = urlparse(self.path)
                payload = None
                if self.command == "POST":
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                transport.requests.append((self.command, parsed.path, parse_qs(parsed.query), payload))
                return parsed.path, payload

            def do_GET(self):
                path, _ = self.record()
                if path == "/":
                    self.reply((Path(__file__).resolve().parents[1] / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/status":
                    self.reply({"status": "running", "cookie_configured": False})
                elif path == "/api/parse":
                    response = transport.response
                    if callable(response):
                        response = response(parse_qs(urlparse(self.path).query)["url"][0])
                    self.reply(response)
                elif path == "/api/download":
                    if transport.options.get("slow"):
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", str(4 * 1024 * 1024))
                        self.end_headers()
                        try:
                            self.wfile.write(b"\x00\x00\x00\x18ftypmp42" + b"x" * (65536 - 12))
                            self.wfile.flush()
                            transport.release.wait(5)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    elif transport.options.get("download_error"):
                        self.reply({"detail": "上游返回错误页面"}, status=502)
                    elif transport.options.get("html_error"):
                        self.reply(b"<html>Access denied</html>", "text/html")
                    elif transport.options.get("huge"):
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", str(1024 * 1024 * 1024))
                        self.end_headers()
                    elif transport.options.get("truncated"):
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b"\x00\x00\x00\x18ftypmp42")
                    elif transport.options.get("disguised_json"):
                        self.reply(b'{"error":"Access denied"}', "video/mp4")
                    else:
                        self.reply(b"\x00\x00\x00\x18ftypmp42", "video/mp4")
                elif path == "/api/download/jobs/job-1/file":
                    self.reply(b"ZIP-or-mux-result", "application/octet-stream", Content_Disposition='attachment; filename="result.mp4"')
                elif path == "/api/download/jobs/job-1":
                    self.reply(transport.job_response())
                else:
                    self.reply({}, status=404)

            def do_POST(self):
                self.record()
                self.reply(transport.job_response())

            def do_DELETE(self):
                self.record()
                self.reply({"code": 200, "data": {"id": "job-1", "status": "cancelled", "stage": "下载已取消"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def job_response(self):
        state = self.options.get("job_state", "completed")
        data = {"id": "job-1", "status": state, "stage": "正在下载媒体", "downloaded_bytes": 65536,
                "completed_items": 1, "total_items": 4, "filename": "result.mp4"}
        if state == "completed":
            data.update(stage="文件已准备好", download_url="/api/download/jobs/job-1/file")
        if state == "failed":
            data.update(stage="下载失败", error="资源不完整，请重新解析")
        return {"code": 200, "data": data}

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


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
        (3840, 2160, 16000000, "h265"), (3840, 2160, 12000000, "h265"),
        (2560, 1440, 8000000, "h264"), (1920, 1080, 6000000, "h264"),
        (1280, 720, 3000000, "h264"), (960, 540, 1500000, "h264"), (640, 360, 800000, "h264"),
    ]):
        variants.append({"quality": f"normal_{height}_0", "url": f"https://media.example/variant-{i}.mp4",
                         "width": width, "height": height, "bit_rate": bitrate, "fps": 60 if i < 2 else 30,
                         "data_size": 3000000 + i * 1000000, "codec": codec, "format": "mp4"})
    return {"code": 200, "platform": "douyin", "data": {
        "type": "video", "title": "画质示例", "quality": "original", "url": "https://media.example/original.mp4",
        "data_size": 12000000, "original_url": "https://media.example/original-unavailable.mp4",
        "author": {"name": "示例作者"}, "source": "web_api_open", "video_backup": variants,
    }}


@pytest.fixture
def video_page(browser, request):
    resources = []

    def open_page(response, width=1000, **options):
        transport = PlaywrightMockTransport(response, **options)
        context = browser.new_context(viewport={"width": width, "height": 1000}, accept_downloads=True)
        resources.append((context, transport))
        page = context.new_page()
        page.transport = transport

        def external(route):
            if route.request.url.startswith(transport.origin + "/"):
                route.continue_()
            else:
                route.fulfill(status=200, content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320"><rect width="240" height="320" fill="#2a2a4a"/></svg>')

        page.route("**/*", external)
        page.goto(transport.origin)
        page.locator("#urlInput").fill("https://www.douyin.com/video/1234567890123456789")
        page.get_by_role("button", name="解析", exact=True).click()
        expect(page.locator("#resultBox .download-button").first).to_be_visible()
        evidence = os.getenv("VIDEO_UI_EVIDENCE")
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination / f"{request.node.name}.png"), full_page=True)
        return page

    yield open_page
    for context, transport in resources:
        context.close()
        transport.close()


def test_all_quality_variants_are_visible_with_metadata(video_page, quality_response):
    page = video_page(quality_response)
    expect(page.locator("#resultBox .download-button")).to_have_count(8)
    for text in ["4K", "2K", "3840×2160", "60 fps", "H.265", "3.00 MB"]:
        expect(page.locator("#resultBox")).to_contain_text(text)
    expect(page.get_by_role("button", name="下载 原画", exact=True)).to_be_visible()


def test_quality_download_uses_selected_variant_url(video_page, quality_response):
    page = video_page(quality_response)
    with page.expect_download() as download:
        page.get_by_role("button", name="下载 720p", exact=True).click()
    request = next(r for r in page.transport.requests if r[1] == "/api/download")
    assert request[2]["url"] == ["https://media.example/variant-4.mp4"]
    assert "720p" in download.value.suggested_filename
    assert "画质示例" in download.value.suggested_filename


def test_quality_list_fits_mobile_and_supports_keyboard(video_page, quality_response):
    page = video_page(quality_response, width=390)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    first = page.get_by_role("button", name="下载 原画", exact=True)
    first.focus()
    expect(first).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#resultBox .download-button").nth(1)).to_be_focused()


def test_transcode_fallback_is_not_labelled_original(video_page, quality_response):
    response = copy.deepcopy(quality_response)
    response["data"].update(url="https://media.example/variant-3.mp4", quality="normal_1080_0", width=1920, height=1080)
    page = video_page(response)
    expect(page.get_by_role("button", name="下载 原画", exact=True)).to_have_count(0)
    expect(page.locator("#resultBox")).to_contain_text("1080p")
    expect(page.locator("#resultBox")).not_to_contain_text("NaN")


def test_upstream_quality_labels_cannot_inject_html(video_page, quality_response):
    response = copy.deepcopy(quality_response)
    response["data"]["video_backup"][0].update(quality='<img src=x onerror="window.qualityXss=1">', width=0, height=0)
    page = video_page(response)
    expect(page.locator("#resultBox img")).to_have_count(0)
    assert page.evaluate("window.qualityXss || 0") == 0


def test_live_photo_pairs_work_without_main_video_and_zip_all_resources(video_page):
    response = {"code": 200, "platform": "xiaohongshu", "data": {"type": "live", "title": "实况相册",
        "images": ["https://media.example/one.jpg", "https://media.example/two.jpg"],
        "live_photo": [{"image": "https://media.example/one.jpg", "video": "https://media.example/one.mp4"},
                       {"image": "https://media.example/two.jpg", "video": "https://media.example/two.mp4"}]}}
    page = video_page(response, width=390)
    expect(page.get_by_role("button", name="下载 实况 1 · 照片", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="下载 实况 1 · 动态视频", exact=True)).to_be_visible()
    expect(page.locator(".download-button")).to_have_count(5)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    with page.expect_download():
        page.get_by_role("button", name="下载 全部打包 ZIP", exact=True).click()
    payload = next(r[3] for r in page.transport.requests if r[0] == "POST")
    assert payload["mode"] == "zip"
    assert set(payload["urls"]) == {"https://media.example/one.jpg", "https://media.example/one.mp4",
                                     "https://media.example/two.jpg", "https://media.example/two.mp4"}


def test_mux_choices_audio_track_and_selected_part_reparse(video_page):
    data = {"type": "video", "title": "分P示例", "url": "https://media.example/video.m4s", "quality": "1080P 高清",
            "requires_mux": True, "audio_url": "https://media.example/audio.m4s",
            "video_backup": [{"url": "https://media.example/hevc.m4s", "quality": "1080P 高清", "codec": "hevc",
                              "requires_mux": True, "audio_url": "https://media.example/audio.m4s"}],
            "audio_backup": [{"url": "https://media.example/audio.m4s", "quality": "AAC", "codec": "aac", "format": "m4a"}],
            "parts": [{"index": 1, "title": "上集", "url": "https://www.bilibili.com/video/BVsample?p=1", "cid": 1, "duration": 10000},
                      {"index": 2, "title": "下集", "url": "https://www.bilibili.com/video/BVsample?p=2", "cid": 2, "duration": 20000}]}

    def response(url):
        selected = copy.deepcopy(data)
        if "p=2" in url:
            selected["title"] = "分P示例 · 下集"
        return {"code": 200, "platform": "bilibili", "data": selected}

    page = video_page(response)
    expect(page.locator("#resultBox")).to_contain_text("合并音视频")
    expect(page.locator("#resultBox")).to_contain_text("音轨下载")
    with page.expect_download():
        page.locator(".download-button").first.click()
    payload = next(r[3] for r in page.transport.requests if r[0] == "POST")
    assert payload["mode"] == "mux"
    assert payload["audio_url"] == data["audio_url"]
    assert payload["url"] == data["url"]
    assert len([r for r in page.transport.requests if r[1] == "/api/parse"]) == 1
    page.get_by_label("选择分 P").select_option("1")
    page.get_by_role("button", name="解析所选分 P", exact=True).click()
    expect(page.locator("#resultBox")).to_contain_text("分P示例 · 下集")
    parse_requests = [r for r in page.transport.requests if r[1] == "/api/parse"]
    assert parse_requests[-1][2]["url"] == [data["parts"][1]["url"]]


@pytest.mark.parametrize("resource,mode", [
    ({"url": "https://media.example/master.m3u8?token=1"}, "hls"),
    ({"url": "https://media.example/one.mp4", "requires_concat": True,
      "segments": ["https://media.example/one.mp4", "https://media.example/two.mp4"]}, "concat"),
])
def test_manifest_and_segment_downloads_use_processing_jobs(video_page, resource, mode):
    page = video_page({"code": 200, "data": {"type": "video", "title": "分段视频", **resource}})
    with page.expect_download():
        page.locator(".download-button").first.click()
    payload = next(r[3] for r in page.transport.requests if r[0] == "POST")
    assert payload["mode"] == mode
    if mode == "concat":
        assert payload["urls"] == resource["segments"]


def test_stream_download_reports_actual_bytes_and_can_cancel(video_page, quality_response):
    page = video_page(quality_response, width=390, slow=True)
    row = page.locator(".download-item").first
    row.locator(".download-button").click()
    expect(row.locator(".download-status")).to_contain_text("64.00 KB")
    expect(row.locator(".download-status")).not_to_contain_text("100%")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if os.getenv("VIDEO_UI_EVIDENCE"):
        page.screenshot(path=str(Path(os.environ["VIDEO_UI_EVIDENCE"]) / "mobile-active-progress.png"), full_page=True)
    row.get_by_role("button", name="取消下载", exact=True).click()
    expect(row.locator(".download-status")).to_contain_text("已取消")
    expect(row.locator(".download-button")).to_be_enabled()


def test_job_progress_and_cancellation_use_server_state(video_page):
    page = video_page({"code": 200, "data": {"type": "video", "title": "合并测试", "url": "https://media.example/v.m4s",
        "requires_mux": True, "audio_url": "https://media.example/a.m4s"}}, job_state="downloading")
    row = page.locator(".download-item").first
    with page.expect_request(lambda request: request.method == "GET" and urlparse(request.url).path == "/api/download/jobs/job-1"):
        row.locator(".download-button").click()
    expect(row.locator(".download-status")).to_contain_text("64.00 KB")
    expect(row.locator(".download-status")).to_contain_text("1/4")
    row.get_by_role("button", name="取消下载", exact=True).click()
    expect(row.locator(".download-status")).to_contain_text("已取消")
    assert any(r[0] == "DELETE" and r[1] == "/api/download/jobs/job-1" for r in page.transport.requests)


@pytest.mark.parametrize("options,error", [({"download_error": True}, "上游返回错误页面"), ({"html_error": True}, "错误页面")])
def test_download_error_does_not_save_a_media_file_and_can_retry(video_page, quality_response, options, error):
    page = video_page(quality_response, **options)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    row = page.locator(".download-item").first
    row.locator(".download-button").click()
    expect(row.locator(".download-status")).to_contain_text(error)
    expect(row.locator(".download-button")).to_be_enabled()
    assert downloads == []


def test_large_response_offers_native_download_without_claiming_completion(video_page, quality_response):
    page = video_page(quality_response, huge=True)
    row = page.locator(".download-item").first
    row.locator(".download-button").click()
    expect(row.locator(".download-status")).to_contain_text("浏览器")
    expect(row.get_by_role("button", name="交给浏览器下载", exact=True)).to_be_visible()
    expect(row.locator(".download-status")).not_to_contain_text("下载完成")


def test_human_quality_label_is_case_insensitive_and_unsafe_media_is_rejected(video_page):
    page = video_page({"code": 200, "data": {"type": "video", "title": "清晰度测试", "quality": "720P 高清",
        "url": "https://media.example/video.mp4", "video_backup": [{"url": "javascript:alert(1)", "quality": "1080P"}]}})
    expect(page.locator("#resultBox")).to_contain_text("720p")
    expect(page.locator(".download-button")).to_have_count(1)


@pytest.mark.parametrize("option,error", [("truncated", "不完整"), ("disguised_json", "JSON")])
def test_incomplete_or_disguised_media_is_rejected(video_page, quality_response, option, error):
    page = video_page(quality_response, **{option: True})
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    row = page.locator(".download-item").first
    row.locator(".download-button").click()
    expect(row.locator(".download-status")).to_contain_text(error)
    expect(row.locator(".download-button")).to_be_enabled()
    assert downloads == []


def test_job_failure_is_visible_and_retry_is_possible(video_page):
    page = video_page({"code": 200, "data": {"title": "失败示例", "url": "https://media.example/v.m3u8"}}, job_state="failed")
    row = page.locator(".download-item").first
    row.locator(".download-button").click()
    expect(row.locator(".download-status")).to_contain_text("资源不完整，请重新解析")
    expect(row.locator(".download-button")).to_be_enabled()


def test_same_url_different_codec_or_bitrate_choices_are_preserved(video_page, quality_response):
    response = copy.deepcopy(quality_response)
    response["data"]["video_backup"][1]["url"] = response["data"]["video_backup"][0]["url"]
    page = video_page(response)
    expect(page.locator(".download-button")).to_have_count(8)


def test_only_real_numeric_statistics_and_millisecond_duration_are_shown(video_page, quality_response):
    response = copy.deepcopy(quality_response)
    response["data"].update(duration=125000, play_count=1234, statistics={"like_count": 12, "comment_count": "99", "share_count": True})
    page = video_page(response)
    expect(page.locator(".work-metadata")).to_contain_text("时长 2:05")
    expect(page.locator(".work-metadata")).to_contain_text("播放 1,234")
    expect(page.locator(".work-metadata")).to_contain_text("点赞 12")
    expect(page.locator(".work-metadata")).not_to_contain_text("评论")
    expect(page.locator(".work-metadata")).not_to_contain_text("分享")
