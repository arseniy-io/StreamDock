from __future__ import annotations

from threading import Event

import pytest
import yt_dlp

from app.services.playlist_analyzer import (
    MAX_QUEUE_ITEMS,
    QueueAnalysisCancelled,
    QueueAnalysisError,
    analyze_queue,
)


def _fake_ydl(monkeypatch, info_by_url: dict[str, dict], *, on_extract=None) -> list[dict]:
    captured_options: list[dict] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict) -> None:
            captured_options.append(options)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def extract_info(self, url: str, download: bool) -> dict:
            assert download is False
            if on_extract:
                on_extract(url)
            return info_by_url[url]

    monkeypatch.setattr("app.services.playlist_analyzer.yt_dlp.YoutubeDL", FakeYoutubeDL)
    return captured_options


def test_playlist_is_flat_deduplicated_and_limited(monkeypatch) -> None:
    playlist_url = "https://www.youtube.com/playlist?list=demo"
    entries = [
        {
            "id": f"video-{index}",
            "extractor_key": "Youtube",
            "webpage_url": f"https://www.youtube.com/watch?v=video-{index}",
            "title": f"  Урок   {index}  ",
            "duration": 60 + index,
        }
        for index in range(MAX_QUEUE_ITEMS + 1)
    ]
    entries.insert(1, dict(entries[0]))
    captured = _fake_ydl(
        monkeypatch,
        {
            playlist_url: {
                "title": "Курс по Python",
                "playlist_count": 80,
                "entries": entries,
            }
        },
    )

    result = analyze_queue(playlist_url)

    assert result.is_playlist is True
    assert result.source_title == "Курс по Python"
    assert result.truncated is True
    assert len(result.items) == MAX_QUEUE_ITEMS
    assert result.items[0].index == 1
    assert result.items[0].title == "Урок 0"
    assert result.items[0].duration_seconds == 60
    assert result.items[0].duration_text == "01:00"
    assert result.items[-1].index == MAX_QUEUE_ITEMS
    assert captured[0]["extract_flat"] is True
    assert captured[0]["skip_download"] is True
    assert captured[0]["noplaylist"] is False
    assert captured[0]["playlistend"] == MAX_QUEUE_ITEMS + 1


def test_single_url_returns_one_queue_item(monkeypatch) -> None:
    url = "https://vimeo.com/123456"
    _fake_ydl(
        monkeypatch,
        {
            url: {
                "id": "123456",
                "extractor_key": "Vimeo",
                "webpage_url": url,
                "title": "Доклад",
                "duration": 3661.9,
            }
        },
    )

    result = analyze_queue(url)

    assert result.is_playlist is False
    assert result.truncated is False
    assert result.items[0].url == url
    assert result.items[0].duration_seconds == 3661
    assert result.items[0].duration_text == "01:01:01"


def test_url_list_keeps_order_and_removes_media_duplicates(monkeypatch) -> None:
    first = "https://www.youtube.com/watch?v=first"
    duplicate = "https://youtu.be/first"
    second = "https://rutube.ru/video/second/"
    captured = _fake_ydl(
        monkeypatch,
        {
            first: {
                "id": "first",
                "extractor_key": "Youtube",
                "webpage_url": first,
                "title": "Первое видео",
                "duration": 12,
            },
            duplicate: {
                "id": "first",
                "extractor_key": "Youtube",
                "webpage_url": duplicate,
                "title": "Дубликат",
                "duration": 12,
            },
            second: {
                "id": "second",
                "extractor_key": "Rutube",
                "webpage_url": second,
                "title": "Второе видео",
                "duration": None,
            },
        },
    )

    result = analyze_queue([first, duplicate, second])

    assert [item.index for item in result.items] == [1, 2]
    assert [item.title for item in result.items] == ["Первое видео", "Второе видео"]
    assert result.items[1].duration_text == "Неизвестно"
    assert result.is_playlist is False
    assert captured[0]["noplaylist"] is True


def test_list_is_capped_at_fifty_unique_urls(monkeypatch) -> None:
    urls = [f"https://www.youtube.com/watch?v=item-{index}" for index in range(52)]
    info = {
        url: {
            "id": f"item-{index}",
            "extractor_key": "Youtube",
            "webpage_url": url,
            "title": f"Видео {index}",
        }
        for index, url in enumerate(urls[:MAX_QUEUE_ITEMS])
    }
    _fake_ydl(monkeypatch, info)

    result = analyze_queue(urls)

    assert len(result.items) == MAX_QUEUE_ITEMS
    assert result.truncated is True


@pytest.mark.parametrize(
    "source",
    [
        [],
        ["javascript:alert(1)"],
        ["https://example.com/video"],
        ["https://www.youtube.com/watch?v=ok", 42],
    ],
)
def test_invalid_queue_input_has_friendly_error(source) -> None:
    with pytest.raises(QueueAnalysisError) as error:
        analyze_queue(source)

    assert str(error.value)
    assert "Traceback" not in str(error.value)


def test_cancel_before_analysis_does_not_start_yt_dlp(monkeypatch) -> None:
    cancel_event = Event()
    cancel_event.set()

    def fail_if_created(_options):
        raise AssertionError("yt-dlp не должен запускаться после отмены")

    monkeypatch.setattr("app.services.playlist_analyzer.yt_dlp.YoutubeDL", fail_if_created)

    with pytest.raises(QueueAnalysisCancelled, match="отменён"):
        analyze_queue("https://www.youtube.com/playlist?list=demo", cancel_event)


def test_cancel_during_url_list_stops_before_next_url(monkeypatch) -> None:
    first = "https://www.youtube.com/watch?v=first"
    second = "https://www.youtube.com/watch?v=second"
    cancel_event = Event()
    extracted: list[str] = []

    def cancel_after_first(url: str) -> None:
        extracted.append(url)
        cancel_event.set()

    _fake_ydl(
        monkeypatch,
        {
            first: {"webpage_url": first, "title": "Первое"},
            second: {"webpage_url": second, "title": "Второе"},
        },
        on_extract=cancel_after_first,
    )

    with pytest.raises(QueueAnalysisCancelled):
        analyze_queue([first, second], cancel_event)

    assert extracted == [first]


def test_cancel_wrapped_by_yt_dlp_is_still_reported_as_cancellation(monkeypatch) -> None:
    url = "https://www.youtube.com/watch?v=first"
    cancel_event = Event()

    class FakeYoutubeDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def extract_info(self, _url: str, download: bool) -> dict:
            cancel_event.set()
            raise yt_dlp.utils.DownloadError("Interrupted")

    monkeypatch.setattr("app.services.playlist_analyzer.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(QueueAnalysisCancelled):
        analyze_queue([url], cancel_event)


def test_private_playlist_error_is_friendly(monkeypatch) -> None:
    class FakeYoutubeDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def extract_info(self, _url: str, download: bool) -> dict:
            raise yt_dlp.utils.DownloadError("Private playlist")

    monkeypatch.setattr("app.services.playlist_analyzer.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(QueueAnalysisError, match="приватный"):
        analyze_queue("https://www.youtube.com/playlist?list=private")


def test_unsafe_entry_from_extractor_is_ignored(monkeypatch) -> None:
    playlist_url = "https://www.youtube.com/playlist?list=demo"
    safe_url = "https://www.youtube.com/watch?v=safe"
    _fake_ydl(
        monkeypatch,
        {
            playlist_url: {
                "title": "Плейлист",
                "entries": [
                    {"webpage_url": "file:///C:/secret.txt", "title": "Опасный"},
                    {"webpage_url": safe_url, "title": "Безопасный"},
                ],
            }
        },
    )

    result = analyze_queue(playlist_url)

    assert [item.url for item in result.items] == [safe_url]
    assert result.items[0].index == 1
