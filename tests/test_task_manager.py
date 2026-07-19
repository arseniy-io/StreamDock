import time
from pathlib import Path
from threading import Event

import pytest

from app.services.downloader import DownloadCancelled, VideoAnalysisError
from app.services.task_manager import TaskManager, TaskRecord


def wait_for_terminal(manager: TaskManager, task_id: str, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task and task.status in {"completed", "failed", "cancelled"}:
            return
        time.sleep(0.01)
    raise AssertionError("Фоновая задача не завершилась")


def test_task_manager_completes_download(monkeypatch, tmp_path: Path) -> None:
    def fake_download(url, height, output_directory, progress_callback, cancel_event):
        assert url.startswith("https://")
        assert height == 720
        progress_callback("downloading", 50, "Скачиваем")
        result = output_directory / "result.mp4"
        result.write_bytes(b"video")
        return result.resolve()

    monkeypatch.setattr("app.services.task_manager.download_video", fake_download)
    manager = TaskManager(tmp_path)
    task = manager.create_video_download("https://youtu.be/abcdefghijk", 720)
    wait_for_terminal(manager, task.task_id)

    completed = manager.get(task.task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.progress == 100
    assert completed.files[0].name == "result.mp4"


def test_task_manager_cancels_download(monkeypatch, tmp_path: Path) -> None:
    def fake_download(url, height, output_directory, progress_callback, cancel_event):
        progress_callback("downloading", 5, "Скачиваем")
        if cancel_event.wait(timeout=1):
            raise DownloadCancelled("Отменено")
        raise AssertionError("Событие отмены не установлено")

    monkeypatch.setattr("app.services.task_manager.download_video", fake_download)
    manager = TaskManager(tmp_path)
    task = manager.create_video_download("https://youtu.be/abcdefghijk", 360)
    manager.cancel(task.task_id)
    wait_for_terminal(manager, task.task_id)

    cancelled = manager.get(task.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.files == []


def test_task_manager_deletes_file_returned_after_cancel_race(monkeypatch, tmp_path: Path) -> None:
    def fake_download(url, height, output_directory, progress_callback, cancel_event):
        result = output_directory / "late-result.mp4"
        result.write_bytes(b"video")
        cancel_event.set()
        return result.resolve()

    monkeypatch.setattr("app.services.task_manager.download_video", fake_download)
    manager = TaskManager(tmp_path)
    task = manager.create_video_download("https://youtu.be/abcdefghijk", 720)
    wait_for_terminal(manager, task.task_id)

    cancelled = manager.get(task.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.files == []
    assert not (tmp_path / "late-result.mp4").exists()


def test_task_manager_cancel_completed_download_deletes_file(monkeypatch, tmp_path: Path) -> None:
    def fake_download(url, height, output_directory, progress_callback, cancel_event):
        result = output_directory / "completed-result.mp4"
        result.write_bytes(b"video")
        return result.resolve()

    monkeypatch.setattr("app.services.task_manager.download_video", fake_download)
    manager = TaskManager(tmp_path)
    task = manager.create_video_download("https://youtu.be/abcdefghijk", 720)
    wait_for_terminal(manager, task.task_id)

    manager.cancel(task.task_id)
    wait_for_terminal(manager, task.task_id)
    cancelled = manager.get(task.task_id)

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.files == []
    assert not (tmp_path / "completed-result.mp4").exists()


def test_cancelled_task_cannot_be_removed_while_locked_file_cleanup_is_pending(
    monkeypatch, tmp_path: Path
) -> None:
    manager = TaskManager(tmp_path)
    output = (tmp_path / "locked-result.mp4").resolve()
    output.write_bytes(b"video")
    task = TaskRecord(task_id="locked-cleanup", status="completed", stage="completed", files=[output])
    manager._tasks[task.task_id] = task
    release_lock = Event()
    real_unlink = Path.unlink

    def locked_unlink(path: Path, *args, **kwargs):
        if path.resolve() == output and not release_lock.is_set():
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    cancelling = manager.cancel(task.task_id)

    assert cancelling is not None
    assert cancelling.status == "running"
    assert cancelling.stage == "cancelling"
    assert cancelling.cleanup_paths == [output]
    assert not cancelling.cleanup_complete.is_set()
    with pytest.raises(ValueError):
        manager.remove_terminal(task.task_id)

    release_lock.set()
    wait_for_terminal(manager, task.task_id)

    cancelled = manager.get(task.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.cleanup_paths == []
    assert cancelled.cleanup_complete.is_set()
    assert not output.exists()


def test_task_manager_cancels_all_active_tasks(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    pending = TaskRecord(task_id="pending")
    running = TaskRecord(task_id="running", status="running")
    completed = TaskRecord(task_id="completed", status="completed")
    failed = TaskRecord(task_id="failed", status="failed")
    manager._tasks = {
        task.task_id: task
        for task in (pending, running, completed, failed)
    }

    cancelled_count = manager.cancel_all()

    assert cancelled_count == 2
    assert pending.cancel_event.is_set()
    assert running.cancel_event.is_set()
    assert pending.message == "Отменяем операцию..."
    assert running.message == "Отменяем операцию..."
    assert not completed.cancel_event.is_set()
    assert not failed.cancel_event.is_set()


def test_progress_update_does_not_overwrite_cancelling_state(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    task = TaskRecord(task_id="cancel-progress")
    manager._tasks[task.task_id] = task

    manager.cancel(task.task_id)
    manager._update(task.task_id, "downloading", 75, "Скачиваем")

    updated = manager.get(task.task_id)
    assert updated is not None
    assert updated.stage == "cancelling"
    assert updated.progress is None
    assert updated.message == "Останавливаем загрузку и удаляем временные файлы..."


def test_task_manager_removes_cancelled_task_and_client_mapping(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    task = TaskRecord(task_id="cancelled-task", status="cancelled", stage="cancelled")
    manager._tasks[task.task_id] = task
    manager._extension_request_tasks["browser-job"] = task.task_id

    assert manager.remove_terminal(task.task_id) is True
    assert manager.get(task.task_id) is None
    assert manager._extension_request_tasks == {}
    assert manager.remove_terminal(task.task_id) is False


def test_extension_download_is_idempotent_for_client_request(monkeypatch, tmp_path: Path) -> None:
    def fake_download(
        stream_url,
        title,
        stream_kind,
        request_headers,
        output_directory,
        progress_callback,
        cancel_event,
    ):
        result = output_directory / "extension.mp4"
        result.write_bytes(b"video")
        return result.resolve()

    monkeypatch.setattr("app.services.task_manager.download_extension_stream", fake_download)
    manager = TaskManager(tmp_path)
    first = manager.create_extension_download(
        "https://media.example.com/master.m3u8",
        title="Эфир",
        stream_kind="hls",
        request_headers={},
        client_request_id="same-browser-job",
    )
    second = manager.create_extension_download(
        "https://media.example.com/master.m3u8",
        title="Эфир",
        stream_kind="hls",
        request_headers={},
        client_request_id="same-browser-job",
    )

    assert second is first
    assert len(manager._tasks) == 1
    wait_for_terminal(manager, first.task_id)


def test_task_manager_completes_transcription(monkeypatch, tmp_path: Path) -> None:
    def fake_transcribe(url, output_directory, progress_callback, cancel_event, **options):
        assert options["model_name"] == "small"
        assert options["formats"] == ("md", "srt")
        assert options["diarize_speakers"] is True
        assert options["speaker_count"] == 2
        progress_callback("transcribing", 70, "Распознаём речь")
        markdown = output_directory / "Видео - транскрибация.md"
        subtitles = output_directory / "Видео - транскрибация.srt"
        markdown.write_text("# Видео", encoding="utf-8")
        subtitles.write_text("1", encoding="utf-8")
        return [markdown.resolve(), subtitles.resolve()]

    monkeypatch.setattr("app.services.task_manager.transcribe_video", fake_transcribe)
    manager = TaskManager(tmp_path)
    task = manager.create_transcription(
        "https://youtu.be/abcdefghijk",
        model_name="small",
        language="auto",
        formats=("md", "srt"),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        diarize_speakers=True,
        speaker_count=2,
    )
    wait_for_terminal(manager, task.task_id)

    completed = manager.get(task.task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.progress == 100
    assert [path.suffix for path in completed.files] == [".md", ".srt"]


def test_task_manager_completes_audio_download(monkeypatch, tmp_path: Path) -> None:
    def fake_audio(url, output_format, bitrate_kbps, output_directory, progress_callback, cancel_event):
        assert output_format == "mp3"
        assert bitrate_kbps == 192
        progress_callback("downloading", 60, "Скачиваем аудио")
        result = output_directory / "audio.mp3"
        result.write_bytes(b"audio")
        return result.resolve()

    monkeypatch.setattr("app.services.task_manager.download_audio", fake_audio)
    manager = TaskManager(tmp_path)
    task = manager.create_audio_download(
        "https://vimeo.com/123456789",
        output_format="mp3",
        bitrate_kbps=192,
    )
    wait_for_terminal(manager, task.task_id)

    completed = manager.get(task.task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.files[0].suffix == ".mp3"


def test_progress_resets_when_stage_changes(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    task = TaskRecord(task_id="progress-task")
    manager._tasks[task.task_id] = task

    manager._update(
        task.task_id,
        "downloading",
        86,
        "Скачиваем",
        {"downloaded_bytes": 860, "total_bytes": 1000},
    )
    manager._update(task.task_id, "processing", None, "Обрабатываем")

    updated = manager.get(task.task_id)
    assert updated is not None
    assert updated.stage == "processing"
    assert updated.progress is None
    assert updated.downloaded_bytes is None
    assert updated.total_bytes is None


def test_task_manager_transcribes_local_file_and_removes_temporary_copy(monkeypatch, tmp_path: Path) -> None:
    upload_directory = tmp_path / "upload"
    upload_directory.mkdir()
    source = upload_directory / "source.mp4"
    source.write_bytes(b"local media")
    output_directory = tmp_path / "output"

    def fake_transcribe(source_path, original_name, output, progress_callback, cancel_event, **options):
        assert source_path == source.resolve()
        assert original_name == "lesson.mp4"
        assert options["model_name"] == "large-v3"
        assert options["diarize_speakers"] is True
        assert options["speaker_count"] == 3
        progress_callback(
            "transcribing",
            50,
            "Распознаём",
            {"processed_seconds": 30, "total_seconds": 60},
        )
        output.mkdir(parents=True, exist_ok=True)
        result = output / "lesson - транскрибация.md"
        result.write_text("# lesson", encoding="utf-8")
        return [result.resolve()]

    monkeypatch.setattr("app.services.task_manager.transcribe_local_file", fake_transcribe)
    manager = TaskManager(output_directory)
    task = manager.create_file_transcription(
        source,
        "lesson.mp4",
        model_name="large-v3",
        language="ru",
        formats=("md",),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        diarize_speakers=True,
        speaker_count=3,
    )
    wait_for_terminal(manager, task.task_id)

    completed = manager.get(task.task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.files[0].suffix == ".md"
    assert not upload_directory.exists()


def test_task_manager_passes_time_range_to_video_downloader(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_download(
        url,
        height,
        output_directory,
        progress_callback,
        cancel_event,
        *,
        start_seconds,
        end_seconds,
    ):
        captured.update(start=start_seconds, end=end_seconds)
        result = output_directory / "fragment.mp4"
        result.write_bytes(b"video")
        return result.resolve()

    monkeypatch.setattr("app.services.task_manager.download_video", fake_download)
    manager = TaskManager(tmp_path)
    task = manager.create_video_download(
        "https://youtu.be/abcdefghijk",
        720,
        start_seconds=30,
        end_seconds=95,
    )
    wait_for_terminal(manager, task.task_id)

    assert captured == {"start": 30, "end": 95}
    assert manager.get(task.task_id).status == "completed"


def test_queue_task_processes_items_sequentially_and_keeps_partial_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = []

    def fake_transcribe(url, output_directory, progress_callback, cancel_event, **options):
        calls.append(url)
        progress_callback("transcribing", 50, "Распознаём")
        if url.endswith("first"):
            raise VideoAnalysisError("Видео удалено")
        result = output_directory / "second - транскрибация.md"
        result.write_text("# Второе", encoding="utf-8")
        return [result.resolve()]

    monkeypatch.setattr("app.services.task_manager.transcribe_video", fake_transcribe)
    manager = TaskManager(tmp_path)
    task = manager.create_queue_task(
        (
            {"url": "https://example.com/first", "title": "Первое"},
            {"url": "https://example.com/second", "title": "Второе"},
        ),
        action="transcription",
        height=1080,
        output_format="mp3",
        bitrate_kbps=192,
        engine="hybrid",
        model_name="large-v3",
        language="ru",
        formats=("md",),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        text_source="auto",
        custom_terms=("Kubernetes",),
    )
    wait_for_terminal(manager, task.task_id)

    completed = manager.get(task.task_id)
    assert calls == ["https://example.com/first", "https://example.com/second"]
    assert completed is not None
    assert completed.status == "completed"
    assert completed.progress == 100
    assert [path.name for path in completed.files] == ["second - транскрибация.md"]
    assert completed.item_errors == [{"title": "Первое", "message": "Видео удалено"}]
    assert "1 из 2" in completed.message


def test_queue_task_fails_when_every_item_fails(monkeypatch, tmp_path: Path) -> None:
    def fake_audio(*_args, **_kwargs):
        raise VideoAnalysisError("Аудио недоступно")

    monkeypatch.setattr("app.services.task_manager.download_audio", fake_audio)
    manager = TaskManager(tmp_path)
    task = manager.create_queue_task(
        ({"url": "https://example.com/audio", "title": "Аудио"},),
        action="audio",
        height=1080,
        output_format="mp3",
        bitrate_kbps=192,
        engine="hybrid",
        model_name="large-v3",
        language="ru",
        formats=("md",),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        text_source="auto",
        custom_terms=(),
    )
    wait_for_terminal(manager, task.task_id)

    failed = manager.get(task.task_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.files == []
    assert failed.item_errors == [{"title": "Аудио", "message": "Аудио недоступно"}]


def test_cancelling_queue_removes_files_created_by_previous_items(monkeypatch, tmp_path: Path) -> None:
    call_count = 0

    def fake_video(url, height, output_directory, progress_callback, cancel_event):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            result = output_directory / "first.mp4"
            result.write_bytes(b"video")
            return result.resolve()
        cancel_event.set()
        raise DownloadCancelled("Отменено")

    monkeypatch.setattr("app.services.task_manager.download_video", fake_video)
    manager = TaskManager(tmp_path)
    task = manager.create_queue_task(
        (
            {"url": "https://example.com/first", "title": "Первое"},
            {"url": "https://example.com/second", "title": "Второе"},
        ),
        action="video",
        height=720,
        output_format="mp3",
        bitrate_kbps=192,
        engine="hybrid",
        model_name="large-v3",
        language="ru",
        formats=("md",),
        include_timestamps=True,
        paragraphize=True,
        remove_short_fragments=True,
        text_source="auto",
        custom_terms=(),
    )
    wait_for_terminal(manager, task.task_id)

    cancelled = manager.get(task.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.files == []
    assert not (tmp_path / "first.mp4").exists()


def test_queue_task_rejects_more_than_fifty_items(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    items = tuple(
        {"url": f"https://example.com/{index}", "title": str(index)}
        for index in range(51)
    )

    with pytest.raises(ValueError, match="от 1 до 50"):
        manager.create_queue_task(
            items,
            action="video",
            height=720,
            output_format="mp3",
            bitrate_kbps=192,
            engine="hybrid",
            model_name="large-v3",
            language="ru",
            formats=("md",),
            include_timestamps=True,
            paragraphize=True,
            remove_short_fragments=True,
            text_source="auto",
            custom_terms=(),
        )
