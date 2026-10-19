import struct

import pytest

from original_metadata import (
    MetadataError, apply_original_metadata, parse_moov, probe_metadata,
    validate_range_headers,
)


def box(kind, payload=b"", *, extended=False):
    return (struct.pack(">I4sQ", 1, kind, len(payload) + 16) if extended else struct.pack(">I4s", len(payload) + 8, kind)) + payload


def time_box(kind, scale, duration, version=0):
    payload = bytes([version, 0, 0, 0])
    payload += struct.pack(">QQIQ", 0, 0, scale, duration) if version else struct.pack(">IIII", 0, 0, scale, duration)
    return box(kind, payload)


def track(*, codec=b"hvc1", width=2160, height=3840, scale=30000, entries=((216, 1000),), rotation=0, handler=b"vide", sample_size=1000, sample_sizes=None, version=0):
    count = sum(item[0] for item in entries)
    duration = sum(item[0] * item[1] for item in entries)
    header = bytearray(84)
    header[:4] = b"\x00\x00\x00\x01"
    struct.pack_into(">I", header, 12, 1)
    struct.pack_into(">I", header, 20, 7200)
    matrix = (0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824) if rotation == 90 else (65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
    struct.pack_into(">9i", header, 40, *matrix)
    sample = bytearray(78)
    struct.pack_into(">H", sample, 6, 1)
    struct.pack_into(">HH", sample, 24, width, height)
    stsd = box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + box(codec, bytes(sample)))
    stts = box(b"stts", b"\0" * 4 + struct.pack(">I", len(entries)) + b"".join(struct.pack(">II", *entry) for entry in entries))
    stsz_payload = b"\0" * 4 + struct.pack(">II", sample_size, count)
    if not sample_size:
        stsz_payload += b"".join(struct.pack(">I", size) for size in (sample_sizes or [1000] * count))
    stbl = box(b"stbl", stsd + stts + box(b"stsz", stsz_payload))
    media = time_box(b"mdhd", scale, duration, version) + box(b"hdlr", b"\0" * 8 + handler + b"\0" * 12) + box(b"minf", stbl)
    return box(b"trak", box(b"tkhd", bytes(header)) + box(b"mdia", media))


def moov(*, tracks=None, duration=7200, version=0, extended=False, fragmented=False, **kwargs):
    payload = time_box(b"mvhd", 1000, duration, version) + (b"".join(tracks) if tracks is not None else track(version=version, **kwargs))
    if fragmented:
        payload += box(b"mvex", box(b"trex", b"\0" * 24))
    return box(b"moov", payload, extended=extended)


def test_native_metadata_recovers_original_video_fields_without_transcode_values():
    result = parse_moov(moov(), 300000)
    assert (result["width"], result["height"]) == (2160, 3840)
    assert result["codec"] == "h265" and result["codec_tag"] == "hvc1"
    assert result["fps"] == 30 and result["fps_kind"] == "average"
    assert result["duration"] == result["video_duration"] == 7200
    assert result["bitrate"] == 240000 and result["bitrate_kind"] == "video_average"
    assert result["overall_bitrate"] == 333333


def test_audio_first_track_rotation_and_64bit_time_fields():
    result = parse_moov(moov(tracks=[track(handler=b"soun", width=320, height=240), track(codec=b"avc1", width=2560, height=1440, rotation=90, version=1)], version=1, extended=True), 300000)
    assert (result["width"], result["height"], result["rotation"]) == (2560, 1440, 90)
    assert result["codec"] == "h264"
    assert result["duration"] == 7200


def test_variable_frame_rate_is_an_average_and_track_bitrate_uses_sample_bytes():
    entries = ((100, 1000), (50, 2000))
    result = parse_moov(moov(scale=30000, entries=entries, sample_size=0, sample_sizes=[2000] * 150), 1000000)
    assert result["fps"] == pytest.approx(22.5)
    assert result["bitrate"] == 360000
    assert result["video_duration"] == 6667
    assert result["duration"] == 7200


def test_fragmented_or_oversized_timing_tables_keep_dimensions_without_inventing_rates():
    fragmented = parse_moov(moov(fragmented=True), 300000)
    assert fragmented["width"] == 2160
    assert not fragmented.get("fps") and not fragmented.get("bitrate")
    capped = parse_moov(moov(entries=((1, 1000), (1, 2000))), 300000, max_timing_entries=1)
    assert capped["height"] == 3840
    assert not capped.get("fps") and not capped.get("bitrate")


def test_worker_sample_size_budget_still_returns_mean_fps_and_container_bitrate():
    result = parse_moov(moov(sample_size=0), 300000, max_sample_entries=0)
    assert result["fps"] == 30 and result["overall_bitrate"] == 333333
    assert not result.get("bitrate")


def test_impossible_sample_sizes_do_not_become_video_bitrate():
    result = parse_moov(moov(), 1000)
    assert result["width"] == 2160 and result["fps"] == 30
    assert not result.get("bitrate")


@pytest.mark.parametrize("content", [b"", b"not a movie", moov()[:-4], box(b"moov", b"\0\0\0\1trak"), struct.pack(">I4sQ", 1, b"moov", 7)])
def test_bad_boxes_do_not_become_verified_metadata(content):
    assert parse_moov(content, 300000) == {}


def test_box_count_limit_is_shared_across_all_nested_containers():
    assert parse_moov(moov(), 300000, max_boxes=8) == {}
    assert parse_moov(moov(), 300000, max_boxes=256)["width"] == 2160


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["head", "tail", "boundary", "extended-mdat"])
async def test_range_planner_jumps_over_media_and_reads_aligned_moov(layout):
    metadata = moov()
    ftyp = box(b"ftyp", b"isom\0\0\0\0isom")
    if layout == "head":
        content = ftyp + metadata + box(b"mdat", b"x" * 100000)
    else:
        target = 65532 if layout == "boundary" else 1024 * 1024 + 12
        header_size = 16 if layout == "extended-mdat" else 8
        payload = b"x" * (target - len(ftyp) - header_size)
        # A fake moov string in media data must never be scanned as metadata.
        payload = payload[:100] + b"moov" + payload[104:]
        content = ftyp + box(b"mdat", payload, extended=layout == "extended-mdat") + metadata
    requests = []
    async def read(start, end):
        requests.append((start, end))
        return content[start:end + 1]
    result = await probe_metadata(read, len(content))
    assert result["width"] == 2160 and result["fps"] == 30
    assert len(requests) <= 2
    assert sum(end - start + 1 for start, end in requests) < 140000
    if layout != "head":
        assert requests[1][0] == content.index(metadata)


@pytest.mark.asyncio
async def test_probe_limits_and_network_failures_leave_metadata_unknown():
    content = box(b"ftyp", b"isom") + box(b"mdat", b"x" * 100000) + moov()
    requests = []
    async def read(start, end):
        requests.append((start, end))
        return content[start:end + 1]
    assert await probe_metadata(read, len(content), max_requests=1) == {}
    assert len(requests) == 1
    assert await probe_metadata(read, len(content), max_moov_bytes=32) == {}
    async def failed(start, end):
        raise TimeoutError("sample upstream failure")
    assert await probe_metadata(failed, len(content)) == {}


@pytest.mark.asyncio
async def test_extended_mdat_offsets_above_four_gib_are_not_truncated():
    first = box(b"ftyp", b"isom")
    mdat_size = 2 ** 32 + 1024
    location = len(first) + mdat_size
    metadata = moov()
    segments = [(0, first + struct.pack(">I4sQ", 1, b"mdat", mdat_size)), (location, metadata)]
    total = location + len(metadata)
    requests = []
    async def read(start, end):
        requests.append((start, end))
        result = bytearray(end - start + 1)
        for offset, content in segments:
            begin, finish = max(start, offset), min(end + 1, offset + len(content))
            if begin < finish:
                result[begin - start:finish - start] = content[begin - offset:finish - offset]
        return bytes(result)
    result = await probe_metadata(read, total)
    assert result["fps"] == 30
    assert requests[1][0] == location and location > 2 ** 32
    assert sum(end - start + 1 for start, end in requests) < 70000


@pytest.mark.asyncio
async def test_probe_uses_bounded_completed_metadata_cache():
    content = moov()
    count = 0
    async def read(start, end):
        nonlocal count
        count += 1
        return content[start:end + 1]
    first = await probe_metadata(read, len(content), cache_key="unit-test-cache-original")
    first["width"] = 1
    second = await probe_metadata(read, len(content), cache_key="unit-test-cache-original")
    assert second["width"] == 2160 and count == 1


@pytest.mark.parametrize("status,headers", [(200, {}), (206, {"content-range": "bytes 1-4/100"}), (206, {"content-range": "bytes 0-3/101"}), (206, {"content-range": "bytes 0-3/100", "content-length": "8"}), (206, {"content-range": "bytes 0-3/100", "content-encoding": "gzip"})])
def test_range_header_validation_rejects_ignored_inconsistent_or_encoded_ranges(status, headers):
    with pytest.raises(MetadataError):
        validate_range_headers(status, headers, 0, 3, 100)


def test_original_update_clears_stale_transcode_metadata_and_preserves_download():
    result = {"url": "https://media.example/original.mp4", "duration": 10000, "codec": "h264", "width": 1920, "height": 1080, "bitrate": 2000000, "bit_rate": 2000000, "fps": 60}
    apply_original_metadata(result, {"width": 2160, "height": 3840, "codec": "h265", "codec_tag": "hvc1"})
    assert result["url"].endswith("original.mp4")
    assert result["codec"] == "h265" and result["width"] == 2160
    assert result["bitrate"] == result["bit_rate"] == result["fps"] == 0
    assert result["duration"] == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize("encoder,codec", [("libx264", "h264"), ("libx265", "h265")])
@pytest.mark.parametrize("faststart", [True, False])
async def test_real_encoded_files_match_generated_video_parameters(tmp_path, encoder, codec, faststart):
    import subprocess
    from media_downloads import ffmpeg_path
    path = tmp_path / "sample.mp4"
    args = [ffmpeg_path(), "-v", "error", "-f", "lavfi", "-i", "color=size=160x90:rate=10", "-t", "0.4", "-an", "-c:v", encoder, "-preset", "ultrafast"]
    if encoder == "libx265":
        args += ["-x265-params", "log-level=error:pools=1"]
    if faststart:
        args += ["-movflags", "+faststart"]
    subprocess.run(args + [str(path)], check=True, capture_output=True)
    content = path.read_bytes()
    async def read(start, end):
        return content[start:end + 1]
    result = await probe_metadata(read, len(content))
    assert (result["width"], result["height"], result["codec"]) == (160, 90, codec)
    assert result["fps"] == pytest.approx(10)
    assert result["duration"] == 400
    assert result["format"] == "mp4"


@pytest.mark.asyncio
async def test_http_adapter_never_reads_an_ignored_range_response(monkeypatch):
    import httpx
    import server
    events = []
    class HugeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            events.append("read")
            yield b"unexpected body"
        async def aclose(self):
            events.append("closed")
    async def opened(url, **kwargs):
        response = httpx.Response(200, headers={"content-length": "50000000"}, stream=HugeBody())
        class Client:
            async def aclose(self):
                events.append("client closed")
        return Client(), response
    monkeypatch.setattr(server, "open_media", opened)
    assert await server.probe_original_video_metadata("https://media.example/range-ignored.mp4", 50000000) == {}
    assert "read" not in events
    assert "closed" in events and "client closed" in events


@pytest.mark.asyncio
async def test_http_adapter_does_not_combine_different_media_versions(monkeypatch):
    import httpx
    import server
    content = box(b"ftyp", b"isom") + box(b"mdat", b"x" * 100000) + moov()
    calls = []
    async def opened(url, **kwargs):
        calls.append(kwargs)
        start, end = map(int, kwargs["range_header"][6:].split("-"))
        etag = '"version-one"' if len(calls) == 1 else '"version-two"'
        response = httpx.Response(206, content=content[start:end + 1], headers={"content-range": f"bytes {start}-{end}/{len(content)}", "etag": etag})
        class Client:
            async def aclose(self):
                pass
        return Client(), response
    monkeypatch.setattr(server, "open_media", opened)
    result = await server.probe_original_video_metadata("https://media.example/changing-original.mp4", len(content))
    assert result == {} and len(calls) == 2
    assert calls[1]["if_range"] == '"version-one"'
