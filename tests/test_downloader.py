import pytest

from app.config import YTDLP_CONCURRENT_FRAGMENTS
from app.services.downloader import (
    _apply_download_range,
    build_video_format_selector,
    format_duration,
    select_audio_options,
    select_subtitle_options,
    select_video_qualities,
    source_name_for_url,
    validate_video_url,
    validate_time_range,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://youtu.be/abcdefghijk",
        "http://m.youtube.com/watch?v=abcdefghijk",
        "https://rutube.ru/video/abcdefghijk/",
        "https://vkvideo.ru/video-1_2",
        "https://vimeo.com/123456789",
        "https://www.dailymotion.com/video/abc123",
        "https://www.tiktok.com/@author/video/123",
        "https://www.twitch.tv/videos/123",
        "https://soundcloud.com/author/track",
    ],
)
def test_validate_video_url_accepts_youtube(url: str) -> None:
    assert validate_video_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///C:/secret.txt",
        "https://youtube.com.evil.example/video",
        "https://example.com/video",
    ],
)
def test_validate_video_url_rejects_unsafe_or_foreign_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_video_url(url)


def test_format_duration() -> None:
    assert format_duration(65) == "01:05"
    assert format_duration(3661) == "01:01:01"
    assert format_duration(None) == "Неизвестно"


def test_source_name_is_derived_from_safe_domain() -> None:
    assert source_name_for_url("https://player.vimeo.com/video/123") == "Vimeo"
    assert source_name_for_url("https://m.youtube.com/watch?v=abc") == "YouTube"


def test_fragment_parallelism_is_fast_but_not_aggressive() -> None:
    assert YTDLP_CONCURRENT_FRAGMENTS == 8


def test_video_selector_prefers_ready_mp4_before_separate_streams() -> None:
    selector = build_video_format_selector(720)

    assert selector.startswith("best[height=720][ext=mp4][vcodec!=none][acodec!=none]/")
    assert "bestvideo[height=720][ext=mp4]+bestaudio[ext=m4a]" in selector


def test_select_video_qualities_removes_duplicates_and_prefers_mp4() -> None:
    formats = [
        {"format_id": "1", "height": 720, "ext": "webm", "vcodec": "vp9", "acodec": "none", "tbr": 900},
        {"format_id": "2", "height": 720, "ext": "mp4", "vcodec": "avc1", "acodec": "none", "tbr": 700},
        {"format_id": "3", "height": 360, "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "filesize": 1000},
        {"format_id": "4", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
    ]

    qualities = select_video_qualities(formats)

    assert [item.height for item in qualities] == [720, 360]
    assert qualities[0].container == "MP4"
    assert qualities[1].approximate_size == 1000


def test_select_audio_options_keeps_best_bitrate_per_container() -> None:
    formats = [
        {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 64},
        {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128},
        {"ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 96},
        {"ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "abr": 128},
    ]

    options = select_audio_options(formats)

    assert [(item.container, item.bitrate_kbps) for item in options] == [("M4A", 128), ("WEBM", 96)]


def test_select_subtitle_options_prioritizes_russian_manual_track() -> None:
    info = {
        "subtitles": {
            "en": [{"ext": "vtt", "name": "English"}],
            "ru": [{"ext": "vtt", "name": "Русский"}],
        },
        "automatic_captions": {
            "ru": [{"ext": "srt", "name": "Русский автоматически"}],
            "de": [{"ext": "json3", "name": "Deutsch"}],
        },
    }

    options = select_subtitle_options(info)

    assert [(item.language, item.automatic) for item in options] == [
        ("ru", False),
        ("ru", True),
        ("en", False),
    ]


def test_validate_time_range_clamps_end_to_media_duration() -> None:
    assert validate_time_range(10, 90, 60) == (10.0, 60.0)
    assert validate_time_range(None, None, 60) is None


@pytest.mark.parametrize("start,end", [(None, 10), (10, None), (10, 10.2), (80, 90)])
def test_validate_time_range_rejects_invalid_values(start, end) -> None:
    with pytest.raises(ValueError):
        validate_time_range(start, end, 60)


def test_download_range_uses_safe_yt_dlp_callable() -> None:
    options = {}

    _apply_download_range(options, (10.0, 20.0))

    assert list(options["download_ranges"]({}, None)) == [
        {"start_time": 10.0, "end_time": 20.0}
    ]
    assert options["force_keyframes_at_cuts"] is True
