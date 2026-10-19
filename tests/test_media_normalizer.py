import pytest

from media_normalizer import normalize_provider_result


def test_bugpk_seconds_sizes_variants_and_actual_statistics():
    result = normalize_provider_result({"code": 200, "data": {
        "url": "https://cdn.example/original.mp4", "title": "sample",
        "duration": 11.123, "size": 2432219,
        "author": {"nickname": "author", "uid": 12, "avatar": "https://cdn.example/avatar.jpg"},
        "statistics": {"play_count": 32, "digg_count": 4, "share_count": "unavailable"},
        "video_backup": [
            {"url": "https://cdn.example/720.mp4", "url_list": ["https://cdn2.example/720.mp4", "javascript:bad"],
             "quality": "720p", "size": 2395049, "bit_rate": 2395049},
            {"url": "https://cdn.example/1080.mp4", "width": 1080, "height": 1920,
             "data_size": 3200000, "bitrate": 1700000, "fps": 30, "codec": "h264", "format": "mp4"},
            "https://cdn.example/backup.mp4",
        ],
        "api_key": "must-not-leak", "extra": {"cookie": "must-not-leak"},
    }}, "douyin")
    assert result["duration"] == 11123
    assert result["data_size"] == 2432219
    assert result["author"] == {"name": "author", "id": "12", "avatar": "https://cdn.example/avatar.jpg"}
    assert result["statistics"] == {"play_count": 32, "digg_count": 4}
    assert result["play_count"] == 32
    assert result["video_backup"][0]["url_list"] == ["https://cdn.example/720.mp4", "https://cdn2.example/720.mp4"]
    assert result["video_backup"][0]["bit_rate"] == 0
    assert result["video_backup"][0]["width"] == 0
    assert result["video_backup"][0]["codec"] == ""
    assert result["video_backup"][1]["bit_rate"] == 1700000
    assert result["video_backup"][2]["url"] == "https://cdn.example/backup.mp4"
    assert result["source"] == "public_api"
    assert result["provider"] == "bugpk"
    assert "api_key" not in result and "extra" not in result


@pytest.mark.parametrize("seconds, expected", [(1, 1000), (100001, 100001000), (0, 0), ("1.25", 1250)])
def test_bugpk_duration_uses_documented_seconds_without_magnitude_guessing(seconds, expected):
    result = normalize_provider_result({"url": "https://cdn.example/a.mp4", "duration": seconds}, "douyin")
    assert result["duration"] == expected


def test_explicit_milliseconds_and_unknown_provider_duration():
    data = {"url": "https://cdn.example/a.mp4", "duration": 1.25, "duration_ms": 1250}
    assert normalize_provider_result(data, "douyin")["duration"] == 1250
    assert normalize_provider_result({"url": data["url"], "duration": 123}, "douyin", "other")["duration"] == 0


@pytest.mark.parametrize("data", [None, [], {}, {"code": 200, "data": None}, {"code": 200, "data": []},
                                      {"code": 200, "data": {}}, {"code": 400, "data": {"url": "https://cdn.example/a"}},
                                      {"success": False, "url": "https://cdn.example/a"},
                                      {"url": "javascript:bad", "images": [None, "file:///bad", 123]}])
def test_failure_or_missing_media_is_not_success(data):
    assert normalize_provider_result(data, "douyin") is None


def test_live_photo_and_plain_images_filter_invalid_urls_and_preserve_pairs():
    result = normalize_provider_result({"code": "200", "data": {
        "type": "live", "images": ["https://cdn.example/still.jpg", {"url_list": ["https://cdn.example/b.jpg"]}],
        "live_photo": [
            {"image": "https://cdn.example/still.jpg", "video": "https://cdn.example/motion.mp4"},
            {"image": "https://cdn.example/b.jpg", "video": "data:invalid"},
        ],
    }}, "xiaohongshu")
    assert result["type"] == "live"
    assert result["live_photo"] == [{"image": "https://cdn.example/still.jpg", "video": "https://cdn.example/motion.mp4"}]
    assert result["images"] == ["https://cdn.example/still.jpg", "https://cdn.example/b.jpg"]
    assert normalize_provider_result({"images": ["https://cdn.example/a.jpg"]}, "xiaohongshu")["type"] == "image"


def test_bilibili_typo_author_and_parts_keep_parseable_page_urls():
    result = normalize_provider_result({"code": 200, "data": {
        "title": "video", "url": "https://cdn.example/main.mp4", "auther": "creator",
        "user": {"name": "creator", "avatar": "https://cdn.example/u.jpg", "mid": 9},
        "bvid": "BV1GJ411x7h7", "videos": [
            {"index": 1, "title": "part 1", "duration": 30, "cid": 101},
            {"index": 2, "title": "part 2", "duration": 45, "url": "https://www.bilibili.com/video/BV1GJ411x7h7/?p=2"},
        ],
    }}, "bilibili")
    assert result["author"] == {"name": "creator", "id": "9", "avatar": "https://cdn.example/u.jpg"}
    assert result["parts"] == [
        {"index": 1, "title": "part 1", "url": "https://www.bilibili.com/video/BV1GJ411x7h7/?p=1", "cid": 101, "duration": 30000},
        {"index": 2, "title": "part 2", "url": "https://www.bilibili.com/video/BV1GJ411x7h7/?p=2", "cid": 0, "duration": 45000},
    ]


@pytest.mark.parametrize("media", [{"video_url": "https://cdn.example/v.mp4"},
                                  {"durl": [{"url": "https://cdn.example/v.mp4", "backup_url": ["https://cdn2.example/v.mp4"]}]}])
def test_bilibili_root_and_data_media_shapes(media):
    for data in (media, {"code": 200, "data": media}):
        result = normalize_provider_result(data, "bilibili")
        assert result["url"] == "https://cdn.example/v.mp4"
        assert result["author"] == {"name": "", "id": "", "avatar": ""}


def test_audio_mux_fields_follow_each_stream_and_only_media_urls_are_forwarded():
    result = normalize_provider_result({"url": "https://cdn.example/v.mp4", "audio_url": "https://cdn.example/a.m4a",
                                        "requires_mux": True, "music": {"title": "track", "url": "https://cdn.example/music.mp3"},
                                        "video_backup": [{"url": "https://cdn.example/other.mp4"},
                                                         {"url": "https://cdn.example/no-mux.mp4", "requires_mux": False}]}, "bilibili")
    assert result["requires_mux"] is True
    assert result["audio_url"] == "https://cdn.example/a.m4a"
    assert result["video_backup"][0]["audio_url"] == result["audio_url"]
    assert result["video_backup"][0]["requires_mux"] is True
    assert result["video_backup"][1]["requires_mux"] is False
    assert result["music"]["url"] == "https://cdn.example/music.mp3"


def test_backups_alone_are_video_media_and_keep_distinct_urls():
    result = normalize_provider_result({"video_backup": [
        {"url_list": ["https://cdn.example/a.mp4", "https://cdn2.example/a.mp4"]},
        "https://cdn.example/b.mp4", "https://cdn.example/b.mp4", 2,
    ]}, "weibo")
    assert result["url"] == "https://cdn.example/a.mp4"
    assert len(result["video_backup"]) == 2


def test_invalid_numeric_and_secret_bearing_author_urls_are_not_forwarded():
    result = normalize_provider_result({"url": "https://cdn.example/a.mp4", "duration": "NaN", "size": float("inf"),
                                        "width": True, "fps": -30, "play_count": True,
                                        "author": {"avatar": "https://user:secret@cdn.example/avatar"}}, "douyin")
    assert result["duration"] == 0 and result["data_size"] == 0
    assert result["width"] == 0 and result["fps"] == 0
    assert result["author"]["avatar"] == ""
    assert "play_count" not in result


@pytest.mark.parametrize("field,value", [("live_photo", 3), ("live_photo", True), ("images", False),
                                         ("video_backup", 2), ("durl", "invalid")])
def test_malformed_optional_media_fields_do_not_break_valid_video(field, value):
    result = normalize_provider_result({"url": "https://cdn.example/v.mp4", field: value}, "douyin")
    assert result["url"] == "https://cdn.example/v.mp4"


@pytest.mark.parametrize("url", ["https://bad host.example/v.mp4", "https://cdn.example/has space.mp4", "https://cdn.example\\@evil.example/v"])
def test_invalid_http_url_strings_are_not_media(url):
    assert normalize_provider_result({"url": url}, "douyin") is None


def test_extreme_duration_is_rejected_and_bilibili_actual_statistic_names_survive():
    result = normalize_provider_result({"url": "https://cdn.example/v.mp4", "duration": "1e10000",
                                        "statistics": {"view": 200, "coin": 2, "like": 8, "cookie": "private"}}, "bilibili")
    assert result["duration"] == 0
    assert result["statistics"] == {"view": 200, "coin": 2, "like": 8}
