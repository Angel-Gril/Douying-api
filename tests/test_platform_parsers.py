import json

import httpx
import pytest

import platform_parsers as parsers


NOTE_ID = "0123456789abcdef01234567"
PHOTO_ID = "3xsynthetic123"


def script(name, state):
    return f"<script>window.{name}={json.dumps(state)};</script>"


def xhs_html(note, name="__INITIAL_STATE__"):
    if name == "__INITIAL_SSR_STATE__":
        state = {"noteData": {"data": {"noteData": note}}}
    else:
        state = {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}
    return script(name, state)


def xhs_stream(url, width, height, bitrate, **extra):
    return {"masterUrl": url, "width": width, "height": height, "avgBitrate": bitrate, **extra}


def mock_http(monkeypatch, handle):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(parsers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))


def test_xhs_all_codecs_keep_their_own_dimensions_size_and_backups():
    result = parsers._parse_xhs_html(xhs_html({
        "type": "video", "title": "synthetic", "user": {"nickname": "author", "userId": 27},
        "video": {"media": {"stream": {
            "h264": [xhs_stream("https://media.example/low.mp4", "720", "1280", "1000000", size="1000"),
                     xhs_stream("https://media.example/high.mp4", "1080", "1920", "3800000", size="9200", fps="29.97", duration="12500", format="mp4", backupUrls=["https://backup.example/high.mp4"])],
            "h265": [xhs_stream("https://media.example/hevc.mp4", 1080, 1920, 4000000, size=8300)],
            "av1": [xhs_stream("https://media.example/av1.mp4", 720, 1280, 900000, size=900)],
        }}},
        "interactInfo": {"likedCount": "12", "commentCount": 3, "collectedCount": None, "shareCount": "1.2万"},
    }), NOTE_ID)
    assert result["source"] == "xiaohongshu_native"
    assert result["url"] == "https://media.example/high.mp4"
    assert result["author"] == {"name": "author", "id": "27", "avatar": ""}
    assert result["duration"] == 12500
    assert (result["width"], result["height"], result["data_size"], result["bit_rate"]) == (1080, 1920, 9200, 3800000)
    variants = {item["url"]: item for item in result["video_backup"]}
    assert len(variants) == 4
    assert variants[result["url"]]["url_list"] == [result["url"], "https://backup.example/high.mp4"]
    assert variants[result["url"]]["fps"] == 29.97
    assert variants[result["url"]]["format"] == "mp4"
    assert variants["https://media.example/low.mp4"]["width"] == 720
    assert variants["https://media.example/hevc.mp4"]["data_size"] == 8300
    assert {item["codec"] for item in variants.values()} == {"h264", "h265", "av1"}
    assert result["statistics"] == {"like_count": 12, "comment_count": 3}


def test_xhs_real_resolution_beats_codec_or_bitrate_preferences():
    result = parsers._parse_xhs_html(xhs_html({"type": "video", "video": {"media": {"stream": {
        "h264": [xhs_stream("https://media.example/1080.mp4", 1080, 1920, 5000000)],
        "h265": [xhs_stream("https://media.example/720.mp4", 720, 1280, 9000000)],
        "av1": [xhs_stream("https://media.example/4k.mp4", 2160, 3840, 3000000)],
    }}}}), NOTE_ID)
    assert result["url"] == "https://media.example/4k.mp4"
    assert result["height"] == 3840


def test_xhs_ssr_media_v2_preserves_unknown_metadata():
    result = parsers._parse_xhs_html(xhs_html({
        "type": "video", "video": {"mediaV2": {"stream": {"h264": [{"master_url": "https://media.example/unknown.mp4"}]}}},
    }, "__INITIAL_SSR_STATE__"), NOTE_ID)
    assert result["url"] == "https://media.example/unknown.mp4"
    assert result["duration"] == 0
    assert all(result.get(key, 0) == 0 for key in ["width", "height", "fps", "data_size", "bit_rate"])
    assert result.get("statistics", {}) == {}


def test_xhs_multiple_live_photos_and_still_images_remain_paired():
    result = parsers._parse_xhs_html(xhs_html({"type": "normal", "imageList": [
        {"urlDefault": "https://images.example/one.jpg", "stream": {"h264": [{"masterUrl": "https://media.example/one.mp4"}]}},
        {"url": "https://images.example/two.jpg", "videoInfo": {"media": {"stream": {"h265": [{"masterUrl": "https://media.example/two.mp4"}]}}}},
        {"urlDefault": "https://images.example/still.jpg"},
    ]}), NOTE_ID)
    assert result["type"] == "live"
    assert result["images"] == ["https://images.example/one.jpg", "https://images.example/two.jpg", "https://images.example/still.jpg"]
    assert result["live_photo"] == [
        {"image": "https://images.example/one.jpg", "video": "https://media.example/one.mp4"},
        {"image": "https://images.example/two.jpg", "video": "https://media.example/two.mp4"},
    ]


def test_xhs_undefined_is_only_replaced_outside_json_strings():
    html = xhs_html({"type": "normal", "title": "undefined stays in text", "user": None, "imageList": [{"url": "https://images.example/a.jpg"}]})
    html = html.replace('"user": null', '"user": undefined')
    result = parsers._parse_xhs_html(html, NOTE_ID)
    assert result["title"] == "undefined stays in text"
    assert result["author"] == {"name": "", "id": "", "avatar": ""}


def test_xhs_live_photo_direct_video_info_is_distinct_from_still_image_url():
    result = parsers._parse_xhs_html(xhs_html({"type": "normal", "imageList": [
        {"url": "https://images.example/live.jpg", "videoInfo": {"url": "https://media.example/live.mp4"}},
        {"url": "https://images.example/still.jpg"},
    ]}), NOTE_ID)
    assert result["type"] == "live"
    assert result["live_photo"] == [{"image": "https://images.example/live.jpg", "video": "https://media.example/live.mp4"}]


@pytest.mark.parametrize("state", [{}, {"note": None}, {"note": {"noteDetailMap": None}}, {"note": {"noteDetailMap": {NOTE_ID: {"note": {}}}}}])
def test_xhs_missing_or_null_state_returns_none(state):
    assert parsers._parse_xhs_html(script("__INITIAL_STATE__", state), NOTE_ID) is None


@pytest.mark.asyncio
async def test_xhs_short_link_preserves_token_and_scopes_configured_cookie(monkeypatch):
    monkeypatch.setenv("XHS_COOKIE", "synthetic=session")
    seen = []
    def handle(request):
        seen.append(request)
        if request.url.host == "xhslink.com":
            assert "cookie" not in request.headers
            return httpx.Response(302, headers={"location": f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=token%2Bwith%2Fslash%3D"})
        assert request.headers["cookie"] == "synthetic=session"
        assert request.url.params["xsec_token"] == "token+with/slash="
        return httpx.Response(200, text=xhs_html({"type": "normal", "imageList": [{"url": "https://images.example/a.jpg"}]}))
    mock_http(monkeypatch, handle)
    result = await parsers.parse_xiaohongshu("http://xhslink.com/a/synthetic")
    assert result["type"] == "image"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_xhs_cookie_alias_and_serialized_ssr_state(monkeypatch):
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    monkeypatch.setenv("XIAOHONGSHU_COOKIE", "synthetic=alias")
    state = {"noteData": {"data": {"noteData": {"type": "normal", "imageList": [{"url": "https://images.example/a.jpg"}]}}}}
    def handle(request):
        assert request.headers["cookie"] == "synthetic=alias"
        return httpx.Response(200, text=f"<script>window.__INITIAL_SSR_STATE__=JSON.parse({json.dumps(json.dumps(state))});</script>")
    mock_http(monkeypatch, handle)
    assert (await parsers.parse_xiaohongshu(f"https://www.xiaohongshu.com/explore/{NOTE_ID}"))["type"] == "image"


@pytest.mark.parametrize("parser,url", [
    (parsers.parse_xiaohongshu, f"https://evil.example/explore/{NOTE_ID}"),
    (parsers.parse_xiaohongshu, f"https://xiaohongshu.com.evil.example/explore/{NOTE_ID}"),
    (parsers.parse_xiaohongshu, f"https://user:pass@www.xiaohongshu.com/explore/{NOTE_ID}"),
    (parsers.parse_xiaohongshu, f"file://www.xiaohongshu.com/explore/{NOTE_ID}"),
    (parsers.parse_xiaohongshu, f"//www.xiaohongshu.com/explore/{NOTE_ID}"),
    (parsers.parse_kuaishou, f"https://evil.example/short-video/{PHOTO_ID}"),
    (parsers.parse_kuaishou, f"https://evil-kuaishou.com/short-video/{PHOTO_ID}"),
    (parsers.parse_kuaishou, f"https://user:pass@www.kuaishou.com/short-video/{PHOTO_ID}"),
])
@pytest.mark.asyncio
async def test_native_origins_reject_arbitrary_hosts_and_credentials(monkeypatch, parser, url):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})
    mock_http(monkeypatch, handle)
    assert await parser(url) is None
    assert requests == []


@pytest.mark.parametrize("parser,url", [(parsers.parse_xiaohongshu, "https://xhslink.com/a/synthetic"), (parsers.parse_kuaishou, "https://v.kuaishou.com/synthetic")])
@pytest.mark.asyncio
async def test_native_short_links_refuse_unknown_redirects(monkeypatch, parser, url):
    requests = []
    monkeypatch.setenv("XHS_COOKIE", "synthetic=session")
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    mock_http(monkeypatch, handle)
    assert await parser(url) is None
    assert len(requests) == 1


def ks_resource():
    return {"h264": {"adaptationSet": [{"representation": [
        {"url": "https://media.example/low.mp4", "width": "720", "height": "1280", "avgBitrate": "1000000", "size": "1000"},
        {"url": "https://media.example/high.mp4", "backupUrl": ["https://backup.example/high.mp4"], "width": "1080", "height": "1920", "avgBitrate": "3800000", "size": "9200", "frameRate": "30", "qualityType": "1080p", "format": "mp4"},
    ]}]}, "h265": {"adaptationSet": [{"representation": [{"url": "https://media.example/hevc.mp4", "width": 1080, "height": 1920, "avgBitrate": 4000000, "size": 8500}]}]}}


def test_kuaishou_detail_collects_all_representations_with_selected_metadata():
    result = parsers._parse_kuaishou_detail({"photo": {"caption": "synthetic", "duration": "15000", "videoResource": ks_resource(), "viewCount": 42, "likeCount": "13", "commentCount": None}, "author": {"name": "author", "id": 9, "headerUrl": "https://images.example/avatar.jpg"}})
    assert result["source"] == "kuaishou_native"
    assert result["url"] == "https://media.example/high.mp4"
    assert result["duration"] == 15000
    assert (result["width"], result["height"], result["data_size"], result["fps"]) == (1080, 1920, 9200, 30)
    assert result["author"]["id"] == "9"
    assert result["statistics"] == {"play_count": 42, "like_count": 13}
    assert len(result["video_backup"]) == 3
    variant = next(item for item in result["video_backup"] if item["url"] == result["url"])
    assert variant["url_list"] == [result["url"], "https://backup.example/high.mp4"]


@pytest.mark.asyncio
async def test_kuaishou_init_state_h5_photo_atlas_and_music(monkeypatch):
    photo = {"id": PHOTO_ID, "caption": "synthetic album", "userName": "author", "userId": 7, "ext_params": {"atlas": {"cdn": ["images.example"], "list": ["/one.jpg", "/two.jpg"]}}, "music": {"name": "track", "audioUrls": [{"url": "https://music.example/track.mp3"}]}}
    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"result": 0})
        return httpx.Response(200, text=script("INIT_STATE", {"tusjohsynthetic": {"fid": PHOTO_ID, "photo": photo}}))
    mock_http(monkeypatch, handle)
    result = await parsers.parse_kuaishou(f"https://v.m.chenzhongtech.com/fw/photo/{PHOTO_ID}")
    assert result["type"] == "image"
    assert result["images"] == ["https://images.example/one.jpg", "https://images.example/two.jpg"]
    assert result["music"] == {"title": "track", "url": "https://music.example/track.mp3"}
    assert result["author"] == {"name": "author", "id": "7", "avatar": ""}


@pytest.mark.asyncio
async def test_kuaishou_apollo_default_client_resolves_author_and_manifest_json(monkeypatch):
    state = {"defaultClient": {
        f"VisionVideoDetailPhoto:{PHOTO_ID}": {"caption": "apollo synthetic", "manifestH265": {"json": json.dumps({"adaptationSet": [{"representation": [{"backupUrl": ["https://media.example/long.mp4", "https://backup.example/long.mp4"], "width": 1920, "height": 1080, "bitrate": "2000000"}]}]})}, "author": {"__ref": "VisionVideoDetailAuthor:chosen"}},
        "VisionVideoDetailAuthor:unrelated": {"name": "wrong author"},
        "VisionVideoDetailAuthor:chosen": {"id": "chosen", "name": "correct author", "headerUrl": "https://images.example/avatar.jpg"},
    }}
    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"result": 0})
        return httpx.Response(200, text=script("__APOLLO_STATE__", state))
    mock_http(monkeypatch, handle)
    result = await parsers.parse_kuaishou(f"https://www.kuaishou.com/short-video/{PHOTO_ID}")
    assert result["url"] == "https://media.example/long.mp4"
    assert result["author"]["name"] == "correct author"
    assert result["video_backup"][0]["codec"] == "h265"
    assert result["video_backup"][0]["url_list"] == [result["url"], "https://backup.example/long.mp4"]


@pytest.mark.asyncio
async def test_kuaishou_mobile_api_remains_available(monkeypatch):
    def handle(request):
        assert request.method == "POST"
        assert request.url.host == "v.m.chenzhongtech.com"
        assert json.loads(request.content)["photoId"] == PHOTO_ID
        return httpx.Response(200, json={"result": 1, "photo": {"caption": "mobile", "mainMvUrls": [{"url": "https://media.example/mobile.mp4"}], "duration": 5000, "width": 720, "height": 1280}})
    mock_http(monkeypatch, handle)
    result = await parsers.parse_kuaishou(f"https://www.kuaishou.com/short-video/{PHOTO_ID}")
    assert result["url"] == "https://media.example/mobile.mp4"
    assert result["duration"] == 5000
    assert result["width"] == 720


@pytest.mark.parametrize("parser,url", [(parsers.parse_xiaohongshu, f"https://www.xiaohongshu.com/explore/{NOTE_ID}"), (parsers.parse_kuaishou, f"https://www.kuaishou.com/short-video/{PHOTO_ID}")])
@pytest.mark.asyncio
async def test_native_failed_http_does_not_parse_error_body(monkeypatch, parser, url):
    body = xhs_html({"type": "normal", "imageList": [{"url": "https://images.example/a.jpg"}]})
    def handle(request):
        return httpx.Response(503, text=body)
    mock_http(monkeypatch, handle)
    assert await parser(url) is None


@pytest.mark.parametrize("detail", [{}, {"photo": None}, {"photo": {"videoResource": None}}, {"photo": {"mainMvUrls": []}}])
def test_kuaishou_null_or_empty_detail_returns_none(detail):
    assert parsers._parse_kuaishou_detail(detail) is None


def test_kuaishou_apollo_nested_video_resource_keeps_all_variant_fields():
    state = {"defaultClient": {f"VisionVideoDetailPhoto:{PHOTO_ID}": {"videoResource": ks_resource()}}}
    result = parsers._parse_kuaishou_html(script("__APOLLO_STATE__", state), PHOTO_ID)
    assert result["url"] == "https://media.example/high.mp4"
    assert result["data_size"] == 9200
    assert len(result["video_backup"]) == 3


def test_kuaishou_direct_urls_do_not_mix_other_variants_into_backup_lists():
    result = parsers._parse_kuaishou_detail({"photo": {
        "photoUrl": "https://media.example/main.mp4", "mainMvUrls": [{"url": "https://media.example/other.mp4"}],
    }})
    assert {item["url"]: item["url_list"] for item in result["video_backup"]} == {
        "https://media.example/main.mp4": ["https://media.example/main.mp4"],
        "https://media.example/other.mp4": ["https://media.example/other.mp4"],
    }


def test_kuaishou_next_data_shape_still_parses():
    state = {"props": {"pageProps": {"videoDetail": {"photo": {"photoUrl": "https://media.example/next.mp4"}, "author": {"name": "next author"}}}}}
    result = parsers._parse_kuaishou_html(f'<script type="application/json" id="__NEXT_DATA__">{json.dumps(state)}</script>', PHOTO_ID)
    assert result["url"] == "https://media.example/next.mp4"
    assert result["author"]["name"] == "next author"
