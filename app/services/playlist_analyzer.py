from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from threading import Event
from urllib.parse import urldefrag, urlsplit, urlunsplit

import yt_dlp

from app.services import downloader
from app.services.downloader import YTDLP_RUNTIME_OPTIONS, format_duration


MAX_QUEUE_ITEMS = 50

# В текущем проекте валидатор называется validate_video_url. Локальное имя
# оставляет сервис совместимым с более общим названием при дальнейшем рефакторинге.
validate_media_url = getattr(downloader, "validate_media_url", downloader.validate_video_url)


class QueueAnalysisError(RuntimeError):
    """Понятная пользователю ошибка анализа очереди или плейлиста."""


class QueueAnalysisCancelled(RuntimeError):
    """Пользователь отменил анализ очереди или плейлиста."""


@dataclass(frozen=True, slots=True)
class QueueItem:
    index: int
    url: str
    title: str
    duration_seconds: int | None
    duration_text: str


@dataclass(frozen=True, slots=True)
class QueueAnalysis:
    items: list[QueueItem]
    source_title: str | None
    is_playlist: bool
    truncated: bool


def analyze_queue(
    source: str | Sequence[str],
    cancel_event: Event | None = None,
) -> QueueAnalysis:
    """Анализирует одну ссылку/плейлист или список обычных ссылок без скачивания."""

    event = cancel_event or Event()
    _raise_if_cancelled(event)

    if isinstance(source, str):
        safe_url = _validate_url(source, label="Ссылка")
        return _analyze_playlist_or_single(safe_url, event)

    if not isinstance(source, Sequence) or not source:
        raise QueueAnalysisError("Добавьте хотя бы одну ссылку")

    return _analyze_url_list(source, event)


def _analyze_playlist_or_single(url: str, cancel_event: Event) -> QueueAnalysis:
    options = _yt_dlp_options(cancel_event, allow_playlist=True)
    info = _extract_info(url, options, cancel_event, context="проанализировать ссылку")

    entries = info.get("entries")
    if entries is None:
        item = _queue_item_from_info(info, fallback_url=url, index=1)
        if item is None:
            raise QueueAnalysisError("Сервис не вернул безопасную ссылку на видео")
        return QueueAnalysis(
            items=[item],
            source_title=None,
            is_playlist=False,
            truncated=False,
        )

    items: list[QueueItem] = []
    identities: set[str] = set()
    truncated = False
    for entry in entries:
        _raise_if_cancelled(cancel_event)
        if not isinstance(entry, dict):
            continue

        item = _queue_item_from_info(entry, fallback_url=None, index=len(items) + 1)
        if item is None:
            continue
        identity = _media_identity(entry, item.url)
        if identity in identities:
            continue
        if len(items) >= MAX_QUEUE_ITEMS:
            truncated = True
            break

        identities.add(identity)
        items.append(item)

    _raise_if_cancelled(cancel_event)
    if not items:
        raise QueueAnalysisError("В плейлисте не найдено доступных видео")

    return QueueAnalysis(
        items=items,
        source_title=_clean_title(info.get("title"), fallback="Плейлист"),
        is_playlist=True,
        truncated=truncated or _reported_count_exceeds_limit(info),
    )


def _analyze_url_list(urls: Sequence[str], cancel_event: Event) -> QueueAnalysis:
    safe_urls: list[str] = []
    normalized_urls: set[str] = set()
    truncated = False

    for position, value in enumerate(urls, start=1):
        _raise_if_cancelled(cancel_event)
        if not isinstance(value, str):
            raise QueueAnalysisError(f"Ссылка №{position} имеет неверный формат")
        safe_url = _validate_url(value, label=f"Ссылка №{position}")
        key = _normalized_url(safe_url)
        if key in normalized_urls:
            continue
        if len(safe_urls) >= MAX_QUEUE_ITEMS:
            truncated = True
            break
        normalized_urls.add(key)
        safe_urls.append(safe_url)

    if not safe_urls:
        raise QueueAnalysisError("Добавьте хотя бы одну уникальную ссылку")

    options = _yt_dlp_options(cancel_event, allow_playlist=False)
    items: list[QueueItem] = []
    identities: set[str] = set()
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            for position, safe_url in enumerate(safe_urls, start=1):
                _raise_if_cancelled(cancel_event)
                try:
                    info = ydl.extract_info(safe_url, download=False)
                except QueueAnalysisCancelled:
                    raise
                except yt_dlp.utils.DownloadError as exc:
                    if cancel_event.is_set():
                        raise QueueAnalysisCancelled("Анализ отменён") from exc
                    raise QueueAnalysisError(
                        _friendly_yt_dlp_error(str(exc), context=f"проверить ссылку №{position}")
                    ) from exc
                except Exception as exc:
                    if cancel_event.is_set():
                        raise QueueAnalysisCancelled("Анализ отменён") from exc
                    raise QueueAnalysisError(f"Не удалось проверить ссылку №{position}") from exc

                _raise_if_cancelled(cancel_event)
                if not isinstance(info, dict):
                    raise QueueAnalysisError(f"Сервис не вернул данные для ссылки №{position}")
                item = _queue_item_from_info(info, fallback_url=safe_url, index=len(items) + 1)
                if item is None:
                    raise QueueAnalysisError(f"Сервис вернул небезопасный адрес для ссылки №{position}")
                identity = _media_identity(info, item.url)
                if identity in identities:
                    continue
                identities.add(identity)
                items.append(item)
    except QueueAnalysisError:
        raise
    except QueueAnalysisCancelled:
        raise
    except Exception as exc:
        raise QueueAnalysisError("Не удалось проанализировать список ссылок") from exc

    _raise_if_cancelled(cancel_event)
    if not items:
        raise QueueAnalysisError("В списке не найдено доступных видео")

    return QueueAnalysis(
        items=items,
        source_title=None,
        is_playlist=False,
        truncated=truncated,
    )


def _extract_info(
    url: str,
    options: dict,
    cancel_event: Event,
    *,
    context: str,
) -> dict:
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except QueueAnalysisCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event.is_set():
            raise QueueAnalysisCancelled("Анализ отменён") from exc
        raise QueueAnalysisError(_friendly_yt_dlp_error(str(exc), context=context)) from exc
    except Exception as exc:
        if cancel_event.is_set():
            raise QueueAnalysisCancelled("Анализ отменён") from exc
        raise QueueAnalysisError(f"Не удалось {context}") from exc

    _raise_if_cancelled(cancel_event)
    if not isinstance(info, dict):
        raise QueueAnalysisError("Сервис не вернул информацию о видео")
    return info


def _yt_dlp_options(cancel_event: Event, *, allow_playlist: bool) -> dict:
    def cancel_filter(_info: dict, *, incomplete: bool) -> None:
        del incomplete
        _raise_if_cancelled(cancel_event)
        return None

    options = {
        **YTDLP_RUNTIME_OPTIONS,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": not allow_playlist,
        "socket_timeout": 20,
        "match_filter": cancel_filter,
    }
    if allow_playlist:
        options["playlistend"] = MAX_QUEUE_ITEMS + 1
    return options


def _queue_item_from_info(
    info: dict,
    *,
    fallback_url: str | None,
    index: int,
) -> QueueItem | None:
    raw_url = info.get("webpage_url") or info.get("original_url") or info.get("url") or fallback_url
    if not isinstance(raw_url, str):
        return None
    try:
        safe_url = validate_media_url(raw_url)
        _normalized_url(safe_url)
    except (TypeError, ValueError):
        if fallback_url is None:
            return None
        try:
            safe_url = validate_media_url(fallback_url)
            _normalized_url(safe_url)
        except (TypeError, ValueError):
            return None

    duration = _duration_seconds(info.get("duration"))
    return QueueItem(
        index=index,
        url=safe_url,
        title=_clean_title(info.get("title"), fallback=f"Видео {index}"),
        duration_seconds=duration,
        duration_text=format_duration(duration),
    )


def _validate_url(value: str, *, label: str) -> str:
    try:
        safe_url = validate_media_url(value)
        _normalized_url(safe_url)
        return safe_url
    except (TypeError, ValueError) as exc:
        message = str(exc).strip() or "Введите корректную ссылку с http:// или https://"
        raise QueueAnalysisError(f"{label}: {message}") from exc


def _duration_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _clean_title(value: object, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    return cleaned or fallback


def _media_identity(info: dict, url: str) -> str:
    media_id = info.get("id")
    extractor = info.get("extractor_key") or info.get("extractor")
    if isinstance(media_id, str) and media_id.strip() and isinstance(extractor, str) and extractor.strip():
        return f"{extractor.strip().lower()}:{media_id.strip()}"
    return _normalized_url(url)


def _normalized_url(url: str) -> str:
    value, _fragment = urldefrag(url.strip())
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


def _reported_count_exceeds_limit(info: dict) -> bool:
    count = info.get("playlist_count") or info.get("n_entries")
    return isinstance(count, (int, float)) and count > MAX_QUEUE_ITEMS


def _friendly_yt_dlp_error(message: str, *, context: str) -> str:
    lowered = message.lower()
    if "private" in lowered:
        return "Видео или плейлист приватный и недоступен"
    if "sign in" in lowered or "login" in lowered or "age-restricted" in lowered:
        return "Источник требует входа в аккаунт или имеет возрастное ограничение"
    if "unavailable" in lowered or "not available" in lowered or "removed" in lowered:
        return "Видео или плейлист недоступен либо удалён"
    return f"Не удалось {context}. Проверьте ссылку и подключение к интернету"


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise QueueAnalysisCancelled("Анализ отменён")
