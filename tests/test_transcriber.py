from pathlib import Path
from threading import Event
from types import SimpleNamespace

import ctranslate2
import faster_whisper
import pytest

from app.config import WHISPER_BEAM_SIZE, WHISPER_CPU_BATCH_SIZE, WHISPER_CPU_THREADS
from app.services import transcriber
from app.services.downloader import DownloadCancelled
from app.services.gigaam_transcriber import GigaamSegment
from app.services.markdown_builder import TranscriptSegment
from app.services.subtitle_transcriber import SubtitleTrack, SubtitleTranscript
from app.services.speaker_diarizer import SpeakerTurn


def test_cpu_model_uses_optimized_resource_profile(monkeypatch) -> None:
    captured = {}
    created = 0

    class FakeWhisperModel:
        def __init__(self, model_name, **options):
            nonlocal created
            created += 1
            captured["model_name"] = model_name
            captured.update(options)

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    monkeypatch.setattr(transcriber, "_model_is_cached", lambda _: True)

    transcriber._load_model.cache_clear()
    try:
        first_model, device_label, batch_size = transcriber._load_model("large-v3")
        second_model, _, _ = transcriber._load_model("large-v3")
    finally:
        transcriber._load_model.cache_clear()

    assert captured["device"] == "cpu"
    assert captured["compute_type"] == "int8"
    assert captured["cpu_threads"] == WHISPER_CPU_THREADS == 12
    assert captured["num_workers"] == 1
    assert captured["local_files_only"] is True
    assert batch_size == WHISPER_CPU_BATCH_SIZE == 4
    assert "12 потоков" in device_label
    assert first_model is second_model
    assert created == 1


def test_busy_transcription_reports_queue_and_can_be_cancelled(tmp_path: Path) -> None:
    source = tmp_path / "lesson.wav"
    source.write_bytes(b"audio")
    cancel_event = Event()
    progress = []

    assert transcriber._INFERENCE_LOCK.acquire(blocking=False)
    try:
        def callback(stage, percent, message, details=None):
            progress.append((stage, percent, message))
            if stage == "queued":
                cancel_event.set()

        with pytest.raises(DownloadCancelled):
            transcriber.transcribe_local_file(
                source,
                source.name,
                tmp_path / "result",
                callback,
                cancel_event,
                model_name="large-v3",
            )
    finally:
        transcriber._INFERENCE_LOCK.release()

    assert any(stage == "queued" and percent is None for stage, percent, _ in progress)


def test_local_transcription_uses_batched_fast_decoding(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "lesson.wav"
    source.write_bytes(b"audio")
    output = tmp_path / "result"
    captured = {}

    class FakePipeline:
        def __init__(self, model):
            captured["model"] = model

        def transcribe(self, media_path, **options):
            captured["media_path"] = media_path
            captured.update(options)
            segments = iter([
                SimpleNamespace(start=0.0, end=10.0, text=" Первый фрагмент."),
                SimpleNamespace(start=10.0, end=20.0, text=" Второй фрагмент."),
            ])
            return segments, SimpleNamespace(duration=20.0, language="ru")

    monkeypatch.setattr(transcriber, "_load_model", lambda _: (object(), "процессоре", 4))
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", FakePipeline)
    progress = []

    files = transcriber.transcribe_local_file(
        source,
        "lesson.wav",
        output,
        lambda stage, percent, message, details=None: progress.append((stage, percent, details)),
        Event(),
        model_name="large-v3",
        language="ru",
        formats=("md",),
    )

    assert captured["beam_size"] == WHISPER_BEAM_SIZE == 1
    assert captured["best_of"] == 1
    assert captured["batch_size"] == 4
    assert captured["without_timestamps"] is False
    assert captured["vad_filter"] is True
    assert any(stage == "transcribing" and percent == 50 for stage, percent, _ in progress)
    assert "- Файл: lesson.wav" in files[0].read_text(encoding="utf-8")


def test_local_gigaam_transcription_uses_new_engine_without_whisper(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "lesson.wav"
    source.write_bytes(b"audio")
    output = tmp_path / "result"
    waveform = __import__("numpy").zeros(160_000, dtype="float32")

    monkeypatch.setattr(transcriber, "_decode_source_audio", lambda *args: waveform)
    monkeypatch.setattr(
        transcriber,
        "recognize_gigaam",
        lambda *args: [GigaamSegment(0.0, 10.0, "Русская транскрибация.", -0.1)],
    )
    monkeypatch.setattr(
        transcriber,
        "_load_model",
        lambda *_: pytest.fail("Whisper не должен запускаться в режиме GigaAM"),
    )

    files = transcriber.transcribe_local_file(
        source,
        source.name,
        output,
        lambda *args: None,
        Event(),
        engine="gigaam",
        language="ru",
    )

    markdown = files[0].read_text(encoding="utf-8")
    assert "Русская транскрибация" in markdown
    assert "GigaAM v3 E2E RNNT" in markdown


def test_auto_mode_uses_ready_subtitles_without_downloading_audio(monkeypatch, tmp_path: Path) -> None:
    metadata = {
        "title": "Урок про Kubernetes",
        "channel": "Тестовый канал",
        "duration": 120,
    }
    monkeypatch.setattr(transcriber, "_extract_video_metadata", lambda *args: metadata)
    monkeypatch.setattr(
        transcriber,
        "transcribe_from_subtitles",
        lambda *args, **kwargs: SubtitleTranscript(
            segments=[TranscriptSegment(30.0, 36.0, "Разбираем Kubernetes и Docker.")],
            language="ru",
            track=SubtitleTrack("ru", "manual", "vtt", "Русский"),
        ),
    )
    monkeypatch.setattr(
        transcriber,
        "_download_audio",
        lambda *args, **kwargs: pytest.fail("Аудио не должно скачиваться при готовых субтитрах"),
    )
    monkeypatch.setattr(
        transcriber,
        "detect_speakers",
        lambda *args, **kwargs: pytest.fail("Разделение спикеров не должно запускаться без флажка"),
    )

    files = transcriber.transcribe_video(
        "https://youtu.be/abcdefghijk",
        tmp_path,
        lambda *args: None,
        Event(),
        text_source="auto",
        formats=("md", "srt"),
    )

    assert [path.suffix for path in files] == [".md", ".srt"]
    markdown = files[0].read_text(encoding="utf-8")
    assert "Готовые субтитры источника" in markdown
    assert "### 00:00:30" in markdown


def test_ready_subtitles_with_speakers_download_audio_and_keep_absolute_range(
    monkeypatch,
    tmp_path: Path,
) -> None:
    metadata = {"title": "Интервью", "channel": "Канал", "duration": 600}
    audio = tmp_path / "fragment.m4a"
    audio.write_bytes(b"audio")
    captured = {}
    monkeypatch.setattr(transcriber, "_extract_video_metadata", lambda *args: metadata)
    monkeypatch.setattr(
        transcriber,
        "transcribe_from_subtitles",
        lambda *args, **kwargs: SubtitleTranscript(
            segments=[TranscriptSegment(60.0, 65.0, "Ответ собеседника.")],
            language="ru",
            track=SubtitleTrack("ru", "manual", "vtt", "Русский"),
        ),
    )

    def fake_download(*args, **kwargs):
        captured.update(kwargs)
        return audio, metadata

    monkeypatch.setattr(transcriber, "_download_audio", fake_download)
    monkeypatch.setattr(
        transcriber,
        "detect_speakers",
        lambda *args, **kwargs: [SpeakerTurn(0.0, 5.0, 0)],
    )

    files = transcriber.transcribe_video(
        "https://youtu.be/abcdefghijk",
        tmp_path / "output",
        lambda *args: None,
        Event(),
        text_source="auto",
        formats=("md", "srt"),
        start_seconds=60,
        end_seconds=120,
        diarize_speakers=True,
        speaker_count=2,
    )

    markdown = files[0].read_text(encoding="utf-8")
    assert captured["start_seconds"] == 60
    assert captured["end_seconds"] == 120
    assert "### 00:01:00" in markdown
    assert "**Спикер 1:** Ответ собеседника." in markdown
    assert "разделение по спикерам" in markdown


def test_auto_mode_falls_back_to_local_speech_when_subtitles_are_missing(monkeypatch, tmp_path: Path) -> None:
    audio = tmp_path / "source.m4a"
    audio.write_bytes(b"audio")
    result = tmp_path / "fallback.md"
    result.write_text("# Результат", encoding="utf-8")
    stages = []

    monkeypatch.setattr(transcriber, "_extract_video_metadata", lambda *args: {"title": "Видео"})

    def no_subtitles(*args, **kwargs):
        raise transcriber.SubtitlesUnavailableError("Готовых субтитров нет")

    monkeypatch.setattr(transcriber, "transcribe_from_subtitles", no_subtitles)
    monkeypatch.setattr(transcriber, "_download_audio", lambda *args, **kwargs: (audio, {"title": "Видео"}))
    monkeypatch.setattr(transcriber, "_transcribe_source", lambda *args, **kwargs: [result])

    files = transcriber.transcribe_video(
        "https://youtu.be/abcdefghijk",
        tmp_path,
        lambda stage, percent, message, details=None: stages.append((stage, message)),
        Event(),
        text_source="auto",
    )

    assert files == [result]
    assert any("локальное распознавание" in message for _, message in stages)


def test_subtitles_only_mode_reports_missing_track_without_audio_download(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(transcriber, "_extract_video_metadata", lambda *args: {"title": "Видео"})
    monkeypatch.setattr(
        transcriber,
        "transcribe_from_subtitles",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            transcriber.SubtitlesUnavailableError("Готовых субтитров нет")
        ),
    )
    monkeypatch.setattr(
        transcriber,
        "_download_audio",
        lambda *args, **kwargs: pytest.fail("Аудио не должно скачиваться в строгом режиме субтитров"),
    )

    with pytest.raises(transcriber.TranscriptionError, match="Готовых субтитров нет"):
        transcriber.transcribe_video(
            "https://youtu.be/abcdefghijk",
            tmp_path,
            lambda *args: None,
            Event(),
            text_source="subtitles",
        )


def test_audio_range_falls_back_to_full_download_and_local_trim(monkeypatch, tmp_path: Path) -> None:
    fallback_calls = []

    class FailingYdl:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, *_args, **_kwargs):
            raise transcriber.yt_dlp.utils.DownloadError("range stream reset")

    def fake_fallback(url, work_directory, options, selected_range, *args):
        fallback_calls.append((url, selected_range, "download_ranges" in options))
        output = work_directory / "source.m4a"
        output.write_bytes(b"trimmed audio")
        return {"title": "Видео", "duration": 600}

    monkeypatch.setattr(transcriber.yt_dlp, "YoutubeDL", FailingYdl)
    monkeypatch.setattr(transcriber, "_download_full_audio_then_trim", fake_fallback)

    path, info = transcriber._download_audio(
        "https://youtu.be/abcdefghijk",
        tmp_path,
        lambda *args: None,
        Event(),
        start_seconds=60,
        end_seconds=120,
    )

    assert path.name == "source.m4a"
    assert info["duration"] == 60
    assert fallback_calls == [
        ("https://youtu.be/abcdefghijk", (60.0, 120.0), True)
    ]


def test_timestamp_offset_keeps_original_time_for_selected_fragment(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "fragment.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr(
        transcriber,
        "_recognize_with_whisper",
        lambda *args, **kwargs: ([TranscriptSegment(0.0, 5.0, "Текст фрагмента")], "ru", 5.0),
    )

    files = transcriber._transcribe_source_with_slot(
        source,
        tmp_path / "result",
        lambda *args: None,
        Event(),
        source_info={"title": "Видео"},
        source_value="https://youtu.be/abcdefghijk",
        source_label="Ссылка",
        engine="whisper",
        model_name="small",
        language="ru",
        formats=("md",),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        timestamp_offset=600.0,
    )

    assert "### 00:10:00" in files[0].read_text(encoding="utf-8")


def test_speech_diarization_labels_segments_before_timestamp_offset(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "fragment.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr(
        transcriber,
        "_recognize_with_whisper",
        lambda *args, **kwargs: ([TranscriptSegment(0.0, 5.0, "Вопрос")], "ru", 5.0),
    )
    monkeypatch.setattr(
        transcriber,
        "detect_speakers",
        lambda *args, **kwargs: [SpeakerTurn(0.0, 5.0, 0)],
    )

    files = transcriber._transcribe_source_with_slot(
        source,
        tmp_path / "result-with-speakers",
        lambda *args: None,
        Event(),
        source_info={"title": "Видео"},
        source_value="https://youtu.be/abcdefghijk",
        source_label="Ссылка",
        engine="whisper",
        model_name="small",
        language="ru",
        formats=("md",),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        diarize_speakers=True,
        speaker_count=2,
        timestamp_offset=600.0,
    )

    markdown = files[0].read_text(encoding="utf-8")
    assert "### 00:10:00" in markdown
    assert "**Спикер 1:** Вопрос" in markdown


def test_technical_glossary_prioritizes_custom_and_metadata_terms() -> None:
    glossary = transcriber._technical_glossary(
        {"title": "Kubernetes, Docker и CI/CD для DevOps"},
        text="Настраиваем контейнеры и деплой",
        custom_terms=("Argo CD",),
    )

    assert glossary[0] == "Argo CD"
    assert "Kubernetes" in glossary
    assert "Docker" in glossary
