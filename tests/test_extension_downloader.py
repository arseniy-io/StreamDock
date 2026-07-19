import os
import shutil
import socket
from threading import Event

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
    assert captured["http_headers"] == {"Authorization": "Bearer test"}
    assert any(stage == "downloading" and progress == 100 for stage, progress, _ in updates)


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
