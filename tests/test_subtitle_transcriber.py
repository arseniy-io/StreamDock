from pathlib import Path
from threading import Event

import pytest
import yt_dlp

from app.services.downloader import DownloadCancelled
from app.services import subtitle_transcriber
from app.services.subtitle_transcriber import (
    SubtitleTranscriptionError,
    SubtitlesUnavailableError,
    parse_subtitle_text,
    select_subtitle_track,
    transcribe_from_subtitles,
)


def _track(file_format: str = "vtt", name: str | None = None) -> list[dict]:
    return [{"ext": file_format, "url": f"https://cdn.example/subtitle.{file_format}", "name": name}]


def test_select_track_prioritizes_manual_russian_over_other_tracks() -> None:
    metadata = {
        "subtitles": {"en": _track(), "ru-RU": _track(name="Русский")},
        "automatic_captions": {"ru": _track(), "en": _track()},
    }

    selected = select_subtitle_track(metadata, "en")

    assert selected is not None
    assert selected.language == "ru-RU"
    assert selected.kind == "manual"
    assert selected.file_format == "vtt"


def test_select_track_uses_russian_automatic_before_selected_language() -> None:
    metadata = {
        "subtitles": {"en": _track("srt")},
        "automatic_captions": {"ru": _track()},
    }

    selected = select_subtitle_track(metadata, "en")

    assert selected is not None
    assert (selected.language, selected.kind) == ("ru", "automatic")


def test_select_track_uses_selected_language_and_ignores_unsafe_or_unknown_formats() -> None:
    metadata = {
        "subtitles": {
            "../../ru": _track(),
            "ru": [{"ext": "json3", "url": "https://cdn.example/subtitle.json3"}],
            "en-US": _track("srt"),
        }
    }

    selected = select_subtitle_track(metadata, "en")

    assert selected is not None
    assert selected.language == "en-US"
    assert selected.file_format == "srt"
    assert select_subtitle_track({"subtitles": {"ru": _track("json3")}}, "ru") is None


def test_parse_vtt_removes_tags_and_rolling_caption_duplicates() -> None:
    content = """WEBVTT

00:00:01.000 --> 00:00:05.000
<c>Запускаем Docker</c>

00:00:03.000 --> 00:00:07.000
Запускаем Docker и Kubernetes

00:00:06.000 --> 00:00:09.000
Docker и Kubernetes в облаке

00:00:10.000 --> 00:00:12.000
Готово &amp; проверено
"""

    segments = parse_subtitle_text(content, file_format="vtt")

    assert [segment.text for segment in segments] == [
        "Запускаем Docker",
        "и Kubernetes",
        "в облаке",
        "Готово & проверено",
    ]
    assert segments[0].start == 1
    assert segments[-1].end == 12


def test_parse_vtt_removes_contiguous_youtube_rolling_lines() -> None:
    content = """WEBVTT

00:00:06.950 --> 00:00:06.960
чтобы контекст не становился слишком

00:00:06.960 --> 00:00:08.910
чтобы контекст не становился слишком
большим?

00:00:08.910 --> 00:00:08.920
большим?

00:00:08.920 --> 00:00:14.030
большим?
А, ну, на самом деле
"""

    segments = parse_subtitle_text(content, file_format="vtt")

    assert [segment.text for segment in segments] == [
        "чтобы контекст не становился слишком",
        "большим?",
        "А, ну, на самом деле",
    ]


def test_parse_srt_filters_fragment_without_resetting_timestamps() -> None:
    content = """1
00:00:05,000 --> 00:00:12,000
Первый фрагмент

2
00:00:12,000 --> 00:00:20,000
Второй фрагмент

3
00:00:20,000 --> 00:00:25,000
Третий фрагмент
"""

    segments = parse_subtitle_text(
        content,
        file_format="srt",
        start_seconds=10,
        end_seconds=22,
    )

    assert [(item.start, item.end, item.text) for item in segments] == [
        (10, 12, "Первый фрагмент"),
        (12, 20, "Второй фрагмент"),
        (20, 22, "Третий фрагмент"),
    ]


def test_parse_rejects_invalid_range_and_supports_cancellation() -> None:
    with pytest.raises(SubtitleTranscriptionError, match="позже начала"):
        parse_subtitle_text("", file_format="vtt", start_seconds=20, end_seconds=10)

    cancelled = Event()
    cancelled.set()
    with pytest.raises(DownloadCancelled):
        parse_subtitle_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nТекст",
            file_format="vtt",
            cancel_event=cancelled,
        )


def test_transcribe_downloads_only_manual_subtitles_and_returns_segments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    metadata = {"subtitles": {"ru": _track()}}
    captured: dict = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            captured["url"] = url
            captured["download"] = download
            output_template = Path(captured["outtmpl"])
            output_template.with_name("subtitle.ru.vtt").write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nГотовый текст",
                encoding="utf-8",
            )
            return metadata

    monkeypatch.setattr(subtitle_transcriber.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    progress = []

    result = transcribe_from_subtitles(
        "https://www.youtube.com/watch?v=test",
        lambda stage, percent, message, details=None: progress.append((stage, percent, details)),
        Event(),
        metadata=metadata,
        language="ru",
        temp_parent=tmp_path,
    )

    assert captured["skip_download"] is True
    assert captured["writesubtitles"] is True
    assert captured["writeautomaticsub"] is False
    assert captured["subtitleslangs"] == ["ru"]
    assert captured["download"] is True
    assert result.segments[0].text == "Готовый текст"
    assert result.track.source_label == "ручные субтитры"
    assert progress[-1][0:2] == ("subtitles_ready", 100)
    assert not list(tmp_path.glob("local-subtitles-*"))


def test_transcribe_can_fetch_metadata_and_download_automatic_subtitles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    metadata = {"automatic_captions": {"ru": _track("srt")}}
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            calls.append((download, self.options.copy()))
            if download:
                Path(self.options["outtmpl"]).with_name("subtitle.ru.srt").write_text(
                    "1\n00:00:02,000 --> 00:00:04,000\nАвтоматический текст",
                    encoding="utf-8",
                )
            return metadata

    monkeypatch.setattr(subtitle_transcriber.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    result = transcribe_from_subtitles(
        "https://youtu.be/test",
        lambda *args: None,
        Event(),
        temp_parent=tmp_path,
    )

    assert [download for download, _ in calls] == [False, True]
    assert calls[0][1]["skip_download"] is True
    assert calls[1][1]["writeautomaticsub"] is True
    assert calls[1][1]["writesubtitles"] is False
    assert result.track.kind == "automatic"


def test_transcribe_reports_unavailable_subtitles_without_starting_download(monkeypatch) -> None:
    monkeypatch.setattr(
        subtitle_transcriber.yt_dlp,
        "YoutubeDL",
        lambda *_: pytest.fail("Скачивание не должно запускаться"),
    )

    with pytest.raises(SubtitlesUnavailableError, match="локальное распознавание"):
        transcribe_from_subtitles(
            "https://www.youtube.com/watch?v=test",
            lambda *args: None,
            Event(),
            metadata={"subtitles": {}},
        )


def test_transcribe_cancels_download_from_progress_hook(monkeypatch, tmp_path: Path) -> None:
    metadata = {"subtitles": {"ru": _track()}}
    cancelled = Event()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            cancelled.set()
            self.options["progress_hooks"][0]({"status": "downloading"})

    monkeypatch.setattr(subtitle_transcriber.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    with pytest.raises(DownloadCancelled):
        transcribe_from_subtitles(
            "https://www.youtube.com/watch?v=test",
            lambda *args: None,
            cancelled,
            metadata=metadata,
            temp_parent=tmp_path,
        )


def test_transcribe_converts_yt_dlp_failure_to_friendly_error(monkeypatch, tmp_path: Path) -> None:
    metadata = {"subtitles": {"ru": _track()}}

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            raise yt_dlp.utils.DownloadError("network failure")

    monkeypatch.setattr(subtitle_transcriber.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    with pytest.raises(SubtitleTranscriptionError, match="Не удалось скачать готовые субтитры"):
        transcribe_from_subtitles(
            "https://www.youtube.com/watch?v=test",
            lambda *args: None,
            Event(),
            metadata=metadata,
            temp_parent=tmp_path,
        )
