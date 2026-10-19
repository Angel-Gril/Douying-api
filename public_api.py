"""Sequential BugPK site selection and safe public-response adaptation."""

import os
from typing import Optional
from urllib.parse import urlsplit

import httpx

from media_normalizer import normalize_provider_result


LEGACY_BASE_URL = "https://api.bugpk.com"
LEGACY_PATHS = {
    "douyin": "/api/douyin", "doubao": "/api/dbvideos",
    "xiaohongshu": "/api/xhsjx", "kuaishou": "/api/ksjx",
    "bilibili": "/api/bilibili", "weibo": "/api/weibo",
    "toutiao": "/api/toutiao", "pipixia": "/api/pipixia",
    "pipigx": "/api/pipigx", "zuiyou": "/api/zuiyou",
}
DEFAULT_GATEWAY = "https://api-new.ifphp.com"
GATEWAY_HOSTS = frozenset(("api-new.ifphp.com", "api-new.bugpk.com"))


def _gateway_base(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or parsed.hostname not in GATEWAY_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return None
        return f"https://{parsed.hostname}"
    except ValueError:
        return None


async def parse_public_url(url: str, platform: str) -> Optional[dict]:
    """Try the configured authenticated site, then the legacy public site.

    Each site receives at most one request. Redirects are disabled so even a
    legitimate gateway cannot forward X-API-Key to an unrelated destination.
    Failures deliberately log neither request URLs nor provider response bodies.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    candidates = []
    key = os.getenv("BUGPK_API_KEY", "").strip()
    if key:
        base = _gateway_base(os.getenv("BUGPK_API_BASE_URL", DEFAULT_GATEWAY))
        if base:
            candidates.append((base, "/api/dyjx" if platform == "douyin" else "/api/svparse", {"X-API-Key": key}))
    candidates.append((LEGACY_BASE_URL, LEGACY_PATHS.get(platform, "/api/short_videos"), {}))
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), follow_redirects=False) as client:
        for base, path, headers in candidates:
            try:
                response = await client.get(base + path, params={"url": url}, headers=headers)
                if response.status_code != 200:
                    continue
                data = response.json()
                if platform == "bilibili" and isinstance(data, dict):
                    # Context is local and never sent as a provider credential.
                    data = {**data, "webpage_url": url}
                result = normalize_provider_result(data, platform, provider="bugpk")
                if result:
                    return result
            except (httpx.HTTPError, ValueError, TypeError):
                continue
    return None
