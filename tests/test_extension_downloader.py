import os
import shutil
import socket
from threading import Event, Thread

import pytest

from app.services.extension_downloader import (
    download_extension_stream,
    normalize_extension_stream_url,
    sanitize_extension_headers,
    validate_public_media_url,
)
from app.services.downloader import DownloadCancelled, VideoAnalysisError


def _resolved_address(ip: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


def test_validate_public_media_url_accepts_public_server(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.extension_downloader.socket.getaddrinfo",
        lambda *args, **kwargs: _resolved_address("93.184.216.34"),
    )

    url = "https://media.example.com/live/master.m3u8?token=test"
    assert validate_public_media_url(url) == url


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.1.1"])
def test_validate_public_media_url_rejects_private_network(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(
        "app.services.extension_downloader.socket.getaddrinfo",
        lambda *args, **kwargs: _resolved_address(ip),
    )

    with pytest.raises(ValueError, match="внутреннюю сеть"):
        validate_public_media_url("https://media.example.com/live.m3u8")


def test_sanitize_extension_headers_uses_allowlist_and_referer() -> None:
    result = sanitize_extension_headers(
        {
            "authorization": "Bearer test",
            "Cookie": "session=test",
            "X-Forwarded-For": "127.0.0.1",
            "Origin": "https://training.example.com",
        },
        "https://training.example.com/lesson/1",
    )

    assert result == {
        "Authorization": "Bearer test",
        "Cookie": "session=test",
        "Origin": "https://training.example.com",
        "Referer": "https://training.example.com/lesson/1",
    }


def test_sanitize_extension_headers_rejects_line_breaks() -> None:
    assert sanitize_extension_headers({"Referer": "https://example.com\r\nX-Test: bad"}) == {}


def test_normalize_extension_stream_url_uses_kinescope_master_playlist() -> None:
    child_url = (
        "https://kinescope.io/b89e8cab-2c45-40a7-9677-a0fc1c8625f3/"
        "media.m3u8?type=video&quality=1080"
    )

    assert normalize_extension_stream_url(child_url) == (
        "https://kinescope.io/b89e8cab-2c45-40a7-9677-a0fc1c8625f3/"
        "master.m3u8?type=video&quality=1080"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://kinescope.io/video-id/master.m3u8?token=keep",
        "https://media.example.com/video-id/media.m3u8?token=keep",
        "https://notkinescope.io/video-id/media.m3u8?token=keep",
        "https://kinescope.io/video-id/media.m3u8/segment.ts",
        "https://kinescope.io/video-id/some-media.m3u8",
    ],
)
def test_normalize_extension_stream_url_preserves_other_streams(url: str) -> None:
    assert normalize_extension_stream_url(url) == url


def test_download_extension_stream_normalizes_kinescope_child_url(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            captured["url"] = url
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"video-with-audio")

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)

    download_extension_stream(
        "https://cdn.kinescope.io/video-id/media.m3u8?type=video&token=secret-test",
        "Тестовый эфир",
        "hls",
        {},
        tmp_path,
        lambda *args: None,
        Event(),
    )

    assert captured["url"] == (
        "https://cdn.kinescope.io/video-id/master.m3u8?type=video&token=secret-test"
    )


def test_download_extension_stream_passes_safe_headers_and_saves_file(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    updates: list[tuple[str, float | None, str]] = []

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = captured["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"test-video")
            captured["progress_hooks"][0](
                {"status": "downloading", "downloaded_bytes": 10, "total_bytes": 10}
            )
            return {"url": url, "download": download}

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)

    result = download_extension_stream(
        "https://media.example.com/live.m3u8",
        "Тестовый эфир",
        "hls",
        {"Authorization": "Bearer test", "X-Unsafe": "ignored"},
        tmp_path,
        lambda stage, progress, message, details=None: updates.append((stage, progress, message)),
        Event(),
    )

    assert result.is_file()
    assert result.read_bytes() == b"test-video"
    assert result.name == "Тестовый эфир.mp4"
    assert captured["format"] == "bestvideo+bestaudio/best"
    assert captured["http_headers"] == {"Authorization": "Bearer test"}
    assert not any(stage == "downloading" and progress == 100 for stage, progress, _ in updates)
    assert updates[-1] == ("completed", 100, "Видео сохранено")


def test_download_extension_stream_reports_one_progress_for_video_and_audio(
    monkeypatch,
    tmp_path,
) -> None:
    updates = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"test-video")

            hook = self.options["progress_hooks"][0]
            video_info = {"format_id": "video", "vcodec": "h264", "acodec": "none"}
            audio_info = {"format_id": "audio", "vcodec": "none", "acodec": "aac"}
            for status, downloaded_bytes in (("downloading", 50), ("finished", 100)):
                hook(
                    {
                        "status": status,
                        "filename": f"{output_path}.fvideo.mp4",
                        "downloaded_bytes": downloaded_bytes,
                        "total_bytes": 100,
                        "speed": 10,
                        "info_dict": video_info,
                    }
                )
            for status, downloaded_bytes in (("downloading", 1), ("finished", 20)):
                hook(
                    {
                        "status": status,
                        "filename": f"{output_path}.faudio.m4a",
                        "downloaded_bytes": downloaded_bytes,
                        "total_bytes": 20,
                        "speed": 2,
                        "info_dict": audio_info,
                    }
                )

            postprocessor_hook = self.options["postprocessor_hooks"][0]
            postprocessor_hook({"status": "started"})
            postprocessor_hook({"status": "finished"})
            return {"url": url, "download": download}

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)

    result = download_extension_stream(
        "https://media.example.com/master.m3u8",
        "Тестовый эфир",
        "hls",
        {},
        tmp_path,
        lambda stage, progress, message, details=None: updates.append(
            (stage, progress, message, details)
        ),
        Event(),
    )

    downloading = [update for update in updates if update[0] == "downloading"]
    progresses = [update[1] for update in downloading]
    downloaded_bytes = [update[3]["downloaded_bytes"] for update in downloading]
    assert progresses == sorted(progresses)
    assert all(progress is not None and progress < 100 for progress in progresses)
    assert downloaded_bytes == sorted(downloaded_bytes)
    assert "Скачиваем видеодорожку" in {update[2] for update in downloading}
    assert "Скачиваем аудиодорожку" in {update[2] for update in downloading}
    assert max(index for index, update in enumerate(updates) if update[0] == "downloading") < min(
        index for index, update in enumerate(updates) if update[0] == "processing"
    )
    processing = [update for update in updates if update[0] == "processing"]
    assert processing
    assert all(update[1] == progresses[-1] for update in processing)
    assert all(update[1] is not None and update[1] < 100 for update in processing)
    assert sum(update[0] == "completed" and update[1] == 100 for update in updates) == 1
    assert result.name == "Тестовый эфир.mp4"


def test_download_extension_stream_serializes_parallel_progress_callbacks(
    monkeypatch,
    tmp_path,
) -> None:
    updates = []
    low_callback_started = Event()
    high_callback_finished = Event()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"test-video")

            hook = self.options["progress_hooks"][0]
            info = {"format_id": "combined", "vcodec": "h264", "acodec": "aac"}
            low = Thread(
                target=lambda: hook(
                    {
                        "status": "downloading",
                        "filename": output_path,
                        "downloaded_bytes": 10,
                        "total_bytes": 100,
                        "info_dict": info,
                    }
                )
            )
            high = Thread(
                target=lambda: hook(
                    {
                        "status": "downloading",
                        "filename": output_path,
                        "downloaded_bytes": 80,
                        "total_bytes": 100,
                        "info_dict": info,
                    }
                )
            )
            low.start()
            assert low_callback_started.wait(timeout=1)
            high.start()
            low.join(timeout=1)
            high.join(timeout=1)
            assert not low.is_alive()
            assert not high.is_alive()
            return {"url": url, "download": download}

    def record_update(stage, progress, message, details=None):
        if stage == "downloading" and details["downloaded_bytes"] == 10:
            low_callback_started.set()
            high_callback_finished.wait(timeout=0.1)
        elif stage == "downloading" and details["downloaded_bytes"] == 80:
            updates.append((stage, progress, message, details))
            high_callback_finished.set()
            return
        updates.append((stage, progress, message, details))

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)

    download_extension_stream(
        "https://media.example.com/master.mpd",
        "Параллельные дорожки",
        "dash",
        {},
        tmp_path,
        record_update,
        Event(),
    )

    downloading = [update for update in updates if update[0] == "downloading"]
    assert [update[1] for update in downloading] == sorted(update[1] for update in downloading)
    assert [update[3]["downloaded_bytes"] for update in downloading] == [10, 80]


def test_download_extension_stream_rejects_single_track(monkeypatch, tmp_path) -> None:
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"video-only")

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, False))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)

    with pytest.raises(VideoAnalysisError, match=r"Видео \+ звук"):
        download_extension_stream(
            "https://media.example.com/2160p.mp4",
            "Тестовый эфир",
            "video",
            {},
            tmp_path,
            lambda *args: None,
            Event(),
        )


def test_download_extension_stream_removes_destination_when_cancelled_after_move(
    monkeypatch, tmp_path
) -> None:
    cancel_event = Event()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"video-with-audio")

    real_replace = os.replace

    def cancel_after_move(source, destination):
        result = real_replace(source, destination)
        cancel_event.set()
        return result

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("app.services.extension_downloader.os.replace", cancel_after_move)

    with pytest.raises(DownloadCancelled):
        download_extension_stream(
            "https://media.example.com/master.m3u8",
            "Отменённый эфир",
            "hls",
            {},
            tmp_path,
            lambda *args: None,
            cancel_event,
        )

    assert list(tmp_path.iterdir()) == []


def test_download_extension_stream_preserves_existing_file_with_actual_suffix(
    monkeypatch, tmp_path
) -> None:
    existing = tmp_path / "Lesson.v1.webm"
    existing.write_bytes(b"user-file")

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "webm")
            with open(output_path, "wb") as destination:
                destination.write(b"new-video")

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)

    result = download_extension_stream(
        "https://media.example.com/master.m3u8",
        "Lesson.v1",
        "hls",
        {},
        tmp_path,
        lambda *args: None,
        Event(),
    )

    assert existing.read_bytes() == b"user-file"
    assert result.name == "Lesson.v1 (2).webm"
    assert result.read_bytes() == b"new-video"


def test_download_extension_stream_does_not_delete_replacement_after_publish_failure(
    monkeypatch, tmp_path
) -> None:
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"new-video")

    replacement_path: list = []

    def replace_with_external_file(source, destination):
        path = type(tmp_path)(destination)
        if not replacement_path:
            path.unlink()
            path.write_bytes(b"external-file")
            replacement_path.append(path)
        raise PermissionError("locked")

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("app.services.extension_downloader.os.replace", replace_with_external_file)
    monkeypatch.setattr("app.services.extension_downloader.time.sleep", lambda _seconds: None)

    with pytest.raises(VideoAnalysisError, match="Не удалось сохранить файл"):
        download_extension_stream(
            "https://media.example.com/master.m3u8",
            "Publish failure",
            "hls",
            {},
            tmp_path,
            lambda *args: None,
            Event(),
        )

    assert replacement_path[0].read_bytes() == b"external-file"
    assert list(tmp_path.iterdir()) == replacement_path


def test_cancel_waits_until_locked_temporary_directory_is_removed(monkeypatch, tmp_path) -> None:
    cancel_event = Event()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"video-with-audio")

    real_replace = os.replace
    real_rmtree = shutil.rmtree
    cleanup_attempts = 0

    def cancel_after_move(source, destination):
        result = real_replace(source, destination)
        cancel_event.set()
        return result

    def temporarily_locked_rmtree(path):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts < 4:
            raise PermissionError("locked")
        return real_rmtree(path)

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("app.services.extension_downloader.os.replace", cancel_after_move)
    monkeypatch.setattr("app.services.extension_downloader.shutil.rmtree", temporarily_locked_rmtree)
    monkeypatch.setattr("app.services.extension_downloader.time.sleep", lambda _seconds: None)

    with pytest.raises(DownloadCancelled):
        download_extension_stream(
            "https://media.example.com/master.m3u8",
            "Locked cleanup",
            "hls",
            {},
            tmp_path,
            lambda *args: None,
            cancel_event,
        )

    assert cleanup_attempts == 4
    assert list(tmp_path.iterdir()) == []


def test_download_extension_stream_removes_destination_when_cancel_arrives_during_temp_cleanup(
    monkeypatch, tmp_path
) -> None:
    cancel_event = Event()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            output_path = self.options["outtmpl"].replace("%(ext)s", "mp4")
            with open(output_path, "wb") as destination:
                destination.write(b"video-with-audio")

    real_rmtree = shutil.rmtree

    def cancel_during_temp_cleanup(path):
        cancel_event.set()
        return real_rmtree(path)

    monkeypatch.setattr("app.services.extension_downloader.validate_public_media_url", lambda url: url)
    monkeypatch.setattr("app.services.extension_downloader.probe_media_tracks", lambda path: (True, True))
    monkeypatch.setattr("app.services.extension_downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("app.services.extension_downloader.shutil.rmtree", cancel_during_temp_cleanup)

    with pytest.raises(DownloadCancelled):
        download_extension_stream(
            "https://media.example.com/master.m3u8",
            "Late cancel",
            "hls",
            {},
            tmp_path,
            lambda *args: None,
            cancel_event,
        )

    assert list(tmp_path.iterdir()) == []
