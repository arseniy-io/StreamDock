from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import shutil
import tempfile
import time
from threading import Event, Thread
from typing import Callable
from urllib.parse import urlparse

import yt_dlp

from app.config import YTDLP_CONCURRENT_FRAGMENTS
from app.models.schemas import AudioOption, SubtitleOption, VideoInfoResponse, VideoQuality
from app.services.file_manager import sanitize_filename, unique_output_stem


SUPPORTED_MEDIA_DOMAINS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "rutube.ru": "Rutube",
    "vk.com": "VK Video",
    "vkvideo.ru": "VK Video",
    "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion",
    "dai.ly": "Dailymotion",
    "tiktok.com": "TikTok",
    "twitch.tv": "Twitch",
    "soundcloud.com": "SoundCloud",
}

def find_ffmpeg_directory() -> Path | None:
    """Находит установленный FFmpeg без изменения системного PATH."""
    executable = shutil.which("ffmpeg")
    if executable:
        return Path(executable).resolve().parent

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    packages_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    candidates = sorted(
        packages_root.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0].parent.resolve() if candidates else None


FFMPEG_DIRECTORY = find_ffmpeg_directory()
YTDLP_RUNTIME_OPTIONS = {
    "js_runtimes": {"node": {}},
    **({"ffmpeg_location": str(FFMPEG_DIRECTORY)} if FFMPEG_DIRECTORY else {}),
}


class VideoAnalysisError(RuntimeError):
    """Понятная пользователю ошибка анализа ссылки."""


class DownloadCancelled(RuntimeError):
    """Пользователь отменил загрузку."""


def validate_video_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Введите корректную ссылку с http:// или https://")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_MEDIA_DOMAINS):
        raise ValueError(
            "Источник пока не поддерживается. Используйте YouTube, Rutube, VK Video, Vimeo, "
            "Dailymotion, TikTok, Twitch или SoundCloud"
        )
    return url.strip()


def source_name_for_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for domain, name in SUPPORTED_MEDIA_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    return "Медиа"


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "Неизвестно"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _format_size(item: dict) -> int | None:
    size = item.get("filesize") or item.get("filesize_approx")
    return int(size) if isinstance(size, (int, float)) and size > 0 else None


def select_video_qualities(formats: Iterable[dict]) -> list[VideoQuality]:
    """Оставляет по одному понятному варианту на каждую высоту кадра."""
    by_height: dict[int, dict] = {}
    for item in formats:
        if item.get("vcodec") in {None, "none"}:
            continue
        height = item.get("height")
        if not isinstance(height, (int, float)) or height <= 0:
            continue
        height = int(height)

        current = by_height.get(height)
        candidate_score = (
            item.get("ext") == "mp4",
            item.get("acodec") not in {None, "none"},
            _format_size(item) is not None,
            float(item.get("tbr") or 0),
        )
        current_score = (
            current.get("ext") == "mp4",
            current.get("acodec") not in {None, "none"},
            _format_size(current) is not None,
            float(current.get("tbr") or 0),
        ) if current else None
        if current is None or candidate_score > current_score:
            by_height[height] = item

    return [
        VideoQuality(
            height=height,
            label=f"{height}p",
            container=str(item.get("ext") or "неизвестно").upper(),
            approximate_size=_format_size(item),
        )
        for height, item in sorted(by_height.items(), reverse=True)
    ]


def select_audio_options(formats: Iterable[dict]) -> list[AudioOption]:
    """Показывает лучшие исходные аудиопотоки по контейнеру."""
    by_container: dict[str, dict] = {}
    for item in formats:
        if item.get("acodec") in {None, "none"} or item.get("vcodec") not in {None, "none"}:
            continue
        container = str(item.get("ext") or "unknown").lower()
        current = by_container.get(container)
        if current is None or float(item.get("abr") or 0) > float(current.get("abr") or 0):
            by_container[container] = item

    return [
        AudioOption(
            container=container.upper(),
            codec=item.get("acodec"),
            bitrate_kbps=round(item["abr"]) if isinstance(item.get("abr"), (int, float)) else None,
            approximate_size=_format_size(item),
        )
        for container, item in sorted(by_container.items())
    ]


def select_subtitle_options(info: dict) -> list[SubtitleOption]:
    """Возвращает понятный список ручных и автоматических дорожек без дублей."""
    result: list[SubtitleOption] = []
    seen: set[tuple[str, bool]] = set()
    for automatic, key in ((False, "subtitles"), (True, "automatic_captions")):
        tracks = info.get(key)
        if not isinstance(tracks, dict):
            continue
        for raw_language, variants in tracks.items():
            language = str(raw_language or "").strip()
            if not language or len(language) > 32 or not isinstance(variants, list):
                continue
            if not any(
                isinstance(variant, dict) and str(variant.get("ext") or "").lower() in {"vtt", "srt"}
                for variant in variants
            ):
                continue
            identity = (language.casefold(), automatic)
            if identity in seen:
                continue
            seen.add(identity)
            label = next(
                (
                    str(variant.get("name")).strip()
                    for variant in variants
                    if isinstance(variant, dict) and str(variant.get("name") or "").strip()
                ),
                language,
            )
            result.append(SubtitleOption(language=language, name=label[:120], automatic=automatic))

    return sorted(
        result,
        key=lambda item: (
            not item.language.lower().startswith("ru"),
            item.automatic,
            item.language.casefold(),
        ),
    )[:40]


def validate_time_range(
    start_seconds: float | None,
    end_seconds: float | None,
    duration_seconds: float | int | None = None,
) -> tuple[float, float] | None:
    """Проверяет фрагмент и при известной длительности не выпускает его за границы."""
    if start_seconds is None and end_seconds is None:
        return None
    if start_seconds is None or end_seconds is None:
        raise ValueError("Укажите и начало, и конец фрагмента")
    start = float(start_seconds)
    end = float(end_seconds)
    if start < 0 or end - start < 0.5:
        raise ValueError("Выбран неверный диапазон фрагмента")
    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        duration = float(duration_seconds)
        if start >= duration:
            raise ValueError("Начало фрагмента находится после конца видео")
        end = min(end, duration)
        if end - start < 0.5:
            raise ValueError("Выбранный фрагмент слишком короткий")
    return start, end


def _apply_download_range(options: dict, selected_range: tuple[float, float] | None) -> None:
    if selected_range is None:
        return
    options["download_ranges"] = yt_dlp.utils.download_range_func(None, [selected_range])
    options["force_keyframes_at_cuts"] = True


def _friendly_yt_dlp_error(message: str) -> str:
    lowered = message.lower()
    if "private video" in lowered:
        return "Видео приватное и недоступно для скачивания"
    if "video unavailable" in lowered or "not available" in lowered:
        return "Видео недоступно, удалено или имеет региональное ограничение"
    if "sign in to confirm your age" in lowered or "age-restricted" in lowered:
        return "Видео имеет возрастное ограничение и недоступно без авторизации"
    return "Не удалось получить информацию о видео. Проверьте ссылку и подключение к интернету"


def analyze_video(url: str) -> VideoInfoResponse:
    safe_url = validate_video_url(url)
    options = {
        **YTDLP_RUNTIME_OPTIONS,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(safe_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise VideoAnalysisError(_friendly_yt_dlp_error(str(exc))) from exc
    except Exception as exc:
        raise VideoAnalysisError("Не удалось проанализировать видео") from exc

    if not isinstance(info, dict):
        raise VideoAnalysisError("Сервис не вернул информацию о видео")
    formats = info.get("formats") or []
    qualities = select_video_qualities(formats)
    audio_options = select_audio_options(formats)
    if not qualities and not audio_options:
        raise VideoAnalysisError("Для этой ссылки не найдено доступных медиаформатов")

    up_to_1080 = [quality.height for quality in qualities if quality.height <= 1080]
    default_quality = (
        max(up_to_1080)
        if up_to_1080
        else min((quality.height for quality in qualities), default=None)
    )
    duration = info.get("duration")

    return VideoInfoResponse(
        source_url=safe_url,
        source_name=source_name_for_url(safe_url),
        title=str(info.get("title") or "Без названия"),
        author=info.get("channel") or info.get("uploader"),
        thumbnail=info.get("thumbnail"),
        duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
        duration_text=format_duration(duration),
        video_qualities=qualities,
        audio_options=audio_options,
        default_quality=default_quality,
        subtitles=select_subtitle_options(info),
    )


ProgressDetails = dict[str, float | int | str | None]
ProgressCallback = Callable[[str, float | None, str, ProgressDetails | None], None]


def build_video_format_selector(height: int) -> str:
    """Сначала выбирает готовый MP4 нужной высоты, затем раздельные дорожки."""
    return (
        f"best[height={height}][ext=mp4][vcodec!=none][acodec!=none]/"
        f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height={height}]+bestaudio/"
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]"
    )


def _cleanup_directory_when_released(directory: Path) -> None:
    """Удаляет временную папку после освобождения файлов внутренними worker yt-dlp."""
    for _ in range(2400):
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)


def download_video(
    url: str,
    height: int,
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> Path:
    """Скачивает выбранное видео и возвращает итоговый MP4-файл."""
    safe_url = validate_video_url(url)
    output_directory.mkdir(parents=True, exist_ok=True)
    progress_callback("preparing", None, "Получаем информацию о видео", None)

    metadata_options = {
        **YTDLP_RUNTIME_OPTIONS,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(metadata_options) as ydl:
            metadata = ydl.extract_info(safe_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise VideoAnalysisError(_friendly_yt_dlp_error(str(exc))) from exc

    if cancel_event.is_set():
        raise DownloadCancelled("Загрузка отменена")
    if not isinstance(metadata, dict):
        raise VideoAnalysisError("Не удалось получить информацию о видео")

    try:
        selected_range = validate_time_range(start_seconds, end_seconds, metadata.get("duration"))
    except ValueError as exc:
        raise VideoAnalysisError(str(exc)) from exc

    title_suffix = ""
    if selected_range is not None:
        title_suffix = f" - фрагмент {format_duration(selected_range[0])}-{format_duration(selected_range[1])}"
    title = sanitize_filename(f"{metadata.get('title') or 'video'}{title_suffix}")
    destination_stem = unique_output_stem(output_directory, title)
    work_directory = Path(tempfile.mkdtemp(prefix="local-video-"))
    work_stem = work_directory / title
    output_template = f"{work_stem}.%(ext)s"
    selected_format = build_video_format_selector(height)

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
                "Скачиваем видеодорожки",
                {
                    "downloaded_bytes": downloaded,
                    "total_bytes": total or None,
                    "speed_bytes_per_second": speed,
                    "eta_seconds": eta,
                },
            )
        elif data.get("status") == "finished":
            progress_callback("processing", None, "Подготавливаем дорожки", None)

    def postprocessor_hook(data: dict) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        if data.get("status") == "started":
            progress_callback("processing", None, "Объединяем видео и аудио через FFmpeg", None)
        elif data.get("status") == "finished":
            progress_callback("processing", 100, "Дорожки обработаны", None)

    def cleanup_work_directory() -> None:
        cleanup_attempts = 5
        for attempt in range(cleanup_attempts):
            try:
                shutil.rmtree(work_directory)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt < cleanup_attempts - 1:
                    time.sleep(0.2)
        Thread(
            target=_cleanup_directory_when_released,
            args=(work_directory,),
            daemon=True,
            name="download-temp-cleanup",
        ).start()

    download_options = {
        **YTDLP_RUNTIME_OPTIONS,
        "format": selected_format,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
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
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }
    _apply_download_range(download_options, selected_range)

    progress_callback("downloading", 0, f"Начинаем загрузку {height}p", None)

    try:
        try:
            with yt_dlp.YoutubeDL(download_options) as ydl:
                ydl.extract_info(safe_url, download=True)
        except DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError as exc:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена") from exc
            raise VideoAnalysisError(_friendly_download_error(str(exc))) from exc
        except Exception as exc:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена") from exc
            raise VideoAnalysisError("Не удалось скачать или обработать видео") from exc

        final_path = work_stem.with_suffix(".mp4")
        if not final_path.is_file():
            candidates = sorted(
                work_directory.glob(f"{work_stem.name}.*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            candidates = [path for path in candidates if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
            if not candidates:
                raise VideoAnalysisError("Загрузка завершилась, но итоговый файл не найден")
            final_path = candidates[0]

        destination_path = destination_stem.with_suffix(final_path.suffix)
        progress_callback("saving", None, "Сохраняем готовый файл", None)
        for attempt in range(10):
            try:
                shutil.move(str(final_path), str(destination_path))
                break
            except PermissionError as exc:
                if attempt == 9:
                    raise VideoAnalysisError("Не удалось сохранить файл: папка занята другой программой") from exc
                time.sleep(0.25)

        progress_callback("completed", 100, "Видео сохранено", None)
        return destination_path.resolve()
    finally:
        cleanup_work_directory()


def download_audio(
    url: str,
    output_format: str,
    bitrate_kbps: int,
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> Path:
    """Скачивает аудиодорожку в MP3, M4A или исходном контейнере."""
    safe_url = validate_video_url(url)
    if output_format not in {"mp3", "m4a", "original"}:
        raise ValueError("Неподдерживаемый формат аудио")
    if bitrate_kbps not in {128, 192, 256, 320}:
        raise ValueError("Неподдерживаемое качество MP3")
    try:
        selected_range = validate_time_range(start_seconds, end_seconds)
    except ValueError as exc:
        raise VideoAnalysisError(str(exc)) from exc

    output_directory.mkdir(parents=True, exist_ok=True)
    work_directory = Path(tempfile.mkdtemp(prefix="local-audio-"))
    output_template = str(work_directory / "source.%(ext)s")

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
                "Скачиваем аудиодорожку",
                {
                    "downloaded_bytes": downloaded,
                    "total_bytes": total or None,
                    "speed_bytes_per_second": speed,
                    "eta_seconds": eta,
                },
            )
        elif data.get("status") == "finished":
            progress_callback("processing", None, "Подготавливаем аудиофайл", None)

    def postprocessor_hook(data: dict) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена")
        if data.get("status") == "started":
            action = "Конвертируем аудио в MP3" if output_format == "mp3" else "Подготавливаем M4A"
            progress_callback("processing", None, action, None)
        elif data.get("status") == "finished":
            progress_callback("processing", 100, "Аудио обработано", None)

    selected_format = "bestaudio[ext=m4a]/bestaudio/best" if output_format == "m4a" else "bestaudio/best"
    options = {
        **YTDLP_RUNTIME_OPTIONS,
        "format": selected_format,
        "outtmpl": output_template,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "overwrites": True,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": YTDLP_CONCURRENT_FRAGMENTS,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }
    if output_format == "mp3":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate_kbps),
        }]
    elif output_format == "m4a":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
        }]
    _apply_download_range(options, selected_range)

    progress_callback("preparing", None, "Получаем информацию об аудиодорожке", None)
    try:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(safe_url, download=True)
        except DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError as exc:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена") from exc
            raise VideoAnalysisError(_friendly_audio_download_error(str(exc))) from exc
        except Exception as exc:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена") from exc
            raise VideoAnalysisError("Не удалось скачать или обработать аудиодорожку") from exc

        if not isinstance(info, dict):
            raise VideoAnalysisError("Источник не вернул информацию об аудиодорожке")
        if selected_range is not None:
            try:
                selected_range = validate_time_range(*selected_range, info.get("duration"))
            except ValueError as exc:
                raise VideoAnalysisError(str(exc)) from exc
        title_suffix = ""
        if selected_range is not None:
            title_suffix = f" - фрагмент {format_duration(selected_range[0])}-{format_duration(selected_range[1])}"
        title = sanitize_filename(f"{info.get('title') or 'audio'}{title_suffix}", fallback="audio")
        extensions = (".mp3",) if output_format == "mp3" else (".m4a",) if output_format == "m4a" else (
            ".m4a", ".webm", ".opus", ".ogg", ".aac", ".mp3",
        )
        destination_stem = unique_output_stem(output_directory, title, extensions)
        candidates = [
            path
            for path in work_directory.glob("source.*")
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        ]
        expected_suffix = f".{output_format}" if output_format != "original" else None
        if expected_suffix:
            matching = [path for path in candidates if path.suffix.lower() == expected_suffix]
            if matching:
                candidates = matching
        if not candidates:
            raise VideoAnalysisError("Аудио скачалось, но итоговый файл не найден")
        final_path = max(candidates, key=lambda path: path.stat().st_mtime)
        destination_path = destination_stem.with_suffix(final_path.suffix.lower())
        progress_callback("saving", None, "Сохраняем аудиофайл", None)
        shutil.move(str(final_path), str(destination_path))
        progress_callback("completed", 100, "Аудио сохранено", None)
        return destination_path.resolve()
    finally:
        shutil.rmtree(work_directory, ignore_errors=True)


def format_size(value: int | float | None) -> str:
    if not value:
        return "0 Б"
    size = float(value)
    units = ("Б", "КБ", "МБ", "ГБ")
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}"


def _friendly_download_error(message: str) -> str:
    lowered = message.lower()
    if "ffmpeg" in lowered:
        return "Не удалось обработать видео через FFmpeg. Проверьте установку FFmpeg"
    if "no space left" in lowered or "not enough space" in lowered:
        return "Недостаточно свободного места на диске"
    if "unable to rename" in lowered or "winerror 32" in lowered:
        return "Не удалось завершить файл: он занят синхронизацией или другой программой"
    if "timed out" in lowered or "connection" in lowered:
        return "Соединение прервалось во время скачивания. Попробуйте ещё раз"
    return "Не удалось скачать видео. Проверьте соединение и попробуйте ещё раз"


def _friendly_audio_download_error(message: str) -> str:
    lowered = message.lower()
    if "ffmpeg" in lowered:
        return "Не удалось обработать аудио через FFmpeg. Проверьте установку FFmpeg"
    if "no space left" in lowered or "not enough space" in lowered:
        return "Недостаточно свободного места на диске"
    if "timed out" in lowered or "connection" in lowered:
        return "Соединение прервалось во время скачивания аудио. Попробуйте ещё раз"
    if "requested format is not available" in lowered:
        return "У источника не нашлось подходящей аудиодорожки"
    return "Не удалось скачать аудио. Проверьте доступность ссылки и соединение"
