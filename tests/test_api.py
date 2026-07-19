import shutil

from fastapi.testclient import TestClient

from app.config import APP_VERSION
from app.main import app
from app.models.schemas import VideoInfoResponse, VideoQuality
from app.services.playlist_analyzer import QueueAnalysis, QueueAnalysisError, QueueItem
from app.services.task_manager import TaskRecord


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION}
    assert response.headers["X-StreamDock-App"] == "1"
    assert response.headers["X-StreamDock-Version"] == APP_VERSION


def test_frontend_assets_are_available_from_root() -> None:
    page = client.get("/")
    styles = client.get("/styles.css")
    script = client.get("/app.js")

    assert page.status_code == 200
    assert 'href="./styles.css?v=' in page.text
    assert 'src="./app.js?v=' in page.text
    assert 'id="transcription-engine"' in page.text
    assert '<option value="hybrid" selected>' in page.text
    assert 'id="text-source"' in page.text
    assert 'id="custom-terms"' in page.text
    assert 'id="queue-panel"' in page.text
    assert 'id="range-selector"' in page.text
    assert 'id="transcript-editor"' in page.text
    assert 'id="diarize-speakers"' in page.text
    assert 'id="speaker-count"' in page.text
    assert page.headers["cache-control"] == "no-store, max-age=0"
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
    assert styles.headers["cache-control"] == "no-store, max-age=0"
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "no-store, max-age=0"
    assert "engine: transcriptionEngine.value" in script.text
    assert "function isOperationBusy()" in script.text
    assert "fetch('/api/tasks/queue'" in script.text
    assert "start_seconds: Number(rangeStart.value)" in script.text
    assert "custom_terms: selectedCustomTerms()" in script.text
    assert "diarize_speakers: diarizeSpeakers.checked" in script.text
    assert "speaker_diarization: 'Разделение по спикерам'" in script.text


def test_video_info_uses_analyzer(monkeypatch) -> None:
    expected = VideoInfoResponse(
        source_url="https://youtu.be/abcdefghijk",
        source_name="YouTube",
        title="Тестовое видео",
        author="Тестовый канал",
        duration_seconds=90,
        duration_text="01:30",
        video_qualities=[VideoQuality(height=720, label="720p", container="MP4")],
        audio_options=[],
        default_quality=720,
    )

    monkeypatch.setattr("app.api.video.analyze_video", lambda _: expected)
    response = client.post("/api/video/info", json={"url": "https://youtu.be/abcdefghijk"})

    assert response.status_code == 200
    assert response.json()["title"] == "Тестовое видео"
    assert response.json()["default_quality"] == 720


def test_video_info_rejects_non_youtube_url() -> None:
    response = client.post("/api/video/info", json={"url": "https://example.com/video"})
    assert response.status_code == 422


def test_create_video_download_returns_task_id(monkeypatch) -> None:
    task = TaskRecord(task_id="test-task")
    monkeypatch.setattr("app.api.tasks.task_manager.create_video_download", lambda url, height: task)

    response = client.post(
        "/api/tasks/video",
        json={"url": "https://youtu.be/abcdefghijk", "height": 720},
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "test-task"}


def test_create_video_download_rejects_invalid_height() -> None:
    response = client.post(
        "/api/tasks/video",
        json={"url": "https://youtu.be/abcdefghijk", "height": 10000},
    )
    assert response.status_code == 422


def test_create_transcription_returns_task_id(monkeypatch) -> None:
    task = TaskRecord(task_id="transcription-task")

    def fake_create(url, **options):
        assert options["engine"] == "hybrid"
        assert options["model_name"] == "small"
        assert options["formats"] == ("md", "vtt")
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_transcription", fake_create)
    response = client.post(
        "/api/tasks/transcription",
        json={
            "url": "https://youtu.be/abcdefghijk",
            "model": "small",
            "language": "auto",
            "formats": ["md", "vtt"],
            "include_timestamps": True,
            "paragraphize": True,
            "remove_short_fragments": True,
        },
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "transcription-task"}


def test_create_transcription_requires_output_format() -> None:
    response = client.post(
        "/api/tasks/transcription",
        json={"url": "https://youtu.be/abcdefghijk", "formats": []},
    )
    assert response.status_code == 422


def test_gigaam_rejects_explicit_english_language() -> None:
    response = client.post(
        "/api/tasks/transcription",
        json={
            "url": "https://youtu.be/abcdefghijk",
            "engine": "gigaam",
            "language": "en",
            "formats": ["md"],
        },
    )
    assert response.status_code == 422
    assert "Для английского выберите Whisper" in response.json()["detail"]


def test_create_audio_download_returns_task_id(monkeypatch) -> None:
    task = TaskRecord(task_id="audio-task")

    def fake_create(url, **options):
        assert options == {"output_format": "mp3", "bitrate_kbps": 192}
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_audio_download", fake_create)
    response = client.post(
        "/api/tasks/audio",
        json={
            "url": "https://vimeo.com/123456789",
            "format": "mp3",
            "bitrate_kbps": 192,
        },
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "audio-task"}


def test_extension_download_requires_local_extension_marker() -> None:
    response = client.post(
        "/api/extension/download",
        json={
            "stream_url": "https://media.example.com/live.m3u8",
            "title": "Тестовый эфир",
            "stream_kind": "hls",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Запрос доступен только локальному расширению"


def test_extension_download_creates_background_task(monkeypatch) -> None:
    task = TaskRecord(task_id="extension-task")
    captured: dict = {}

    monkeypatch.setattr("app.api.extension.validate_public_media_url", lambda url: url)

    def fake_create(url, **options):
        captured["url"] = url
        captured.update(options)
        return task

    monkeypatch.setattr("app.api.extension.task_manager.create_extension_download", fake_create)
    response = client.post(
        "/api/extension/download",
        headers={"X-StreamDock-Extension": "1"},
        json={
            "stream_url": "https://media.example.com/live.m3u8?token=secret",
            "page_url": "https://training.example.com/lesson/1",
            "title": "Тестовый эфир",
            "stream_kind": "hls",
            "request_headers": {"Authorization": "Bearer test", "X-Unsafe": "ignored"},
            "client_request_id": "download-job-1234",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "extension-task"}
    assert captured["title"] == "Тестовый эфир"
    assert captured["client_request_id"] == "download-job-1234"
    assert captured["request_headers"] == {
        "Authorization": "Bearer test",
        "Referer": "https://training.example.com/lesson/1",
    }


def test_extension_download_accepts_legacy_marker(monkeypatch) -> None:
    monkeypatch.setattr("app.api.extension.validate_public_media_url", lambda url: url)
    monkeypatch.setattr(
        "app.api.extension.task_manager.create_extension_download",
        lambda *_args, **_kwargs: TaskRecord(task_id="legacy-extension-task"),
    )

    response = client.post(
        "/api/extension/download",
        headers={"X-Save-Video-Extension": "1"},
        json={"stream_url": "https://media.example.com/video.mp4", "stream_kind": "video"},
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "legacy-extension-task"}


def test_create_file_transcription_streams_to_managed_temporary_file(monkeypatch) -> None:
    task = TaskRecord(task_id="file-transcription-task")

    def fake_create(source_path, original_name, **options):
        assert source_path.name == "source.mp4"
        assert source_path.read_bytes() == b"local-media"
        assert original_name == "Мой урок.mp4"
        assert options["engine"] == "hybrid"
        assert options["model_name"] == "large-v3"
        assert options["formats"] == ("md", "srt")
        shutil.rmtree(source_path.parent, ignore_errors=True)
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_file_transcription", fake_create)
    response = client.post(
        "/api/tasks/transcription/file",
        params={
            "filename": "Мой урок.mp4",
            "model": "large-v3",
            "language": "ru",
            "formats": "md,srt",
        },
        content=b"local-media",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "file-transcription-task"}


def test_create_file_transcription_rejects_unsupported_extension() -> None:
    response = client.post(
        "/api/tasks/transcription/file",
        params={"filename": "archive.exe"},
        content=b"not-media",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422


def test_task_status_exposes_current_stage_details(monkeypatch) -> None:
    task = TaskRecord(
        task_id="progress-details",
        status="running",
        stage="transcribing",
        progress=25,
        message="Распознаём речь",
        processed_seconds=300,
        total_seconds=1200,
        eta_seconds=900,
    )
    monkeypatch.setattr("app.api.tasks.task_manager.get", lambda _: task)

    response = client.get("/api/tasks/progress-details")

    assert response.status_code == 200
    assert response.json()["processed_seconds"] == 300
    assert response.json()["total_seconds"] == 1200
    assert response.json()["eta_seconds"] == 900


def test_delete_terminal_task_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr("app.api.tasks.task_manager.remove_terminal", lambda _: False)

    response = client.delete("/api/tasks/already-removed")

    assert response.status_code == 204


def test_delete_active_task_requires_completed_cancellation(monkeypatch) -> None:
    def reject_active(_):
        raise ValueError("active")

    monkeypatch.setattr("app.api.tasks.task_manager.remove_terminal", reject_active)

    response = client.delete("/api/tasks/still-running")

    assert response.status_code == 409
    assert response.json()["detail"] == "Сначала дождитесь завершения отмены"


def test_open_folder_reveals_downloaded_file(monkeypatch, tmp_path) -> None:
    media_file = tmp_path / "downloaded lesson.mp4"
    media_file.write_bytes(b"video")
    task = TaskRecord(
        task_id="reveal-file",
        status="completed",
        stage="completed",
        files=[media_file],
    )
    revealed: list = []
    monkeypatch.setattr("app.api.tasks.DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr("app.api.tasks.task_manager.get", lambda _: task)
    monkeypatch.setattr("app.api.tasks.reveal_file", lambda path: revealed.append(path) or "selected")

    response = client.post("/api/tasks/reveal-file/open-folder")

    assert response.status_code == 200
    assert response.json() == {"status": "selected"}
    assert revealed == [media_file.resolve()]


def test_open_folder_returns_safe_error_when_explorer_fails(monkeypatch, tmp_path) -> None:
    from app.services.desktop import DesktopRevealError

    media_file = tmp_path / "downloaded lesson.mp4"
    media_file.write_bytes(b"video")
    task = TaskRecord(task_id="reveal-error", status="completed", files=[media_file])
    monkeypatch.setattr("app.api.tasks.DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr("app.api.tasks.task_manager.get", lambda _: task)

    def fail_to_reveal(_path):
        raise DesktopRevealError("Не удалось открыть папку с файлом")

    monkeypatch.setattr("app.api.tasks.reveal_file", fail_to_reveal)

    response = client.post("/api/tasks/reveal-error/open-folder")

    assert response.status_code == 500
    assert response.json() == {"detail": "Не удалось открыть папку с файлом"}


def test_queue_analysis_passes_multiline_source_as_url_list(monkeypatch) -> None:
    captured = {}

    def fake_analyze(source):
        captured["source"] = source
        return QueueAnalysis(
            items=[
                QueueItem(
                    index=1,
                    url="https://youtu.be/abcdefghijk",
                    title="Первое видео",
                    duration_seconds=75,
                    duration_text="01:15",
                )
            ],
            source_title=None,
            is_playlist=False,
            truncated=False,
        )

    monkeypatch.setattr("app.api.video.analyze_queue", fake_analyze)
    response = client.post(
        "/api/video/queue",
        json={"source": " https://youtu.be/abcdefghijk\n\nhttps://vimeo.com/123456789 "},
    )

    assert response.status_code == 200
    assert captured["source"] == [
        "https://youtu.be/abcdefghijk",
        "https://vimeo.com/123456789",
    ]
    assert response.json()["items"][0]["title"] == "Первое видео"


def test_queue_analysis_returns_friendly_validation_error(monkeypatch) -> None:
    def fail(_source):
        raise QueueAnalysisError("Плейлист недоступен")

    monkeypatch.setattr("app.api.video.analyze_queue", fail)
    response = client.post("/api/video/queue", json={"source": "https://youtu.be/abcdefghijk"})

    assert response.status_code == 422
    assert response.json() == {"detail": "Плейлист недоступен"}


def test_video_download_passes_selected_time_range(monkeypatch) -> None:
    task = TaskRecord(task_id="fragment-task")
    captured = {}

    def fake_create(url, height, **options):
        captured.update(url=url, height=height, **options)
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_video_download", fake_create)
    response = client.post(
        "/api/tasks/video",
        json={
            "url": "https://youtu.be/abcdefghijk",
            "height": 720,
            "start_seconds": 60,
            "end_seconds": 125.5,
        },
    )

    assert response.status_code == 202
    assert captured["start_seconds"] == 60
    assert captured["end_seconds"] == 125.5


def test_transcription_passes_source_terms_and_range(monkeypatch) -> None:
    task = TaskRecord(task_id="advanced-transcription")
    captured = {}

    def fake_create(url, **options):
        captured.update(url=url, **options)
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_transcription", fake_create)
    response = client.post(
        "/api/tasks/transcription",
        json={
            "url": "https://youtu.be/abcdefghijk",
            "text_source": "subtitles",
            "custom_terms": ["Kubernetes", "LLM"],
            "diarize_speakers": True,
            "speaker_count": 2,
            "start_seconds": 15,
            "end_seconds": 90,
        },
    )

    assert response.status_code == 202
    assert captured["text_source"] == "subtitles"
    assert captured["custom_terms"] == ("Kubernetes", "LLM")
    assert captured["diarize_speakers"] is True
    assert captured["speaker_count"] == 2
    assert captured["start_seconds"] == 15
    assert captured["end_seconds"] == 90


def test_local_file_transcription_parses_custom_terms(monkeypatch) -> None:
    task = TaskRecord(task_id="local-terms")
    captured = {}

    def fake_create(source_path, original_name, **options):
        captured["terms"] = options["custom_terms"]
        captured["diarize_speakers"] = options["diarize_speakers"]
        captured["speaker_count"] = options["speaker_count"]
        shutil.rmtree(source_path.parent, ignore_errors=True)
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_file_transcription", fake_create)
    response = client.post(
        "/api/tasks/transcription/file",
        params={
            "filename": "lesson.mp4",
            "custom_terms": "Kubernetes, LLM\nKubernetes, CI/CD",
            "diarize_speakers": "true",
            "speaker_count": "3",
        },
        content=b"local-media",
    )

    assert response.status_code == 202
    assert captured["terms"] == ("Kubernetes", "LLM", "CI/CD")
    assert captured["diarize_speakers"] is True
    assert captured["speaker_count"] == 3


def test_create_queue_task_passes_selected_items_and_settings(monkeypatch) -> None:
    task = TaskRecord(task_id="queue-task")
    captured = {}

    def fake_create(items, **options):
        captured["items"] = items
        captured.update(options)
        return task

    monkeypatch.setattr("app.api.tasks.task_manager.create_queue_task", fake_create)
    response = client.post(
        "/api/tasks/queue",
        json={
            "items": [
                {"url": "https://youtu.be/abcdefghijk", "title": "Первое"},
                {"url": "https://vimeo.com/123456789", "title": "Второе"},
            ],
            "action": "transcription",
            "formats": ["md", "srt"],
            "custom_terms": ["DevOps"],
            "diarize_speakers": True,
            "speaker_count": 4,
        },
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "queue-task"}
    assert [item["title"] for item in captured["items"]] == ["Первое", "Второе"]
    assert captured["formats"] == ("md", "srt")
    assert captured["custom_terms"] == ("DevOps",)
    assert captured["diarize_speakers"] is True
    assert captured["speaker_count"] == 4


def test_transcription_rejects_invalid_speaker_count() -> None:
    response = client.post(
        "/api/tasks/transcription",
        json={
            "url": "https://youtu.be/abcdefghijk",
            "diarize_speakers": True,
            "speaker_count": 11,
        },
    )

    assert response.status_code == 422


def test_task_status_exposes_queue_item_errors(monkeypatch) -> None:
    task = TaskRecord(
        task_id="queue-errors",
        status="completed",
        item_errors=[{"title": "Недоступное видео", "message": "Видео удалено"}],
    )
    monkeypatch.setattr("app.api.tasks.task_manager.get", lambda _task_id: task)

    response = client.get("/api/tasks/queue-errors")

    assert response.status_code == 200
    assert response.json()["item_errors"] == [
        {"title": "Недоступное видео", "message": "Видео удалено"}
    ]


def test_completed_markdown_transcript_can_be_read_and_saved_atomically(
    monkeypatch,
    tmp_path,
) -> None:
    markdown = tmp_path / "Видео - транскрибация.md"
    markdown.write_text("# Было", encoding="utf-8")
    task = TaskRecord(
        task_id="editable-transcript",
        status="completed",
        stage="completed",
        files=[markdown],
    )
    monkeypatch.setattr("app.api.tasks.DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr("app.api.tasks.task_manager.get", lambda _task_id: task)

    loaded = client.get("/api/tasks/editable-transcript/transcript")
    saved = client.patch(
        "/api/tasks/editable-transcript/transcript",
        json={"content": "# Стало\n\nИсправленный текст."},
    )

    assert loaded.status_code == 200
    assert loaded.json()["content"] == "# Было"
    assert saved.status_code == 200
    assert markdown.read_text(encoding="utf-8") == "# Стало\n\nИсправленный текст."
    assert list(tmp_path.glob("*.tmp")) == []


def test_transcript_editor_rejects_unfinished_task(monkeypatch) -> None:
    task = TaskRecord(task_id="still-running", status="running")
    monkeypatch.setattr("app.api.tasks.task_manager.get", lambda _task_id: task)

    response = client.get("/api/tasks/still-running/transcript")

    assert response.status_code == 409
