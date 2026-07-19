from __future__ import annotations

import errno
import gc
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlparse

import yt_dlp

from app.config import YTDLP_CONCURRENT_FRAGMENTS
from app.services.downloader import (
    DownloadCancelled,
    FFMPEG_DIRECTORY,
    ProgressCallback,
    VideoAnalysisError,
    YTDLP_RUNTIME_OPTIONS,
    _cleanup_directory_when_released,
)
from app.services.file_manager import sanitize_filename


ALLOWED_HEADER_NAMES = {
    "authorization": "Authorization",
    "cookie": "Cookie",
    "origin": "Origin",
    "referer": "Referer",
    "user-agent": "User-Agent",
}
MAX_HEADER_VALUE_LENGTH = 16_384
MAX_HEADERS_TOTAL_LENGTH = 32_768
_EXTENSION_PUBLISH_LOCK = Lock()
logger = logging.getLogger(__name__)


FileIdentity = tuple[int, int]


def _file_identity(path: Path) -> FileIdentity:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _reserve_unique_destination(directory: Path, stem: str, suffix: str) -> tuple[Path, FileIdentity]:
    """Резервирует свободное имя, не перезаписывая даже файл, созданный в момент проверки."""
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    counter = 1
    while True:
        numbered_stem = stem if counter == 1 else f"{stem} ({counter})"
        candidate = directory / f"{numbered_stem}{safe_suffix}"
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            counter += 1
            continue
        return candidate.resolve(), _file_identity(candidate)


def _unlink_owned_file(
    path: Path,
    output_directory: Path,
    identity: FileIdentity,
    *,
    retry_until_removed: bool,
) -> bool:
    """Удаляет только созданный этой задачей файл и не трогает заменивший его чужой файл."""
    safe_path = path.resolve()
    if not safe_path.is_relative_to(output_directory.resolve()):
        logger.error("Отказ от удаления файла вне downloads: %s", safe_path)
        return False

    attempt = 0
    while True:
        try:
            if _file_identity(safe_path) != identity:
                logger.warning("Файл отменённой загрузки уже заменён другим: %s", safe_path)
                return True
            safe_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            attempt += 1
            if not retry_until_removed and attempt >= 30:
                return False
            time.sleep(0.1 if attempt < 30 else 0.5)


def normalize_extension_stream_url(url: str) -> str:
    """Поднимает дочерний поток Kinescope к плейлисту с видео и звуком."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path
    if (host == "kinescope.io" or host.endswith(".kinescope.io")) and path.lower().endswith(
        "/media.m3u8"
    ):
        master_path = f"{path[:-len('media.m3u8')]}master.m3u8"
        return parsed._replace(path=master_path).geturl()
    return url.strip()


def validate_public_media_url(url: str) -> str:
    """Разрешает только внешние HTTP(S)-адреса и блокирует запросы в локальную сеть."""
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Расширение передало некорректный адрес видеопотока")
    if parsed.username or parsed.password:
        raise ValueError("Адрес видеопотока не должен содержать логин или пароль")

    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Локальные сетевые адреса нельзя использовать как видеопоток")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError("Не удалось проверить адрес видеопотока") from exc

    if not addresses:
        raise ValueError("Не удалось найти сервер видеопотока")
    for address in addresses:
        raw_ip = str(address[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise ValueError("Сервер видеопотока вернул некорректный сетевой адрес") from exc
        if not ip.is_global:
            raise ValueError("Видеопоток ведёт во внутреннюю сеть и заблокирован для безопасности")
    return value


def sanitize_extension_headers(headers: dict[str, str], page_url: str | None = None) -> dict[str, str]:
    """Оставляет только заголовки, необходимые для повторения медиазапроса браузера."""
    result: dict[str, str] = {}
    total_length = 0
    for raw_name, raw_value in headers.items():
        safe_name = ALLOWED_HEADER_NAMES.get(str(raw_name).strip().lower())
        value = str(raw_value)
        if not safe_name or not value or "\r" in value or "\n" in value:
            continue
        if len(value) > MAX_HEADER_VALUE_LENGTH:
            raise ValueError("Данные доступа к видеопотоку слишком большие")
        total_length += len(value)
        if total_length > MAX_HEADERS_TOTAL_LENGTH:
            raise ValueError("Данные доступа к видеопотоку слишком большие")
        result[safe_name] = value

    if page_url and "Referer" not in result:
        result["Referer"] = page_url
    return result


def _friendly_stream_error(message: str) -> str:
    lowered = message.lower()
    if "drm" in lowered or "encrypted" in lowered:
        return "Поток защищён DRM и не может быть скачан"
    if "403" in lowered or "forbidden" in lowered or "401" in lowered or "unauthorized" in lowered:
        return "Сервер отклонил доступ. Обновите страницу с видео и попробуйте ещё раз"
    if "404" in lowered or "not found" in lowered:
        return "Видеопоток уже недоступен. Обновите страницу и запустите видео заново"
    return "Не удалось скачать поток. Запустите видео на странице и попробуйте ещё раз"


def probe_media_tracks(path: Path) -> tuple[bool, bool]:
    """Проверяет, что итоговый контейнер действительно содержит видео и звук."""
    probe_name = "ffprobe.exe" if FFMPEG_DIRECTORY and (FFMPEG_DIRECTORY / "ffprobe.exe").is_file() else "ffprobe"
    probe_path = (FFMPEG_DIRECTORY / probe_name) if FFMPEG_DIRECTORY else shutil.which(probe_name)
    if not probe_path:
        raise VideoAnalysisError("Не найден FFmpeg для проверки видео и звука")

    try:
        result = subprocess.run(
            [
                str(probe_path),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise VideoAnalysisError("Не удалось проверить дорожки готового видео") from exc

    track_types = {stream.get("codec_type") for stream in payload.get("streams", [])}
    return "video" in track_types, "audio" in track_types


def download_extension_stream(
    stream_url: str,
    title: str,
    stream_kind: str,
    request_headers: dict[str, str],
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> Path:
    """Скачивает поток, который пользователь выбрал в локальном расширении Chrome."""
    safe_url = validate_public_media_url(normalize_extension_stream_url(stream_url))
    safe_headers = sanitize_extension_headers(request_headers)
    output_directory.mkdir(parents=True, exist_ok=True)

    safe_title = sanitize_filename(title, fallback="video")
    work_directory = Path(tempfile.mkdtemp(prefix="extension-video-"))
    work_stem = work_directory / safe_title
    output_template = f"{work_stem}.%(ext)s"
    destination_path: Path | None = None
    destination_identity: FileIdentity | None = None
    destination_owned_by_task = False
    destination_published_by_task = False

    def progress_hook(data: dict) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        if data.get("status") == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            speed = float(data.get("speed") or 0) or None
            eta = float(data.get("eta") or 0) or None
            progress = min(100, downloaded / total * 100) if total > 0 else None
            progress_callback(
                "downloading",
                progress,
                "Скачиваем видеопоток",
                {
                    "downloaded_bytes": downloaded,
                    "total_bytes": total or None,
                    "speed_bytes_per_second": speed,
                    "eta_seconds": eta,
                },
            )
        elif data.get("status") == "finished":
            progress_callback("processing", None, "Подготавливаем видеофайл", None)

    def postprocessor_hook(data: dict) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        if data.get("status") == "started":
            progress_callback("processing", None, "Объединяем видео и аудио", None)
        elif data.get("status") == "finished":
            progress_callback("processing", 100, "Видео обработано", None)

    def cleanup_work_directory() -> None:
        attempt = 0
        while True:
            try:
                shutil.rmtree(work_directory)
                return
            except FileNotFoundError:
                return
            except OSError:
                attempt += 1
                if not cancel_event.is_set() and attempt >= 5:
                    Thread(
                        target=_cleanup_directory_when_released,
                        args=(work_directory,),
                        daemon=True,
                        name="extension-download-cleanup",
                    ).start()
                    return
                time.sleep(0.2 if attempt < 150 else 0.5)

    def cleanup_owned_destination(*, retry_until_removed: bool) -> None:
        if (
            not destination_owned_by_task
            or destination_path is None
            or destination_identity is None
        ):
            return
        removed = _unlink_owned_file(
            destination_path,
            output_directory,
            destination_identity,
            retry_until_removed=retry_until_removed,
        )
        if not removed:
            Thread(
                target=_unlink_owned_file,
                args=(destination_path, output_directory, destination_identity),
                kwargs={"retry_until_removed": True},
                daemon=True,
                name="extension-output-cleanup",
            ).start()

    options = {
        **YTDLP_RUNTIME_OPTIONS,
        "format": "bestvideo+bestaudio/best[vcodec!=none][acodec!=none]",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "http_headers": safe_headers,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "overwrites": False,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": YTDLP_CONCURRENT_FRAGMENTS,
        "file_access_retries": 10,
        "socket_timeout": 30,
        "allow_unplayable_formats": False,
        "live_from_start": False,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }

    def perform_download() -> Path:
        nonlocal destination_identity
        nonlocal destination_owned_by_task
        nonlocal destination_path
        nonlocal destination_published_by_task
        progress_message = "Подключаемся к прямому эфиру" if stream_kind == "hls" else "Подключаемся к видеопотоку"
        progress_callback("preparing", None, progress_message, None)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(safe_url, download=True)
        except DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError as exc:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена") from exc
            raise VideoAnalysisError(_friendly_stream_error(str(exc))) from exc
        except Exception as exc:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена") from exc
            raise VideoAnalysisError("Не удалось скачать или обработать видеопоток") from exc

        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")

        candidates = sorted(
            (
                path
                for path in work_directory.glob(f"{work_stem.name}.*")
                if path.is_file() and not path.name.endswith((".part", ".ytdl"))
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise VideoAnalysisError("Загрузка завершилась, но итоговый файл не найден")

        final_path: Path | None = None
        for candidate in candidates:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена")
            try:
                has_video, has_audio = probe_media_tracks(candidate)
            except VideoAnalysisError:
                continue
            if has_video and has_audio:
                final_path = candidate
                break
        if final_path is None:
            raise VideoAnalysisError(
                "Источник отдал только одну дорожку. Выберите в расширении вариант «Видео + звук»"
            )

        progress_callback("saving", None, "Сохраняем готовый файл", None)
        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        with _EXTENSION_PUBLISH_LOCK:
            destination_path, destination_identity = _reserve_unique_destination(
                output_directory,
                safe_title,
                final_path.suffix or ".mp4",
            )
            destination_owned_by_task = True
            for attempt in range(10):
                try:
                    try:
                        os.replace(final_path, destination_path)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        shutil.copy2(final_path, destination_path)
                        final_path.unlink()
                    destination_identity = _file_identity(destination_path)
                    destination_published_by_task = True
                    break
                except PermissionError as exc:
                    if attempt == 9:
                        raise VideoAnalysisError("Не удалось сохранить файл: папка занята другой программой") from exc
                    time.sleep(0.25)

        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        progress_callback("completed", 100, "Видео сохранено", None)
        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        return destination_path.resolve()

    cancelled = False
    succeeded = False
    result_path: Path | None = None
    try:
        try:
            result_path = perform_download()
            succeeded = True
        except DownloadCancelled:
            # Не протаскиваем traceback yt-dlp в finally. Иначе его HLS-контекст
            # продолжает держать открытым основной .part-файл на Windows.
            cancelled = True
    finally:
        if cancelled:
            gc.collect()
        try:
            should_remove_destination = cancel_event.is_set() or (
                destination_owned_by_task
                and (not destination_published_by_task or not succeeded)
            )
            if should_remove_destination:
                cleanup_owned_destination(retry_until_removed=cancel_event.is_set())
        finally:
            cleanup_work_directory()

    if cancelled:
        raise DownloadCancelled("Загрузка отменена")
    if cancel_event.is_set():
        # Отмена могла прийти уже во время удаления рабочей папки, после
        # предыдущей проверки destination. В этом случае итоговый файл всё
        # ещё принадлежит этой задаче и должен быть удалён до статуса cancelled.
        cleanup_owned_destination(retry_until_removed=True)
        raise DownloadCancelled("Загрузка отменена")
    if result_path is None:
        raise VideoAnalysisError("Загрузка завершилась без итогового файла")
    return result_path
