from __future__ import annotations

from dataclasses import dataclass
import html
import math
from pathlib import Path
import re
import tempfile
from threading import Event
from typing import Callable, Literal
from urllib.parse import urlparse

import yt_dlp

from app.services.downloader import (
    DownloadCancelled,
    YTDLP_RUNTIME_OPTIONS,
    validate_video_url,
)
from app.services.markdown_builder import TranscriptSegment


ProgressDetails = dict[str, float | int | str | None]
ProgressCallback = Callable[[str, float | None, str, ProgressDetails | None], None]
SubtitleKind = Literal["manual", "automatic"]
SUPPORTED_SUBTITLE_FORMATS = ("vtt", "srt")

_LANGUAGE_RE = re.compile(r"^[a-zA-Z]{2,8}(?:[-_][a-zA-Z0-9]{1,12})*$")
_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d{1,3}):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2})"
    r"[.,](?P<milliseconds>\d{1,3})$"
)
_TAG_RE = re.compile(r"<[^>]*>")
_SRT_POSITION_RE = re.compile(r"\{\\[^}]+\}")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w+#.-]+", re.UNICODE)
_ROLLING_GAP_TOLERANCE = 0.05


class SubtitleTranscriptionError(RuntimeError):
    """Понятная пользователю ошибка обработки готовых субтитров."""


class SubtitlesUnavailableError(SubtitleTranscriptionError):
    """У источника нет подходящих готовых субтитров."""


@dataclass(frozen=True)
class SubtitleTrack:
    language: str
    kind: SubtitleKind
    file_format: str
    name: str | None = None

    @property
    def source_label(self) -> str:
        return "ручные субтитры" if self.kind == "manual" else "автоматические субтитры"


@dataclass(frozen=True)
class SubtitleTranscript:
    segments: list[TranscriptSegment]
    language: str
    track: SubtitleTrack


def select_subtitle_track(metadata: dict, language: str = "auto") -> SubtitleTrack | None:
    """Выбирает VTT/SRT: русский ручной, русский автоматический, затем выбранный язык."""
    if not isinstance(metadata, dict):
        return None
    selected_language = _validate_language(language)
    manual = _available_tracks(metadata.get("subtitles"), "manual")
    automatic = _available_tracks(metadata.get("automatic_captions"), "automatic")

    priority_groups: list[tuple[list[SubtitleTrack], str | None]] = [
        (manual, "ru"),
        (automatic, "ru"),
    ]
    if selected_language != "auto" and _language_base(selected_language) != "ru":
        priority_groups.extend(
            [
                (manual, selected_language),
                (automatic, selected_language),
            ]
        )
    elif selected_language == "auto":
        priority_groups.extend([(manual, None), (automatic, None)])

    for tracks, wanted_language in priority_groups:
        for track in _matching_tracks(tracks, wanted_language):
            return track
    return None


def transcribe_from_subtitles(
    url: str,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    metadata: dict | None = None,
    language: str = "auto",
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    temp_parent: Path | None = None,
) -> SubtitleTranscript:
    """Скачивает только выбранную дорожку субтитров и возвращает готовые сегменты."""
    safe_url = validate_video_url(url)
    selected_language = _validate_language(language)
    _validate_range(start_seconds, end_seconds)
    _raise_if_cancelled(cancel_event)

    source_metadata = metadata
    if source_metadata is None:
        progress_callback("checking_subtitles", 0, "Проверяем готовые субтитры", None)
        source_metadata = _extract_metadata(safe_url, cancel_event)

    track = select_subtitle_track(source_metadata, selected_language)
    if track is None:
        raise SubtitlesUnavailableError(
            "У видео нет подходящих готовых субтитров. Можно запустить локальное распознавание речи"
        )

    parent = Path(temp_parent).resolve() if temp_parent is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)

    progress_callback(
        "downloading_subtitles",
        10,
        f"Скачиваем {track.source_label}",
        {"language": track.language, "subtitle_kind": track.kind},
    )
    with tempfile.TemporaryDirectory(prefix="local-subtitles-", dir=parent) as temporary:
        subtitle_path = _download_subtitle(
            safe_url,
            track,
            Path(temporary),
            cancel_event,
        )
        _raise_if_cancelled(cancel_event)
        progress_callback("reading_subtitles", 90, "Подготавливаем текст субтитров", None)
        try:
            content = subtitle_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise SubtitleTranscriptionError(
                "Субтитры скачались, но их не удалось прочитать"
            ) from exc

        segments = parse_subtitle_text(
            content,
            file_format=subtitle_path.suffix.lstrip("."),
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            cancel_event=cancel_event,
        )

    if not segments:
        if start_seconds is not None or end_seconds is not None:
            raise SubtitleTranscriptionError("В выбранном фрагменте субтитров нет текста")
        raise SubtitleTranscriptionError("В загруженных субтитрах не найден текст")

    progress_callback(
        "subtitles_ready",
        100,
        "Готовые субтитры подготовлены",
        {"language": track.language, "subtitle_kind": track.kind},
    )
    return SubtitleTranscript(segments=segments, language=track.language, track=track)


def parse_subtitle_text(
    content: str,
    *,
    file_format: str,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    cancel_event: Event | None = None,
) -> list[TranscriptSegment]:
    """Разбирает VTT/SRT, удаляет rolling-дубли и сохраняет абсолютное время."""
    normalized_format = file_format.lower().lstrip(".")
    if normalized_format not in SUPPORTED_SUBTITLE_FORMATS:
        raise SubtitleTranscriptionError("Формат готовых субтитров пока не поддерживается")
    _validate_range(start_seconds, end_seconds)

    event = cancel_event or Event()
    raw_segments = _parse_cues(content, event)
    deduplicated = _remove_rolling_duplicates(raw_segments, event)
    return _filter_time_range(deduplicated, start_seconds, end_seconds)


def _extract_metadata(url: str, cancel_event: Event) -> dict:
    _raise_if_cancelled(cancel_event)
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
            metadata = ydl.extract_info(url, download=False)
    except DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise SubtitleTranscriptionError(
            "Не удалось проверить готовые субтитры. Проверьте доступность видео и соединение"
        ) from exc
    except Exception as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise SubtitleTranscriptionError("Не удалось проверить готовые субтитры") from exc
    if not isinstance(metadata, dict):
        raise SubtitleTranscriptionError("Источник не вернул информацию о субтитрах")
    return metadata


def _download_subtitle(
    url: str,
    track: SubtitleTrack,
    work_directory: Path,
    cancel_event: Event,
) -> Path:
    def progress_hook(_: dict) -> None:
        _raise_if_cancelled(cancel_event)

    options = {
        **YTDLP_RUNTIME_OPTIONS,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "writesubtitles": track.kind == "manual",
        "writeautomaticsub": track.kind == "automatic",
        "subtitleslangs": [track.language],
        "subtitlesformat": track.file_format,
        "outtmpl": str(work_directory / "subtitle.%(ext)s"),
        "overwrites": True,
        "socket_timeout": 20,
        "retries": 3,
        "progress_hooks": [progress_hook],
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise SubtitleTranscriptionError(
            "Не удалось скачать готовые субтитры. Проверьте доступность видео и соединение"
        ) from exc
    except Exception as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise SubtitleTranscriptionError("Не удалось подготовить готовые субтитры") from exc

    candidates = [
        path.resolve()
        for path in work_directory.glob("subtitle*")
        if path.is_file()
        and path.suffix.lower().lstrip(".") in SUPPORTED_SUBTITLE_FORMATS
        and path.resolve().is_relative_to(work_directory.resolve())
    ]
    if not candidates:
        raise SubtitleTranscriptionError("Субтитры загрузились, но временный файл не найден")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _available_tracks(raw_tracks: object, kind: SubtitleKind) -> list[SubtitleTrack]:
    if not isinstance(raw_tracks, dict):
        return []
    tracks: list[SubtitleTrack] = []
    for language, formats in raw_tracks.items():
        if not isinstance(language, str) or not _is_safe_track_language(language):
            continue
        if not isinstance(formats, list):
            continue
        available_formats: dict[str, str | None] = {}
        for item in formats:
            if not isinstance(item, dict):
                continue
            file_format = _subtitle_format(item)
            if file_format is None:
                continue
            name = item.get("name")
            available_formats.setdefault(file_format, name if isinstance(name, str) else None)
        for file_format in SUPPORTED_SUBTITLE_FORMATS:
            if file_format in available_formats:
                tracks.append(
                    SubtitleTrack(
                        language=language,
                        kind=kind,
                        file_format=file_format,
                        name=available_formats[file_format],
                    )
                )
                break
    return tracks


def _subtitle_format(item: dict) -> str | None:
    extension = str(item.get("ext") or "").lower().lstrip(".")
    if extension in SUPPORTED_SUBTITLE_FORMATS:
        return extension
    url = item.get("url")
    if isinstance(url, str):
        suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
        if suffix in SUPPORTED_SUBTITLE_FORMATS:
            return suffix
    return None


def _matching_tracks(
    tracks: list[SubtitleTrack],
    wanted_language: str | None,
) -> list[SubtitleTrack]:
    if wanted_language is None:
        return sorted(tracks, key=lambda item: item.language.casefold())
    normalized = wanted_language.replace("_", "-").casefold()
    exact = [
        item
        for item in tracks
        if item.language.replace("_", "-").casefold() == normalized
    ]
    variants = [
        item
        for item in tracks
        if item not in exact and _language_base(item.language) == _language_base(normalized)
    ]
    return exact + sorted(variants, key=lambda item: item.language.casefold())


def _parse_cues(content: str, cancel_event: Event) -> list[TranscriptSegment]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    segments: list[TranscriptSegment] = []
    for block in re.split(r"\n[ \t]*\n", normalized):
        _raise_if_cancelled(cancel_event)
        lines = [line.strip() for line in block.splitlines()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", maxsplit=1)
        if len(timing) != 2:
            continue
        try:
            start = _parse_timestamp(timing[0].strip().split()[0])
            end = _parse_timestamp(timing[1].strip().split()[0])
        except (IndexError, ValueError):
            continue
        if end <= start:
            continue
        text = _clean_cue_text(" ".join(lines[timing_index + 1 :]))
        if text:
            segments.append(TranscriptSegment(start=start, end=end, text=text))
    return sorted(segments, key=lambda item: (item.start, item.end))


def _parse_timestamp(value: str) -> float:
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ValueError("invalid subtitle timestamp")
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    milliseconds_text = match.group("milliseconds").ljust(3, "0")
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid subtitle timestamp")
    return hours * 3600 + minutes * 60 + seconds + int(milliseconds_text) / 1000


def _clean_cue_text(value: str) -> str:
    without_positions = _SRT_POSITION_RE.sub("", value)
    without_tags = _TAG_RE.sub("", without_positions)
    decoded = html.unescape(without_tags)
    decoded = decoded.replace("\u200b", "").replace("\u200e", "").replace("\u200f", "")
    return _SPACE_RE.sub(" ", decoded).strip()


def _remove_rolling_duplicates(
    segments: list[TranscriptSegment],
    cancel_event: Event,
) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    previous_cue: TranscriptSegment | None = None
    previous_tokens: list[str] = []

    for segment in segments:
        _raise_if_cancelled(cancel_event)
        tokens = segment.text.split()
        normalized_tokens = [_normalize_token(token) for token in tokens]
        normalized_tokens = [token for token in normalized_tokens if token]
        # YouTube часто завершает промежуточную строку за 10 мс до следующей.
        # Для зрителя это одна «бегущая» подпись, хотя интервалы формально
        # уже не пересекаются. Считаем такие соседние строки продолжением.
        rolling_neighbour = (
            previous_cue is not None
            and segment.start <= previous_cue.end + _ROLLING_GAP_TOLERANCE
        )
        text = segment.text

        if rolling_neighbour and previous_tokens and normalized_tokens == previous_tokens:
            if result:
                last = result[-1]
                result[-1] = TranscriptSegment(last.start, max(last.end, segment.end), last.text)
            previous_cue = segment
            previous_tokens = normalized_tokens
            continue

        if rolling_neighbour and previous_tokens and _starts_with(normalized_tokens, previous_tokens):
            text = " ".join(tokens[len(previous_tokens) :]).strip()
        elif rolling_neighbour and previous_tokens and _starts_with(previous_tokens, normalized_tokens):
            text = ""
        elif rolling_neighbour and previous_tokens:
            overlap = _token_overlap(previous_tokens, normalized_tokens)
            if overlap >= 2 or (overlap == 1 and len(previous_tokens[-1]) >= 4):
                text = " ".join(tokens[overlap:]).strip()

        if text:
            result.append(TranscriptSegment(segment.start, segment.end, text))
        previous_cue = segment
        previous_tokens = normalized_tokens
    return result


def _filter_time_range(
    segments: list[TranscriptSegment],
    start_seconds: float | None,
    end_seconds: float | None,
) -> list[TranscriptSegment]:
    start_limit = max(0.0, float(start_seconds or 0.0))
    end_limit = float(end_seconds) if end_seconds is not None else None
    result: list[TranscriptSegment] = []
    for segment in segments:
        if segment.end <= start_limit:
            continue
        if end_limit is not None and segment.start >= end_limit:
            continue
        start = max(segment.start, start_limit)
        end = min(segment.end, end_limit) if end_limit is not None else segment.end
        if end > start:
            result.append(TranscriptSegment(start, end, segment.text))
    return result


def _validate_language(language: str) -> str:
    normalized = str(language or "auto").strip()
    if normalized == "auto":
        return normalized
    if not _LANGUAGE_RE.fullmatch(normalized):
        raise SubtitleTranscriptionError("Выбран некорректный язык субтитров")
    return normalized


def _validate_range(start_seconds: float | None, end_seconds: float | None) -> None:
    start = float(start_seconds) if start_seconds is not None else None
    end = float(end_seconds) if end_seconds is not None else None
    if start is not None and (not math.isfinite(start) or start < 0):
        raise SubtitleTranscriptionError("Начало фрагмента не может быть отрицательным")
    if end is not None and (not math.isfinite(end) or end <= 0):
        raise SubtitleTranscriptionError("Конец фрагмента должен быть больше нуля")
    if start is not None and end is not None:
        if end <= start:
            raise SubtitleTranscriptionError("Конец фрагмента должен быть позже начала")


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise DownloadCancelled("Операция отменена")


def _is_safe_track_language(language: str) -> bool:
    return len(language) <= 64 and _LANGUAGE_RE.fullmatch(language) is not None


def _language_base(language: str) -> str:
    return language.replace("_", "-").split("-", maxsplit=1)[0].casefold()


def _normalize_token(token: str) -> str:
    matches = _WORD_RE.findall(token.casefold())
    return "".join(matches)


def _starts_with(tokens: list[str], prefix: list[str]) -> bool:
    return len(tokens) >= len(prefix) and tokens[: len(prefix)] == prefix


def _token_overlap(previous: list[str], current: list[str]) -> int:
    maximum = min(len(previous), len(current))
    for length in range(maximum, 0, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0
