import asyncio
import io
import subprocess
import zipfile

import httpx
import pytest

import media_downloads as downloads
import server


async def finish(job_id):
    await downloads.DOWNLOAD_JOBS[job_id]["task"]
    return downloads.download_job_status(job_id)


def transport(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(downloads, "is_safe_media_url", lambda url: url.startswith("https://media.example/"))


@pytest.mark.asyncio
async def test_zip_job_preserves_images_and_completes(monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200, content=b"image-" + req.url.path.encode(), headers={"content-type": "image/jpeg"}))
    job = downloads.create_download_job({"mode": "zip", "urls": ["https://media.example/one.jpg", "https://media.example/two.jpg"], "filename": "album"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "completed"
        assert result["completed_items"] == 2
        path = downloads.DOWNLOAD_JOBS[job["id"]]["output"]
        with zipfile.ZipFile(path) as archive:
            assert len(archive.namelist()) == 2
            assert archive.read(archive.namelist()[0]) == b"image-/one.jpg"
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_zip_job_rejects_partial_or_error_files(monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200, text="<html>error</html>"))
    job = downloads.create_download_job({"mode": "zip", "urls": ["https://media.example/bad.jpg"], "filename": "album"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "failed"
        assert "download_url" not in result
        assert not any(downloads.DOWNLOAD_JOBS[job["id"]]["directory"].iterdir())
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_cancel_job_stops_download_and_cleans_files(monkeypatch):
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(downloads, "save_media", slow)
    job = downloads.create_download_job({"mode": "zip", "urls": ["https://media.example/one.jpg"], "filename": "album"})
    await started.wait()
    await downloads.cancel_download_job(job["id"])
    try:
        result = downloads.download_job_status(job["id"])
        assert result["status"] == "cancelled"
        assert not any(downloads.DOWNLOAD_JOBS[job["id"]]["directory"].iterdir())
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_hls_encrypted_manifest_is_rejected_before_ffmpeg(monkeypatch):
    body = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="secret.key"\n#EXTINF:1,\npart.ts\n#EXT-X-ENDLIST\n'
    transport(monkeypatch, lambda req: httpx.Response(200, text=body, headers={"content-type": "application/vnd.apple.mpegurl"}))
    job = downloads.create_download_job({"mode": "hls", "url": "https://media.example/list.m3u8", "filename": "video"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "failed"
        assert "加密" in result["error"]
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_mux_uses_local_files_and_produces_audio_video_mp4(monkeypatch, tmp_path):
    ffmpeg = downloads.ffmpeg_path()
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=5", "-t", "0.4", "-an", "-c:v", "libx264", str(video)], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.4", "-c:a", "aac", str(audio)], check=True)
    bodies = {"/v.mp4": video.read_bytes(), "/a.m4a": audio.read_bytes()}
    transport(monkeypatch, lambda req: httpx.Response(200, content=bodies[req.url.path], headers={"content-type": "video/mp4"}))
    job = downloads.create_download_job({"mode": "mux", "url": "https://media.example/v.mp4", "audio_url": "https://media.example/a.m4a", "filename": "combined"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "completed", result
        path = downloads.DOWNLOAD_JOBS[job["id"]]["output"]
        probe = subprocess.run([ffmpeg, "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
        assert "Video:" in probe.stderr and "Audio:" in probe.stderr
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_zip_job_api_downloads_finished_attachment(monkeypatch):
    real = httpx.AsyncClient
    transport(monkeypatch, lambda req: httpx.Response(200, content=b"image", headers={"content-type": "image/jpeg"}))
    async with real(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/download/jobs", json={"mode": "zip", "urls": ["https://media.example/image.jpg"], "filename": "pictures"})
        assert response.status_code == 200
        job_id = response.json()["data"]["id"]
        try:
            await finish(job_id)
            status = (await client.get("/api/download/jobs/" + job_id)).json()["data"]
            result = await client.get(status["download_url"])
            assert result.status_code == 200
            assert "attachment" in result.headers["content-disposition"]
            with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
                assert archive.read(archive.namelist()[0]) == b"image"
        finally:
            downloads.remove_download_job(job_id)


@pytest.mark.asyncio
async def test_hls_download_uses_checked_segments_and_local_ffmpeg(monkeypatch, tmp_path):
    ffmpeg = downloads.ffmpeg_path()
    manifest = tmp_path / "list.m3u8"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=10", "-t", "1.2", "-an", "-c:v", "libx264", "-g", "2", "-f", "hls", "-hls_time", "0.4", "-hls_list_size", "0", str(manifest)], check=True)
    def handle(request):
        file = tmp_path / request.url.path.lstrip("/")
        return httpx.Response(200, content=file.read_bytes(), headers={"content-type": "application/vnd.apple.mpegurl" if file.suffix == ".m3u8" else "video/mp2t"})
    transport(monkeypatch, handle)
    job = downloads.create_download_job({"mode": "hls", "url": "https://media.example/list.m3u8", "filename": "video"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "completed", result
        assert b"ftyp" in downloads.DOWNLOAD_JOBS[job["id"]]["output"].read_bytes()[:64]
        assert result["completed_items"] >= 2
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_hls_private_segment_is_never_requested(monkeypatch):
    requests = []
    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, text="#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1/private.ts\n#EXT-X-ENDLIST\n", headers={"content-type": "application/vnd.apple.mpegurl"})
    transport(monkeypatch, handle)
    job = downloads.create_download_job({"mode": "hls", "url": "https://media.example/list.m3u8", "filename": "video"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "failed"
        assert requests == ["https://media.example/list.m3u8"]
    finally:
        downloads.remove_download_job(job["id"])


@pytest.mark.asyncio
async def test_hls_relative_segments_use_redirected_manifest_location(monkeypatch, tmp_path):
    requests = []
    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/old.m3u8":
            return httpx.Response(302, headers={"location": "/nested/list.m3u8"})
        if request.url.path == "/nested/list.m3u8":
            return httpx.Response(200, text="#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n", headers={"content-type": "application/vnd.apple.mpegurl"})
        assert request.url.path == "/nested/segment.ts"
        return httpx.Response(200, content=b"segment", headers={"content-type": "video/mp2t"})
    transport(monkeypatch, handle)
    job = {"directory": tmp_path, "downloaded_bytes": 0, "total_items": 0, "completed_items": 0}
    manifest, _ = await downloads._prepare_hls("https://media.example/old.m3u8", job)
    assert manifest.is_file()
    assert requests[-1] == "/nested/segment.ts"


@pytest.mark.asyncio
async def test_hls_range_requires_exact_requested_content_range(monkeypatch, tmp_path):
    transport(monkeypatch, lambda req: httpx.Response(206, content=b"part", headers={"content-type": "video/mp2t", "content-range": "bytes 0-3/100"}))
    job = {"downloaded_bytes": 0, "completed_items": 0}
    with pytest.raises(downloads.MediaError):
        await downloads.save_media("https://media.example/range.ts", tmp_path / "part.ts", job, range_header="bytes=40-47")
    assert job["completed_items"] == 0


@pytest.mark.asyncio
async def test_mux_rejects_remote_manifest_disguised_as_video(monkeypatch, tmp_path):
    ffmpeg = downloads.ffmpeg_path()
    private = tmp_path / "private-video.mp4"
    audio = tmp_path / "audio.m4a"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=5", "-t", "0.4", "-an", "-c:v", "libx264", str(private)], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.4", "-c:a", "aac", str(audio)], check=True)
    manifest = f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:0.4,\n{private.as_uri()}\n#EXT-X-ENDLIST\n".encode()
    transport(monkeypatch, lambda req: httpx.Response(200, content=manifest if req.url.path == "/video.mp4" else audio.read_bytes(), headers={"content-type": "video/mp4"}))
    original_ffmpeg = downloads._ffmpeg
    processes = []
    async def inspect_ffmpeg(*args):
        processes.append(1)
        return await original_ffmpeg(*args)
    monkeypatch.setattr(downloads, "_ffmpeg", inspect_ffmpeg)
    job = downloads.create_download_job({"mode": "mux", "url": "https://media.example/video.mp4", "audio_url": "https://media.example/audio.m4a", "filename": "combined"})
    try:
        result = await finish(job["id"])
        assert result["status"] == "failed", "A remote playlist must never be interpreted as a local media input"
        assert processes == []
    finally:
        downloads.remove_download_job(job["id"])
