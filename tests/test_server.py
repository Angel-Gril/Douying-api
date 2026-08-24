import pytest

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
