from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock
from typing import Callable

import numpy as np
import yt_dlp

from app.config import (
    MODELS_DIR,
    SUPPORTED_LOCAL_MEDIA_EXTENSIONS,
    WHISPER_BEAM_SIZE,
    WHISPER_CPU_BATCH_SIZE,
    WHISPER_CPU_THREADS,
    WHISPER_GPU_BATCH_SIZE,
    YTDLP_CONCURRENT_FRAGMENTS,
)
from app.services.downloader import (
    DownloadCancelled,
    FFMPEG_DIRECTORY,
    YTDLP_RUNTIME_OPTIONS,
    format_duration,
    validate_time_range,
    validate_video_url,
)
from app.services.file_manager import sanitize_filename, unique_output_stem
from app.services.gigaam_transcriber import (
    GIGAAM_DISPLAY_NAME,
    SAMPLE_RATE,
    GigaamError,
    GigaamSegment,
    recognize_gigaam,
)
from app.services.hybrid_transcriber import (
    align_whisper_boundaries,
    build_whisper_prompt,
    extract_glossary,
    select_hybrid_candidates,
    should_accept_whisper_text,
)
from app.services.markdown_builder import (
    TranscriptMetadata,
    TranscriptSegment,
    build_blocks,
    build_markdown,
    build_srt,
    build_text,
    build_vtt,
    clean_segments,
)
from app.services.subtitle_transcriber import (
    SubtitleTranscriptionError,
    SubtitlesUnavailableError,
    transcribe_from_subtitles,
)
from app.services.speaker_diarizer import (
    SpeakerDiarizationError,
    assign_speakers_to_segments,
    diarize_speakers as detect_speakers,
    validate_speaker_count,
)
from app.services.technical_dictionary import clean_custom_terms, select_relevant_terms


logger = logging.getLogger(__name__)
ProgressDetails = dict[str, float | int | str | None]
ProgressCallback = Callable[[str, float | None, str, ProgressDetails | None], None]
SUPPORTED_FORMATS = ("md", "txt", "srt", "vtt")
SUPPORTED_ENGINES = ("whisper", "gigaam", "hybrid")
SUPPORTED_TEXT_SOURCES = ("auto", "subtitles", "speech")
_INFERENCE_LOCK = Lock()


class TranscriptionError(RuntimeError):
    """Понятная пользователю ошибка локальной транскрибации."""


def transcribe_video(
    url: str,
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    engine: str = "whisper",
    model_name: str = "large-v3",
    language: str = "auto",
    formats: tuple[str, ...] = ("md",),
    include_timestamps: bool = True,
    paragraphize: bool = True,
    remove_short_fragments: bool = True,
    text_source: str = "auto",
    custom_terms: tuple[str, ...] = (),
    diarize_speakers: bool = False,
    speaker_count: int | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> list[Path]:
    safe_url = validate_video_url(url)
    selected_formats = _validate_formats(formats)
    selected_custom_terms = clean_custom_terms(custom_terms)
    try:
        validate_speaker_count(speaker_count if diarize_speakers else None)
    except ValueError as exc:
        raise TranscriptionError(str(exc)) from exc
    if text_source not in SUPPORTED_TEXT_SOURCES:
        raise TranscriptionError("Выбран неизвестный источник текста")
    try:
        selected_range = validate_time_range(start_seconds, end_seconds)
    except ValueError as exc:
        raise TranscriptionError(str(exc)) from exc
    work_directory = Path(tempfile.mkdtemp(prefix="local-transcription-"))

    try:
        if text_source in {"auto", "subtitles"}:
            metadata = _extract_video_metadata(safe_url, progress_callback, cancel_event)
            try:
                subtitle_result = transcribe_from_subtitles(
                    safe_url,
                    progress_callback,
                    cancel_event,
                    metadata=metadata,
                    language=language,
                    start_seconds=selected_range[0] if selected_range else None,
                    end_seconds=selected_range[1] if selected_range else None,
                    temp_parent=work_directory,
                )
            except SubtitlesUnavailableError as exc:
                if text_source == "subtitles":
                    raise TranscriptionError(str(exc)) from exc
                progress_callback(
                    "preparing_audio",
                    None,
                    "Готовых субтитров нет, запускаем локальное распознавание",
                    None,
                )
            except SubtitleTranscriptionError as exc:
                if text_source == "subtitles":
                    raise TranscriptionError(str(exc)) from exc
                logger.warning("Не удалось использовать готовые субтитры, переключаемся на речь: %s", exc)
                progress_callback(
                    "preparing_audio",
                    None,
                    "Субтитры недоступны, запускаем локальное распознавание",
                    None,
                )
            else:
                subtitle_segments = subtitle_result.segments
                if diarize_speakers:
                    audio_path, _ = _download_audio(
                        safe_url,
                        work_directory,
                        progress_callback,
                        cancel_event,
                        start_seconds=selected_range[0] if selected_range else None,
                        end_seconds=selected_range[1] if selected_range else None,
                    )
                    timestamp_offset = selected_range[0] if selected_range else 0.0
                    relative_segments = _shift_segment_timestamps(
                        subtitle_segments,
                        -timestamp_offset,
                    )
                    relative_segments = _apply_speaker_labels(
                        audio_path,
                        relative_segments,
                        progress_callback,
                        cancel_event,
                        speaker_count=speaker_count,
                        acquire_slot=True,
                    )
                    subtitle_segments = _shift_segment_timestamps(
                        relative_segments,
                        timestamp_offset,
                    )
                return _save_subtitle_outputs(
                    subtitle_segments,
                    subtitle_result.language,
                    subtitle_result.track.source_label,
                    metadata,
                    safe_url,
                    output_directory,
                    selected_formats,
                    include_timestamps,
                    paragraphize,
                    remove_short_fragments,
                    progress_callback,
                    selected_range,
                    speakers_enabled=diarize_speakers,
                )

        audio_path, video_info = _download_audio(
            safe_url,
            work_directory,
            progress_callback,
            cancel_event,
            start_seconds=selected_range[0] if selected_range else None,
            end_seconds=selected_range[1] if selected_range else None,
        )
        return _transcribe_source(
            audio_path,
            output_directory,
            progress_callback,
            cancel_event,
            source_info=video_info,
            source_value=safe_url,
            source_label="Ссылка",
            engine=engine,
            model_name=model_name,
            language=language,
            formats=selected_formats,
            include_timestamps=include_timestamps,
            paragraphize=paragraphize,
            remove_short_fragments=remove_short_fragments,
            custom_terms=selected_custom_terms,
            diarize_speakers=diarize_speakers,
            speaker_count=speaker_count,
            timestamp_offset=selected_range[0] if selected_range else 0.0,
        )
    finally:
        shutil.rmtree(work_directory, ignore_errors=True)


def transcribe_local_file(
    source_path: Path,
    original_name: str,
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    engine: str = "whisper",
    model_name: str = "large-v3",
    language: str = "auto",
    formats: tuple[str, ...] = ("md",),
    include_timestamps: bool = True,
    paragraphize: bool = True,
    remove_short_fragments: bool = True,
    custom_terms: tuple[str, ...] = (),
    diarize_speakers: bool = False,
    speaker_count: int | None = None,
) -> list[Path]:
    path = source_path.resolve()
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_LOCAL_MEDIA_EXTENSIONS:
        raise TranscriptionError("Выбранный локальный медиафайл недоступен или имеет неподдерживаемый формат")

    selected_formats = _validate_formats(formats)
    selected_custom_terms = clean_custom_terms(custom_terms)
    try:
        validate_speaker_count(speaker_count if diarize_speakers else None)
    except ValueError as exc:
        raise TranscriptionError(str(exc)) from exc
    safe_name = sanitize_filename(Path(original_name).name, fallback=f"медиа{path.suffix.lower()}")
    progress_callback("preparing_audio", None, "Проверяем локальный медиафайл", None)
    return _transcribe_source(
        path,
        output_directory,
        progress_callback,
        cancel_event,
        source_info={"title": Path(safe_name).stem},
        source_value=safe_name,
        source_label="Файл",
        engine=engine,
        model_name=model_name,
        language=language,
        formats=selected_formats,
        include_timestamps=include_timestamps,
        paragraphize=paragraphize,
        remove_short_fragments=remove_short_fragments,
        custom_terms=selected_custom_terms,
        diarize_speakers=diarize_speakers,
        speaker_count=speaker_count,
        timestamp_offset=0.0,
    )


def _extract_video_metadata(
    url: str,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> dict:
    _raise_if_cancelled(cancel_event)
    progress_callback("checking_subtitles", 0, "Проверяем готовые субтитры", None)
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
        raise TranscriptionError("Не удалось проверить субтитры и информацию о видео") from exc
    except Exception as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise TranscriptionError("Не удалось получить информацию о видео") from exc
    if not isinstance(metadata, dict):
        raise TranscriptionError("Источник не вернул информацию о видео")
    return metadata


def _save_subtitle_outputs(
    raw_segments: list[TranscriptSegment],
    language: str,
    track_label: str,
    source_info: dict,
    source_url: str,
    output_directory: Path,
    formats: tuple[str, ...],
    include_timestamps: bool,
    paragraphize: bool,
    remove_short_fragments: bool,
    progress_callback: ProgressCallback,
    selected_range: tuple[float, float] | None,
    *,
    speakers_enabled: bool = False,
) -> list[Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    segments = clean_segments(raw_segments, remove_short_fragments=remove_short_fragments)
    if not segments:
        raise TranscriptionError("В готовых субтитрах не найден текст")
    blocks = build_blocks(segments, paragraphize=paragraphize)
    if selected_range is not None:
        duration = selected_range[1] - selected_range[0]
    else:
        duration = float(source_info.get("duration") or max(segment.end for segment in segments))
    progress_callback("saving", None, "Создаём файлы из готовых субтитров", None)
    metadata = TranscriptMetadata(
        title=str(source_info.get("title") or "Видео без названия"),
        source_url=source_url,
        author=source_info.get("channel") or source_info.get("uploader"),
        duration_text=format_duration(duration),
        language=language,
        model=(
            f"Готовые субтитры источника ({track_label}) + разделение по спикерам"
            if speakers_enabled
            else f"Готовые субтитры источника ({track_label})"
        ),
        created_at=datetime.now().astimezone(),
        source_label="Ссылка",
    )
    files = _save_outputs(
        output_directory,
        metadata,
        segments,
        blocks,
        formats,
        include_timestamps,
    )
    message = (
        "Транскрибация из субтитров со спикерами сохранена"
        if speakers_enabled
        else "Транскрибация из субтитров сохранена"
    )
    progress_callback("completed", 100, message, None)
    return files


def _technical_glossary(
    source_info: dict,
    *,
    text: str = "",
    custom_terms: tuple[str, ...] = (),
) -> tuple[str, ...]:
    try:
        selected = select_relevant_terms(
            metadata=source_info,
            text=text,
            custom_terms=custom_terms,
            limit=80,
        )
    except RuntimeError as exc:
        logger.warning("Встроенный технический словарь недоступен: %s", exc)
        selected = clean_custom_terms(custom_terms)

    result: list[str] = []
    seen: set[str] = set()
    for value in (*selected, *extract_glossary(source_info)):
        term = str(value).strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            result.append(term)
        if len(result) >= 96:
            break
    return tuple(result)


def _validate_formats(formats: tuple[str, ...]) -> tuple[str, ...]:
    selected = tuple(item for item in SUPPORTED_FORMATS if item in set(formats))
    if not selected:
        raise TranscriptionError("Выберите хотя бы один формат транскрибации")
    return selected


def _validate_engine(engine: str, language: str) -> str:
    if engine not in SUPPORTED_ENGINES:
        raise TranscriptionError("Выбран неизвестный режим распознавания речи")
    if engine in {"gigaam", "hybrid"} and language == "en":
        raise TranscriptionError("GigaAM предназначена для русской речи. Для английского выберите Whisper")
    return engine


def _acquire_inference_slot(
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> None:
    acquired = _INFERENCE_LOCK.acquire(blocking=False)
    if not acquired:
        progress_callback(
            "queued",
            None,
            "Ожидаем завершения другой тяжёлой обработки",
            None,
        )
        while not acquired:
            _raise_if_cancelled(cancel_event)
            acquired = _INFERENCE_LOCK.acquire(timeout=0.25)


def _shift_segment_timestamps(
    segments: list[TranscriptSegment],
    offset: float,
) -> list[TranscriptSegment]:
    shifted: list[TranscriptSegment] = []
    for segment in segments:
        start = max(0.0, segment.start + offset)
        end = max(start, segment.end + offset)
        shifted.append(
            TranscriptSegment(
                start=start,
                end=end,
                text=segment.text,
                speaker=segment.speaker,
            )
        )
    return shifted


def _apply_speaker_labels(
    source_path: Path,
    segments: list[TranscriptSegment],
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    speaker_count: int | None,
    acquire_slot: bool,
) -> list[TranscriptSegment]:
    if not segments:
        return segments

    slot_acquired = False
    if acquire_slot:
        _acquire_inference_slot(progress_callback, cancel_event)
        slot_acquired = True

    try:
        turns = detect_speakers(
            source_path,
            progress_callback,
            cancel_event,
            speaker_count=speaker_count,
        )
        if not turns:
            raise TranscriptionError("В записи не удалось определить голоса спикеров")
        assignments = assign_speakers_to_segments(segments, turns)
        return [
            TranscriptSegment(
                start=assignment.segment.start,
                end=assignment.segment.end,
                text=assignment.segment.text,
                speaker=assignment.label,
            )
            for assignment in assignments
        ]
    except DownloadCancelled:
        raise
    except SpeakerDiarizationError as exc:
        raise TranscriptionError(str(exc)) from exc
    except ValueError as exc:
        raise TranscriptionError(str(exc)) from exc
    finally:
        if slot_acquired:
            _INFERENCE_LOCK.release()


def _transcribe_source(
    source_path: Path,
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    source_info: dict,
    source_value: str,
    source_label: str,
    engine: str,
    model_name: str,
    language: str,
    formats: tuple[str, ...],
    include_timestamps: bool,
    paragraphize: bool,
    remove_short_fragments: bool,
    custom_terms: tuple[str, ...] = (),
    diarize_speakers: bool = False,
    speaker_count: int | None = None,
    timestamp_offset: float = 0.0,
) -> list[Path]:
    engine = _validate_engine(engine, language)
    _raise_if_cancelled(cancel_event)
    _acquire_inference_slot(progress_callback, cancel_event)

    try:
        return _transcribe_source_with_slot(
            source_path,
            output_directory,
            progress_callback,
            cancel_event,
            source_info=source_info,
            source_value=source_value,
            source_label=source_label,
            engine=engine,
            model_name=model_name,
            language=language,
            formats=formats,
            include_timestamps=include_timestamps,
            paragraphize=paragraphize,
            remove_short_fragments=remove_short_fragments,
            custom_terms=custom_terms,
            diarize_speakers=diarize_speakers,
            speaker_count=speaker_count,
            timestamp_offset=timestamp_offset,
        )
    finally:
        _INFERENCE_LOCK.release()


def _transcribe_source_with_slot(
    source_path: Path,
    output_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    source_info: dict,
    source_value: str,
    source_label: str,
    engine: str,
    model_name: str,
    language: str,
    formats: tuple[str, ...],
    include_timestamps: bool,
    paragraphize: bool,
    remove_short_fragments: bool,
    custom_terms: tuple[str, ...] = (),
    diarize_speakers: bool = False,
    speaker_count: int | None = None,
    timestamp_offset: float = 0.0,
) -> list[Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _raise_if_cancelled(cancel_event)

    if engine == "whisper":
        raw_segments, detected_language, duration = _recognize_with_whisper(
            source_path,
            source_info,
            progress_callback,
            cancel_event,
            model_name=model_name,
            language=language,
            custom_terms=custom_terms,
        )
        model_label = f"Whisper {model_name}"
    else:
        waveform = _decode_source_audio(source_path, progress_callback, cancel_event)
        duration = len(waveform) / SAMPLE_RATE
        if not source_info.get("duration"):
            source_info["duration"] = duration
        try:
            gigaam_segments = recognize_gigaam(
                waveform,
                duration,
                progress_callback,
                cancel_event,
            )
        except GigaamError as exc:
            raise TranscriptionError(str(exc)) from exc

        if engine == "hybrid":
            raw_segments = _refine_with_whisper(
                waveform,
                duration,
                gigaam_segments,
                source_info,
                progress_callback,
                cancel_event,
                custom_terms=custom_terms,
            )
            model_label = f"{GIGAAM_DISPLAY_NAME} + Whisper large-v3"
        else:
            raw_segments = [
                TranscriptSegment(start=segment.start, end=segment.end, text=segment.text)
                for segment in gigaam_segments
            ]
            model_label = GIGAAM_DISPLAY_NAME
        detected_language = "ru"

    if diarize_speakers:
        raw_segments = _apply_speaker_labels(
            source_path,
            raw_segments,
            progress_callback,
            cancel_event,
            speaker_count=speaker_count,
            acquire_slot=False,
        )
        model_label += " + разделение по спикерам"

    if timestamp_offset:
        raw_segments = [
            TranscriptSegment(
                start=segment.start + timestamp_offset,
                end=segment.end + timestamp_offset,
                text=segment.text,
                speaker=segment.speaker,
            )
            for segment in raw_segments
        ]
    segments = clean_segments(raw_segments, remove_short_fragments=remove_short_fragments)
    if not segments:
        raise TranscriptionError("В аудиодорожке не удалось обнаружить распознаваемую речь")
    blocks = build_blocks(segments, paragraphize=paragraphize)
    final_duration = duration or max((segment.end for segment in segments), default=0)

    progress_callback("saving", None, "Создаём файлы транскрибации", None)
    metadata = TranscriptMetadata(
        title=str(source_info.get("title") or "Видео без названия"),
        source_url=source_value,
        author=source_info.get("channel") or source_info.get("uploader"),
        duration_text=format_duration(final_duration),
        language=detected_language,
        model=model_label,
        created_at=datetime.now().astimezone(),
        source_label=source_label,
    )
    files = _save_outputs(
        output_directory,
        metadata,
        segments,
        blocks,
        formats,
        include_timestamps,
    )
    progress_callback("completed", 100, "Транскрибация сохранена", None)
    return files


def _recognize_with_whisper(
    source_path: Path,
    source_info: dict,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    model_name: str,
    language: str,
    custom_terms: tuple[str, ...] = (),
) -> tuple[list[TranscriptSegment], str, float]:
    progress_callback("model", None, f"Подготавливаем модель Whisper: {model_name}", None)
    model, device_label, batch_size = _load_model(model_name)
    _raise_if_cancelled(cancel_event)

    requested_language = None if language == "auto" else language
    progress_callback(
        "transcribing",
        0,
        f"Распознаём речь на {device_label}",
        {"processed_seconds": 0, "total_seconds": source_info.get("duration")},
    )
    started_at = time.monotonic()

    try:
        from faster_whisper import BatchedInferencePipeline

        pipeline = BatchedInferencePipeline(model=model)
        glossary = _technical_glossary(source_info, custom_terms=custom_terms)
        segment_iterator, recognition_info = pipeline.transcribe(
            str(source_path),
            language=requested_language,
            beam_size=WHISPER_BEAM_SIZE,
            best_of=WHISPER_BEAM_SIZE,
            batch_size=batch_size,
            vad_filter=True,
            condition_on_previous_text=True,
            without_timestamps=False,
            initial_prompt=build_whisper_prompt(glossary) or None,
            hotwords=", ".join(glossary)[:500] or None,
        )
        raw_segments: list[TranscriptSegment] = []
        duration = float(getattr(recognition_info, "duration", 0) or source_info.get("duration") or 0)

        for segment in segment_iterator:
            _raise_if_cancelled(cancel_event)
            segment_end = max(0, float(segment.end))
            raw_segments.append(
                TranscriptSegment(
                    start=float(segment.start),
                    end=segment_end,
                    text=str(segment.text),
                )
            )
            if duration > 0:
                ratio = min(1, segment_end / duration)
                elapsed = time.monotonic() - started_at
                eta = elapsed * (1 - ratio) / ratio if ratio >= 0.01 else None
                progress_callback(
                    "transcribing",
                    ratio * 100,
                    "Распознаём речь и собираем фрагменты",
                    {
                        "processed_seconds": min(segment_end, duration),
                        "total_seconds": duration,
                        "eta_seconds": eta,
                    },
                )
        detected_language = str(
            getattr(recognition_info, "language", None) or requested_language or "не определён"
        )
        return raw_segments, detected_language, duration
    except DownloadCancelled:
        raise
    except Exception as exc:
        raise TranscriptionError(_friendly_transcription_error(exc)) from exc


def _decode_source_audio(
    source_path: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> np.ndarray:
    _raise_if_cancelled(cancel_event)
    progress_callback("preparing_audio", None, "Подготавливаем аудио для распознавания", None)
    try:
        from faster_whisper.audio import decode_audio

        waveform = decode_audio(str(source_path), sampling_rate=SAMPLE_RATE)
    except Exception as exc:
        raise TranscriptionError("Не удалось прочитать аудиодорожку этого медиафайла") from exc
    _raise_if_cancelled(cancel_event)
    if not isinstance(waveform, np.ndarray) or waveform.size == 0:
        raise TranscriptionError("В медиафайле не найдена доступная аудиодорожка")
    return np.asarray(waveform, dtype=np.float32)


def _refine_with_whisper(
    waveform: np.ndarray,
    duration: float,
    gigaam_segments: list[GigaamSegment],
    source_info: dict,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    custom_terms: tuple[str, ...] = (),
) -> list[TranscriptSegment]:
    _raise_if_cancelled(cancel_event)
    recognized_text = " ".join(segment.text for segment in gigaam_segments)
    glossary = _technical_glossary(
        source_info,
        text=recognized_text,
        custom_terms=custom_terms,
    )
    progress_callback("hybrid_selecting", None, "Ищем технические места для проверки", None)
    candidates = select_hybrid_candidates(gigaam_segments, glossary)
    result = [
        TranscriptSegment(start=segment.start, end=segment.end, text=segment.text)
        for segment in gigaam_segments
    ]
    if not candidates:
        progress_callback("hybrid_checking", 100, "Подозрительных технических мест не найдено", None)
        return result

    progress_callback("whisper_model", None, "Подготавливаем Whisper large-v3 для точечной проверки", None)
    model, device_label, batch_size = _load_model("large-v3")
    _raise_if_cancelled(cancel_event)

    try:
        from faster_whisper import BatchedInferencePipeline

        pipeline = BatchedInferencePipeline(model=model)
        prompt = build_whisper_prompt(glossary)
        hotwords = ", ".join(glossary)[:500] or None
        total_review_seconds = sum(
            max(0.0, gigaam_segments[item.segment_index].end - gigaam_segments[item.segment_index].start)
            for item in candidates
        )
        reviewed_seconds = 0.0
        started_at = time.monotonic()

        for candidate in candidates:
            _raise_if_cancelled(cancel_event)
            original = gigaam_segments[candidate.segment_index]
            clip_start = max(0.0, original.start - 0.2)
            clip_end = min(duration, original.end + 0.2)
            clip = waveform[int(clip_start * SAMPLE_RATE):int(clip_end * SAMPLE_RATE)]
            segment_iterator, _ = pipeline.transcribe(
                clip,
                language="ru",
                beam_size=WHISPER_BEAM_SIZE,
                best_of=WHISPER_BEAM_SIZE,
                batch_size=batch_size,
                vad_filter=False,
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=prompt,
                hotwords=hotwords,
            )
            revised_parts: list[str] = []
            logprobs: list[float] = []
            for segment in segment_iterator:
                _raise_if_cancelled(cancel_event)
                text = str(segment.text).strip()
                if text:
                    revised_parts.append(text)
                average_logprob = getattr(segment, "avg_logprob", None)
                if average_logprob is not None and np.isfinite(float(average_logprob)):
                    logprobs.append(float(average_logprob))

            revised = align_whisper_boundaries(
                original.text,
                " ".join(revised_parts).strip(),
            )
            whisper_average_logprob = sum(logprobs) / len(logprobs) if logprobs else None
            if should_accept_whisper_text(
                original.text,
                revised,
                glossary,
                candidate.reasons,
                whisper_average_logprob,
            ):
                result[candidate.segment_index] = TranscriptSegment(
                    start=original.start,
                    end=original.end,
                    text=revised,
                )

            reviewed_seconds += max(0.0, original.end - original.start)
            ratio = reviewed_seconds / total_review_seconds if total_review_seconds else 1.0
            elapsed = time.monotonic() - started_at
            eta = elapsed * (1 - ratio) / ratio if ratio >= 0.01 else None
            progress_callback(
                "hybrid_checking",
                min(100.0, ratio * 100),
                f"Проверяем технические места через Whisper на {device_label}",
                {
                    "processed_seconds": reviewed_seconds,
                    "total_seconds": total_review_seconds,
                    "eta_seconds": eta,
                },
            )
    except DownloadCancelled:
        raise
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(_friendly_transcription_error(exc)) from exc

    return result


def _download_audio(
    url: str,
    work_directory: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> tuple[Path, dict]:
    def progress_hook(data: dict) -> None:
        _raise_if_cancelled(cancel_event)
        if data.get("status") == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            speed = float(data.get("speed") or 0) or None
            eta = float(data.get("eta") or 0) or None
            progress = min(100, downloaded / total * 100) if total > 0 else None
            progress_callback(
                "downloading_audio",
                progress,
                "Скачиваем аудиодорожку для транскрибации",
                {
                    "downloaded_bytes": downloaded,
                    "total_bytes": total or None,
                    "speed_bytes_per_second": speed,
                    "eta_seconds": eta,
                },
            )
        elif data.get("status") == "finished":
            progress_callback("preparing_audio", None, "Подготавливаем аудио для распознавания", None)

    options = {
        **YTDLP_RUNTIME_OPTIONS,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(work_directory / "source.%(ext)s"),
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
    }
    selected_range = validate_time_range(start_seconds, end_seconds)
    if selected_range is not None:
        options["download_ranges"] = yt_dlp.utils.download_range_func(None, [selected_range])
        options["force_keyframes_at_cuts"] = True
    progress_callback("downloading_audio", 0, "Получаем аудиодорожку видео", None)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        if selected_range is not None:
            logger.warning(
                "Прямая загрузка фрагмента аудио не удалась, скачиваем дорожку целиком: %s",
                exc,
            )
            info = _download_full_audio_then_trim(
                url,
                work_directory,
                options,
                selected_range,
                progress_callback,
                cancel_event,
            )
        else:
            raise TranscriptionError("Не удалось скачать аудиодорожку. Проверьте доступность видео и соединение") from exc
    except Exception as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise TranscriptionError("Не удалось подготовить аудио для транскрибации") from exc

    if not isinstance(info, dict):
        raise TranscriptionError("Источник не вернул информацию об аудиодорожке")
    if selected_range is not None:
        info = dict(info)
        info["duration"] = selected_range[1] - selected_range[0]
    candidates = [
        path
        for path in work_directory.glob("source.*")
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        raise TranscriptionError("Аудиодорожка скачалась, но временный файл не найден")
    return max(candidates, key=lambda path: path.stat().st_mtime), info


def _download_full_audio_then_trim(
    url: str,
    work_directory: Path,
    base_options: dict,
    selected_range: tuple[float, float],
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> dict:
    """Надёжный fallback для источников, которые рвут поток при прямом диапазоне."""

    full_options = dict(base_options)
    full_options.pop("download_ranges", None)
    full_options.pop("force_keyframes_at_cuts", None)
    full_options["outtmpl"] = str(work_directory / "full-audio.%(ext)s")
    progress_callback(
        "downloading_audio",
        0,
        "Источник не отдал фрагмент напрямую, скачиваем аудио целиком",
        None,
    )
    try:
        with yt_dlp.YoutubeDL(full_options) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise TranscriptionError(
            "Не удалось скачать аудиодорожку. Проверьте доступность видео и соединение"
        ) from exc
    except Exception as exc:
        if cancel_event.is_set():
            raise DownloadCancelled("Операция отменена") from exc
        raise TranscriptionError("Не удалось подготовить аудио для транскрибации") from exc

    full_candidates = [
        path
        for path in work_directory.glob("full-audio.*")
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    if not full_candidates:
        raise TranscriptionError("Аудиодорожка скачалась, но временный файл не найден")
    full_path = max(full_candidates, key=lambda path: path.stat().st_mtime)
    output_path = work_directory / f"source{full_path.suffix.lower()}"
    _trim_local_audio(
        full_path,
        output_path,
        selected_range,
        progress_callback,
        cancel_event,
    )
    full_path.unlink(missing_ok=True)
    return info


def _trim_local_audio(
    source_path: Path,
    output_path: Path,
    selected_range: tuple[float, float],
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> None:
    ffmpeg = _find_ffmpeg_executable()
    if ffmpeg is None:
        raise TranscriptionError("Не найден FFmpeg. Установите FFmpeg и перезапустите приложение")

    _raise_if_cancelled(cancel_event)
    progress_callback("preparing_audio", None, "Вырезаем выбранный фрагмент аудио", None)
    output_path.unlink(missing_ok=True)
    start, end = selected_range
    arguments = [
        str(ffmpeg),
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-i",
        str(source_path),
        "-t",
        str(end - start),
        "-vn",
        "-c:a",
        "copy",
        str(output_path),
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        while process.poll() is None:
            if cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise DownloadCancelled("Операция отменена")
            time.sleep(0.05)
        _, stderr = process.communicate()
    except DownloadCancelled:
        output_path.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        output_path.unlink(missing_ok=True)
        raise TranscriptionError("Не удалось вырезать выбранный фрагмент аудио") from exc

    if process.returncode != 0 or not output_path.is_file():
        output_path.unlink(missing_ok=True)
        logger.warning("FFmpeg не смог вырезать аудиофрагмент: %s", (stderr or "").strip())
        raise TranscriptionError("FFmpeg не смог подготовить выбранный фрагмент аудио")


def _find_ffmpeg_executable() -> Path | None:
    if FFMPEG_DIRECTORY:
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = FFMPEG_DIRECTORY / name
            if candidate.is_file():
                return candidate.resolve()
    executable = shutil.which("ffmpeg")
    return Path(executable).resolve() if executable else None


def _model_is_cached(model_name: str) -> bool:
    cache = MODELS_DIR / f"models--Systran--faster-whisper-{model_name}" / "snapshots"
    return any(path.is_file() for path in cache.glob("*/model.bin"))


@lru_cache(maxsize=1)
def _load_model(model_name: str):
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "Компонент faster-whisper не установлен. Запустите установку зависимостей из requirements.txt"
        ) from exc

    local_only = _model_is_cached(model_name)
    try:
        use_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        use_cuda = False
    if use_cuda:
        try:
            model = WhisperModel(
                model_name,
                device="cuda",
                compute_type="float16",
                num_workers=1,
                download_root=str(MODELS_DIR),
                local_files_only=local_only,
            )
            return model, "видеокарте", WHISPER_GPU_BATCH_SIZE
        except Exception as exc:
            logger.warning("CUDA недоступна для faster-whisper, используем CPU: %s", exc)

    try:
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=WHISPER_CPU_THREADS,
            num_workers=1,
            download_root=str(MODELS_DIR),
            local_files_only=local_only,
        )
    except Exception as exc:
        raise TranscriptionError(_friendly_model_error(exc, False)) from exc
    return model, f"процессоре ({WHISPER_CPU_THREADS} потоков)", WHISPER_CPU_BATCH_SIZE


def _save_outputs(
    output_directory: Path,
    metadata: TranscriptMetadata,
    segments: list[TranscriptSegment],
    blocks,
    formats: tuple[str, ...],
    include_timestamps: bool,
) -> list[Path]:
    title = sanitize_filename(f"{metadata.title} - транскрибация", fallback="транскрибация")
    stem = unique_output_stem(output_directory, title, tuple(f".{item}" for item in formats))
    builders = {
        "md": lambda: build_markdown(metadata, blocks, include_timestamps=include_timestamps),
        "txt": lambda: build_text(blocks, include_timestamps=include_timestamps),
        "srt": lambda: build_srt(segments),
        "vtt": lambda: build_vtt(segments),
    }
    result = []
    for extension in formats:
        path = stem.with_suffix(f".{extension}")
        path.write_text(builders[extension](), encoding="utf-8")
        result.append(path.resolve())
    return result


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise DownloadCancelled("Операция отменена")


def _friendly_model_error(exc: Exception, used_cuda: bool) -> str:
    message = str(exc).lower()
    if "out of memory" in message or "memory allocation" in message:
        return "Недостаточно памяти для выбранной модели Whisper. Выберите модель меньше"
    if used_cuda and ("cuda" in message or "cudnn" in message):
        return "Не удалось запустить Whisper на видеокарте. Обновите драйвер или выберите модель меньше"
    if "download" in message or "connection" in message or "huggingface" in message:
        return "Не удалось скачать модель Whisper. Проверьте соединение и попробуйте ещё раз"
    return "Не удалось загрузить выбранную модель Whisper"


def _friendly_transcription_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "out of memory" in message or "memory allocation" in message:
        return "Недостаточно оперативной или видеопамяти. Выберите модель Whisper меньше"
    if "invalid data" in message or "averror" in message:
        return "Whisper не смог прочитать аудиодорожку этого медиафайла"
    logger.debug("Необработанная ошибка faster-whisper: %s", exc)
    return "Не удалось распознать речь. Попробуйте другую модель или язык"
