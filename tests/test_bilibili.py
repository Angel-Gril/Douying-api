import httpx
import pytest

import bilibili_parser


BVID = "BV1GJ411x7h7"
VIDEO_URL = f"https://www.bilibili.com/video/{BVID}/"


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)
    state = {
        "view": {
            "code": 0,
            "data": {
                "bvid": BVID,
                "aid": 170001,
                "title": "Public video",
                "pic": "https://i0.hdslb.com/cover.jpg",
                "owner": {"name": "Creator", "mid": 123, "face": "https://i0.hdslb.com/avatar.jpg"},
                "stat": {"view": 12345, "like": 90},
                "pages": [
                    {"page": 1, "cid": 111, "part": "First", "duration": 12},
                    {"page": 2, "cid": 222, "part": "Second", "duration": 25},
                ],
            },
        },
        "progressive": {
            "code": 0,
            "data": {
                "quality": 64,
                "format": "mp4720",
                "timelength": 12000,
                "accept_quality": [120, 80, 64, 32, 16],
                "durl": [{"order": 1, "url": "https://upos-sz-mirrorcos.bilivideo.com/main.mp4", "backup_url": ["https://upos-sz-mirrorhw.bilivideo.com/main.mp4"], "size": 123456, "length": 12000}],
            },
        },
        "dash": {"code": -404, "message": "Unavailable"},
        "requests": [],
        "redirects": {},
    }
    real_client = httpx.AsyncClient

    def handle(request):
        state["requests"].append(request)
        if str(request.url) in state["redirects"]:
            return httpx.Response(302, headers={"location": state["redirects"][str(request.url)]})
        if request.url.path == "/x/web-interface/view":
            payload = state["view"]
        elif request.url.path == "/x/player/playurl":
            payload = state["progressive"] if request.url.params.get("fnval") == "0" else state["dash"]
        else:
            raise AssertionError(f"Unexpected request: {request.url}")
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(bilibili_parser.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    return state


@pytest.mark.asyncio
async def test_progressive_has_honest_selected_metadata_and_no_advertised_variants(upstream):
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)

    assert result["type"] == "video"
    assert result["source"] == "bilibili_native"
    assert result["title"] == "Public video"
    assert result["duration"] == 12000
    assert result["url"] == "https://upos-sz-mirrorcos.bilivideo.com/main.mp4"
    assert result["author"] == {"name": "Creator", "id": "123", "avatar": "https://i0.hdslb.com/avatar.jpg"}
    assert result["play_count"] == 12345
    assert result["quality"] == "720P 高清"
    assert result["data_size"] == 123456
    assert result["format"] == "mp4"
    assert len(result["video_backup"]) == 1
    assert result["video_backup"][0]["url_list"] == [
        "https://upos-sz-mirrorcos.bilivideo.com/main.mp4",
        "https://upos-sz-mirrorhw.bilivideo.com/main.mp4",
    ]
    assert result.get("requires_mux", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["http://xy1x2x3x4xy.mcdn.bilivideo.cn:4483/video.mp4", "https://dvh4921e.edge.mountaintoys.cn:4483/video.mp4"])
async def test_regular_https_cdn_is_preferred_over_peer_cdn(upstream, peer):
    item = upstream["progressive"]["data"]["durl"][0]
    stable = "https://upos-sz-mirrorcos.bilivideo.com/video.mp4"
    item.update(url=peer, backup_url=[stable])
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)
    assert result["url"] == stable
    assert result["video_backup"][0]["url_list"] == [stable, peer]


@pytest.mark.asyncio
@pytest.mark.parametrize("video_id", [BVID, "av170001"])
async def test_selected_part_requests_its_cid_and_returns_reparseable_part_urls(upstream, video_id):
    upstream["progressive"]["data"]["timelength"] = 25000
    result = await bilibili_parser.parse_bilibili(f"https://www.bilibili.com/video/{video_id}?p=2&share_source=copy_web")

    playback_requests = [request for request in upstream["requests"] if request.url.path == "/x/player/playurl"]
    assert all(request.url.params["cid"] == "222" for request in playback_requests)
    assert result["duration"] == 25000
    assert result["parts"] == [
        {"index": 1, "title": "First", "url": VIDEO_URL + "?p=1", "cid": 111, "duration": 12000},
        {"index": 2, "title": "Second", "url": VIDEO_URL + "?p=2", "cid": 222, "duration": 25000},
    ]
    if video_id.startswith("av"):
        assert upstream["requests"][0].url.params["aid"] == "170001"


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["0", "-1", "3", "abc", "1.5"])
async def test_invalid_part_is_not_silently_replaced_with_first_part(upstream, part):
    assert await bilibili_parser.parse_bilibili(VIDEO_URL + f"?p={part}") is None
    assert not any(request.url.path == "/x/player/playurl" for request in upstream["requests"])


@pytest.fixture
def dash_payload():
    return {"code": 0, "data": {
        "quality": 120, "accept_quality": [120, 80, 64], "timelength": 12500,
        "dash": {
            "video": [
                {"id": 80, "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/1080-avc.m4s", "backupUrl": ["https://upos-sz-mirrorhw.bilivideo.com/1080-avc.m4s"], "codecs": "avc1.640028", "mimeType": "video/mp4", "width": 1920, "height": 1080, "frameRate": "60000/1001", "bandwidth": 2500000},
                {"id": 80, "base_url": "https://upos-sz-mirrorcos.bilivideo.com/1080-hevc.m4s", "codecs": "hev1.1.6.L120.90", "width": 1920, "height": 1080, "frame_rate": "30", "bandwidth": 1600000},
                {"id": 64, "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/720.m4s", "codecs": "avc1.64001F", "width": 1280, "height": 720, "frameRate": "30", "bandwidth": 1000000},
                {"id": 120, "codecs": "avc1.640033", "width": 3840, "height": 2160},
            ],
            "audio": [
                {"id": 30216, "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/low.m4s", "codecs": "mp4a.40.5", "bandwidth": 44000},
                {"id": 30280, "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/high.m4s", "codecs": "mp4a.40.2", "bandwidth": 192000},
            ],
        },
    }}


@pytest.mark.asyncio
async def test_dash_pairs_every_actual_variant_with_audio_and_matches_selected_metadata(upstream, dash_payload):
    upstream["progressive"] = {"code": -404}
    upstream["dash"] = dash_payload
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)

    assert result["url"] == "https://upos-sz-mirrorcos.bilivideo.com/1080-avc.m4s"
    assert result["duration"] == 12500
    assert (result["width"], result["height"], result["bit_rate"], result["codec"]) == (1920, 1080, 2500000, "avc1.640028")
    assert result["fps"] == pytest.approx(59.94005994)
    assert result["data_size"] == 0
    assert result["format"] == "mp4"
    assert result["requires_mux"] is True
    assert result["audio_url"] == "https://upos-sz-mirrorcos.bilivideo.com/high.m4s"
    assert len(result["video_backup"]) == 3
    assert {variant["quality_id"] for variant in result["video_backup"]} == {80, 64}
    assert all(variant["requires_mux"] and variant["audio_url"] == result["audio_url"] for variant in result["video_backup"])
    assert [audio["bit_rate"] for audio in result["audio_backup"]] == [192000, 44000]
    assert result["audio_backup"][0]["format"] == "m4a"
    assert result["audio_backup"][0]["codec"] == "mp4a.40.2"


@pytest.mark.asyncio
async def test_complete_progressive_remains_default_but_dash_variants_are_available(upstream, dash_payload):
    upstream["dash"] = dash_payload
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)

    assert result["url"] == "https://upos-sz-mirrorcos.bilivideo.com/main.mp4"
    assert (result["quality_id"], result["width"], result["height"], result["bit_rate"]) == (64, 0, 0, 0)
    assert len(result["video_backup"]) == 4
    assert result.get("requires_mux", False) is False
    assert "audio_url" not in result


@pytest.mark.asyncio
async def test_multi_segment_progressive_is_marked_for_concat_and_preserves_order(upstream):
    upstream["progressive"]["data"].update({
        "format": "flv720", "timelength": 0,
        "durl": [
            {"order": 2, "url": "https://upos-sz-mirrorcos.bilivideo.com/part2.flv", "size": 2000, "length": 7000},
            {"order": 1, "url": "https://upos-sz-mirrorcos.bilivideo.com/part1.flv", "backup_url": ["https://upos-sz-mirrorhw.bilivideo.com/part1.flv"], "size": 1000, "length": 5000},
        ],
    })
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)

    assert result["requires_concat"] is True
    assert result["segments"] == ["https://upos-sz-mirrorcos.bilivideo.com/part1.flv", "https://upos-sz-mirrorcos.bilivideo.com/part2.flv"]
    assert result["url"] == result["segments"][0]
    assert result["data_size"] == 3000
    assert result["duration"] == 12000
    assert result["format"] == "flv"
    assert result["video_backup"][0]["requires_concat"] is True
    assert result["video_backup"][0]["segments"] == result["segments"]


@pytest.mark.asyncio
async def test_incomplete_progressive_segment_list_is_not_exposed_as_a_complete_video(upstream):
    upstream["progressive"]["data"]["durl"].append({"order": 2, "length": 2000})
    assert await bilibili_parser.parse_bilibili(VIDEO_URL) is None


@pytest.mark.asyncio
async def test_short_link_resolves_only_site_redirects_and_keeps_selected_part(upstream, monkeypatch):
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=operator-configured")
    upstream["redirects"] = {
        "https://b23.tv/abc": "https://b23.tv/next",
        "https://b23.tv/next": VIDEO_URL + "?p=2",
    }
    result = await bilibili_parser.parse_bilibili("https://b23.tv/abc")

    assert result is not None
    requests = upstream["requests"]
    assert [str(request.url) for request in requests[:2]] == ["https://b23.tv/abc", "https://b23.tv/next"]
    assert all("cookie" not in request.headers for request in requests if request.url.host == "b23.tv")
    assert all(request.headers.get("cookie") == "SESSDATA=operator-configured" for request in requests if request.url.host == "api.bilibili.com")
    assert all(request.url.params["cid"] == "222" for request in requests if request.url.path == "/x/player/playurl")


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["https://evil.example/video/BV1GJ411x7h7/", "http://127.0.0.1/video/BV1GJ411x7h7/", "https://bilibili.com.evil.example/video/BV1GJ411x7h7/", "https://user:password@www.bilibili.com/video/BV1GJ411x7h7/", "file:///etc/passwd"])
async def test_untrusted_short_link_redirect_is_never_requested(upstream, location):
    upstream["redirects"]["https://b23.tv/abc"] = location
    assert await bilibili_parser.parse_bilibili("https://b23.tv/abc") is None
    assert [str(request.url) for request in upstream["requests"]] == ["https://b23.tv/abc"]


@pytest.mark.asyncio
async def test_untrusted_input_is_rejected_before_network_access(upstream):
    assert await bilibili_parser.parse_bilibili("https://www.bilibili.com.evil.example/video/BV1GJ411x7h7/") is None
    assert upstream["requests"] == []


@pytest.mark.asyncio
async def test_metadata_redirect_cannot_leak_cookie_outside_site(upstream, monkeypatch):
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=operator-configured")
    upstream["redirects"][f"https://api.bilibili.com/x/web-interface/view?bvid={BVID}"] = "https://evil.example/view"
    assert await bilibili_parser.parse_bilibili(VIDEO_URL) is None
    assert len(upstream["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_mode", ["progressive", "dash"])
async def test_one_playback_mode_failure_does_not_discard_the_other(upstream, dash_payload, failed_mode):
    upstream["dash"] = dash_payload
    upstream[failed_mode] = httpx.Response(403, json={"code": -403, "message": "Forbidden"})
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)

    assert result is not None
    assert result.get("requires_mux", False) is (failed_mode == "progressive")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"code": -403, "data": {"quality": 80}}, {"code": 0}, {"code": 0, "data": {}}, {"code": 0, "data": {"quality": 120, "accept_quality": [120, 80, 64]}}, httpx.Response(403, text="Forbidden"), httpx.Response(200, text="<html>not a playback payload</html>")])
async def test_failed_or_missing_playback_payload_returns_none(upstream, payload):
    upstream["progressive"] = payload
    upstream["dash"] = payload
    assert await bilibili_parser.parse_bilibili(VIDEO_URL) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"code": -404}, {"code": 0}, {"code": 0, "data": {}}, {"code": 0, "data": {"pages": {"invalid": "structure"}}}, {"code": 0, "data": {"pages": [{"page": 1, "cid": 0}]}}, httpx.Response(403, text="Forbidden")])
async def test_missing_or_failed_view_returns_none(upstream, payload):
    upstream["view"] = payload
    assert await bilibili_parser.parse_bilibili(VIDEO_URL) is None


@pytest.mark.asyncio
async def test_dash_without_an_usable_audio_track_is_not_presented_as_complete(upstream, dash_payload):
    upstream["progressive"] = {"code": -404}
    dash_payload["data"]["dash"]["audio"] = [{"id": 30280, "codecs": "mp4a.40.2"}]
    upstream["dash"] = dash_payload
    assert await bilibili_parser.parse_bilibili(VIDEO_URL) is None


@pytest.mark.asyncio
async def test_optional_audio_tracks_keep_real_codecs_and_aac_remains_compatible_default(upstream, dash_payload):
    upstream["progressive"] = {"code": -404}
    dash_payload["data"]["dash"].update({
        "dolby": {"audio": [{"id": 30250, "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/dolby.m4s", "codecs": "ec-3", "bandwidth": 384000}]},
        "flac": {"audio": {"id": 30251, "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/flac.m4s", "codecs": "fLaC", "bandwidth": 1000000}},
    })
    upstream["dash"] = dash_payload
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)

    assert result["audio_url"] == "https://upos-sz-mirrorcos.bilivideo.com/high.m4s"
    assert {audio["codec"] for audio in result["audio_backup"]} == {"mp4a.40.2", "mp4a.40.5", "ec-3", "fLaC"}
    assert all(audio["format"] == "m4a" for audio in result["audio_backup"])


@pytest.mark.asyncio
async def test_malformed_optional_metadata_does_not_break_available_video(upstream):
    upstream["view"]["data"].update({"owner": ["invalid"], "stat": [12345]})
    result = await bilibili_parser.parse_bilibili(VIDEO_URL)
    assert result["author"] == {"name": "", "id": "", "avatar": ""}
    assert result["statistics"] == {}
