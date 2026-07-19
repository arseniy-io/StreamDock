from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
from threading import Event, Lock
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import wave

import numpy as np

from app.config import MODELS_DIR
from app.services.downloader import DownloadCancelled, FFMPEG_DIRECTORY
from app.services.markdown_builder import TranscriptSegment


ProgressDetails = dict[str, float | int | str | None]
ProgressCallback = Callable[[str, float | None, str, ProgressDetails | None], None]

SAMPLE_RATE = 16_000
DIARIZATION_THREADS = max(1, min(4, (os.cpu_count() or 4) - 2))
AUTO_CLUSTER_THRESHOLD = 0.9
SPEAKER_MODELS_DIR = MODELS_DIR / "speaker-diarization"

SEGMENTATION_ARCHIVE_NAME = "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_DIRECTORY_NAME = "sherpa-onnx-pyannote-segmentation-3-0"
SEGMENTATION_MODEL_NAME = "model.int8.onnx"
EMBEDDING_MODEL_NAME = "nemo_en_titanet_small.onnx"

SEGMENTATION_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    f"speaker-segmentation-models/{SEGMENTATION_ARCHIVE_NAME}"
)
EMBEDDING_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    f"speaker-recongition-models/{EMBEDDING_MODEL_NAME}"
)

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_MAX_MODEL_DOWNLOAD_BYTES = 512 * 1024**2
_MAX_ARCHIVE_FILES = 100
_MAX_ARCHIVE_UNPACKED_BYTES = 256 * 1024**2
_MIN_MODEL_BYTES = 1024
_MODEL_LOCK = Lock()


class SpeakerDiarizationError(RuntimeError):
    """Понятная пользователю ошибка локального разделения по спикерам."""


@dataclass(frozen=True, slots=True)
class SpeakerModelPaths:
    segmentation: Path
    embedding: Path


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """Интервал речи одного спикера. Номер спикера хранится с нуля."""

    start: float
    end: float
    speaker: int

    @property
    def label(self) -> str:
        return f"Спикер {self.speaker + 1}"


@dataclass(frozen=True, slots=True)
class SpeakerAssignment:
    """Фрагмент транскрибации и спикер с наибольшим пересечением по времени."""

    segment: TranscriptSegment
    speaker: int | None

    @property
    def label(self) -> str | None:
        return None if self.speaker is None else f"Спикер {self.speaker + 1}"


def ensure_speaker_models(
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    models_directory: Path | None = None,
) -> SpeakerModelPaths:
    """Атомарно загружает и распаковывает официальные модели sherpa-onnx."""

    root = (models_directory or SPEAKER_MODELS_DIR).resolve()
    paths = _speaker_model_paths(root)
    _raise_if_cancelled(cancel_event)

    if _models_are_ready(paths):
        progress_callback("speaker_models", 100, "Модель разделения голосов готова", None)
        return paths

    with _MODEL_LOCK:
        if _models_are_ready(paths):
            progress_callback("speaker_models", 100, "Модель разделения голосов готова", None)
            return paths

        root.mkdir(parents=True, exist_ok=True)
        archive_path = root / SEGMENTATION_ARCHIVE_NAME

        if not _model_file_ready(paths.segmentation):
            progress_callback("speaker_models", 0, "Загружаем модель поиска голосов", None)
            _download_file_atomic(
                SEGMENTATION_MODEL_URL,
                archive_path,
                progress_callback,
                cancel_event,
                progress_start=0,
                progress_span=30,
                message="Загружаем модель поиска голосов",
                minimum_size=1,
            )
            _extract_segmentation_model_atomic(
                archive_path,
                root / SEGMENTATION_DIRECTORY_NAME,
                cancel_event,
            )
            progress_callback(
                "speaker_models", 35, "Модель поиска голосов подготовлена", None
            )

        if not _model_file_ready(paths.embedding):
            _download_file_atomic(
                EMBEDDING_MODEL_URL,
                paths.embedding,
                progress_callback,
                cancel_event,
                progress_start=35,
                progress_span=65,
                message="Загружаем модель различения спикеров",
                minimum_size=_MIN_MODEL_BYTES,
            )

        if not _models_are_ready(paths):
            raise SpeakerDiarizationError(
                "Не удалось подготовить локальную модель разделения голосов"
            )

    progress_callback("speaker_models", 100, "Модель разделения голосов готова", None)
    return paths


def diarize_speakers(
    media_path: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    speaker_count: int | None = None,
) -> list[SpeakerTurn]:
    """Локально определяет, кто и когда говорил в медиафайле."""

    source = Path(media_path).resolve()
    if not source.is_file():
        raise SpeakerDiarizationError("Исходный медиафайл для разделения голосов не найден")
    validate_speaker_count(speaker_count)

    _raise_if_cancelled(cancel_event)
    model_paths = ensure_speaker_models(progress_callback, cancel_event)

    try:
        with tempfile.TemporaryDirectory(prefix="speaker-diarization-") as temp_name:
            wav_path = Path(temp_name) / "audio-16khz-mono.wav"
            _prepare_diarization_wav(
                source, wav_path, progress_callback, cancel_event
            )
            samples = _read_pcm_wav(wav_path)
            _raise_if_cancelled(cancel_event)

            progress_callback(
                "speaker_diarization", 0, "Определяем границы речи и спикеров", None
            )
            diarizer = _create_diarizer(model_paths, speaker_count)

            def sherpa_progress(processed_chunks: int, total_chunks: int) -> int:
                if cancel_event.is_set():
                    return 1
                total = max(0, int(total_chunks))
                processed = max(0, int(processed_chunks))
                percent = min(99.0, processed / total * 100) if total else 0.0
                progress_callback(
                    "speaker_diarization",
                    percent,
                    "Разделяем речь по спикерам",
                    {"processed_chunks": processed, "total_chunks": total},
                )
                return 0

            result = diarizer.process(samples, callback=sherpa_progress)
            _raise_if_cancelled(cancel_event)
            raw_turns = result.sort_by_start_time()
    except DownloadCancelled:
        raise
    except SpeakerDiarizationError:
        raise
    except MemoryError as exc:
        raise SpeakerDiarizationError(
            "Недостаточно оперативной памяти для разделения голосов"
        ) from exc
    except Exception as exc:
        lowered = str(exc).lower()
        message = (
            "Недостаточно оперативной памяти для разделения голосов"
            if "memory" in lowered or "allocate" in lowered
            else "Не удалось разделить транскрибацию по спикерам"
        )
        raise SpeakerDiarizationError(message) from exc

    turns = _normalize_turns(raw_turns)
    if speaker_count is None:
        turns = _collapse_tiny_speaker_clusters(turns)
    message = (
        f"Найдено спикеров: {len({turn.speaker for turn in turns})}"
        if turns
        else "Голоса в записи не обнаружены"
    )
    progress_callback("speaker_diarization", 100, message, None)
    return turns


def validate_speaker_count(speaker_count: int | None) -> int | None:
    """Проверяет ручное количество спикеров, используемое API приложения."""

    if speaker_count is not None and (
        isinstance(speaker_count, bool) or not 2 <= speaker_count <= 10
    ):
        raise ValueError("Количество спикеров должно быть от 2 до 10")
    return speaker_count


def assign_speakers_to_segments(
    segments: Sequence[TranscriptSegment],
    turns: Sequence[SpeakerTurn],
) -> list[SpeakerAssignment]:
    """Назначает каждому текстовому фрагменту спикера с максимальным пересечением."""

    normalized_turns = [
        turn for turn in turns if turn.end > turn.start and turn.speaker >= 0
    ]
    assignments: list[SpeakerAssignment] = []

    for segment in segments:
        start = max(0.0, float(segment.start))
        end = max(start, float(segment.end))
        overlap_by_speaker: defaultdict[int, float] = defaultdict(float)
        first_overlap_by_speaker: dict[int, float] = {}

        for turn in normalized_turns:
            overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
            if overlap > 0:
                overlap_by_speaker[turn.speaker] += overlap
                first_overlap_by_speaker[turn.speaker] = min(
                    first_overlap_by_speaker.get(turn.speaker, float("inf")),
                    max(start, turn.start),
                )

        speaker = (
            min(
                overlap_by_speaker,
                key=lambda candidate: (
                    -overlap_by_speaker[candidate],
                    first_overlap_by_speaker[candidate],
                    candidate,
                ),
            )
            if overlap_by_speaker
            else None
        )
        if speaker is None and normalized_turns:
            nearest = min(
                normalized_turns,
                key=lambda turn: (
                    max(turn.start - end, start - turn.end, 0.0),
                    turn.start,
                    turn.speaker,
                ),
            )
            distance = max(nearest.start - end, start - nearest.end, 0.0)
            if distance <= 1.0:
                speaker = nearest.speaker
        assignments.append(SpeakerAssignment(segment=segment, speaker=speaker))

    return assignments


def _speaker_model_paths(root: Path) -> SpeakerModelPaths:
    return SpeakerModelPaths(
        segmentation=root / SEGMENTATION_DIRECTORY_NAME / SEGMENTATION_MODEL_NAME,
        embedding=root / EMBEDDING_MODEL_NAME,
    )


def _models_are_ready(paths: SpeakerModelPaths) -> bool:
    return _model_file_ready(paths.segmentation) and _model_file_ready(paths.embedding)


def _model_file_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= _MIN_MODEL_BYTES
    except OSError:
        return False


def _download_file_atomic(
    url: str,
    destination: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    progress_start: float,
    progress_span: float,
    message: str,
    minimum_size: int,
) -> None:
    if destination.is_file() and destination.stat().st_size >= minimum_size:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    request = Request(url, headers={"User-Agent": "local-video-transcriber/1.0"})

    try:
        _raise_if_cancelled(cancel_event)
        with urlopen(request, timeout=45) as response, temporary.open("wb") as output:
            raw_total = response.headers.get("Content-Length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else 0
            if total > _MAX_MODEL_DOWNLOAD_BYTES:
                raise SpeakerDiarizationError("Файл модели имеет недопустимый размер")

            downloaded = 0
            while True:
                _raise_if_cancelled(cancel_event)
                chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > _MAX_MODEL_DOWNLOAD_BYTES:
                    raise SpeakerDiarizationError("Файл модели имеет недопустимый размер")
                output.write(chunk)

                fraction = min(1.0, downloaded / total) if total else 0.0
                progress_callback(
                    "speaker_models",
                    progress_start + fraction * progress_span if total else progress_start,
                    message,
                    {
                        "downloaded_bytes": downloaded,
                        "total_bytes": total or None,
                    },
                )

            output.flush()
            os.fsync(output.fileno())

        if total and downloaded != total:
            raise SpeakerDiarizationError("Загруженный файл модели повреждён")
        if temporary.stat().st_size < minimum_size:
            raise SpeakerDiarizationError("Загруженный файл модели повреждён")
        os.replace(temporary, destination)
    except DownloadCancelled:
        raise
    except SpeakerDiarizationError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise SpeakerDiarizationError(
            "Не удалось скачать модель разделения голосов. Проверьте соединение и повторите попытку"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _extract_segmentation_model_atomic(
    archive_path: Path,
    destination: Path,
    cancel_event: Event,
) -> None:
    extraction_root = Path(
        tempfile.mkdtemp(prefix=".speaker-model-extract-", dir=destination.parent)
    )
    try:
        with tarfile.open(archive_path, mode="r:bz2") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_ARCHIVE_FILES:
                raise SpeakerDiarizationError("Архив модели содержит слишком много файлов")

            total_size = sum(max(0, int(member.size)) for member in members)
            if total_size > _MAX_ARCHIVE_UNPACKED_BYTES:
                raise SpeakerDiarizationError("Архив модели имеет недопустимый размер")

            for member in members:
                _raise_if_cancelled(cancel_event)
                _extract_safe_tar_member(archive, member, extraction_root)

        extracted_directory = extraction_root / SEGMENTATION_DIRECTORY_NAME
        extracted_model = extracted_directory / SEGMENTATION_MODEL_NAME
        if not _model_file_ready(extracted_model):
            raise SpeakerDiarizationError("В архиве не найдена модель разделения голосов")
        _replace_directory_atomic(extracted_directory, destination)
    except DownloadCancelled:
        raise
    except SpeakerDiarizationError:
        archive_path.unlink(missing_ok=True)
        raise
    except (tarfile.TarError, OSError) as exc:
        archive_path.unlink(missing_ok=True)
        raise SpeakerDiarizationError(
            "Архив модели повреждён. Повторите запуск, чтобы скачать его заново"
        ) from exc
    finally:
        shutil.rmtree(extraction_root, ignore_errors=True)


def _extract_safe_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination_root: Path,
) -> None:
    pure_name = PurePosixPath(member.name)
    if (
        pure_name.is_absolute()
        or not pure_name.parts
        or any(part in {"", ".", ".."} for part in pure_name.parts)
        or pure_name.parts[0].endswith(":")
    ):
        raise SpeakerDiarizationError("Архив модели содержит небезопасный путь")
    if member.issym() or member.islnk() or member.isdev():
        raise SpeakerDiarizationError("Архив модели содержит небезопасную ссылку")

    target = destination_root.joinpath(*pure_name.parts)
    resolved_root = destination_root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise SpeakerDiarizationError("Архив модели содержит небезопасный путь")

    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return
    if not member.isfile():
        raise SpeakerDiarizationError("Архив модели содержит неподдерживаемый объект")

    target.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        raise SpeakerDiarizationError("Не удалось прочитать файл из архива модели")
    with source, target.open("wb") as output:
        shutil.copyfileobj(source, output, length=_DOWNLOAD_CHUNK_SIZE)


def _replace_directory_atomic(source: Path, destination: Path) -> None:
    backup: Path | None = None
    if destination.exists():
        backup = destination.with_name(f".{destination.name}.{uuid4().hex}.old")
        os.replace(destination, backup)
    try:
        os.replace(source, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def _prepare_diarization_wav(
    source: Path,
    destination: Path,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> None:
    ffmpeg = _find_ffmpeg_executable()
    if ffmpeg is None:
        raise SpeakerDiarizationError(
            "Не найден FFmpeg. Установите FFmpeg и перезапустите приложение"
        )

    progress_callback(
        "speaker_audio", None, "Подготавливаем аудио для разделения голосов", None
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(ffmpeg),
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(destination),
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
        destination.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        destination.unlink(missing_ok=True)
        raise SpeakerDiarizationError(
            "Не удалось подготовить аудио для разделения голосов"
        ) from exc

    if process.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        technical_error = RuntimeError((stderr or "FFmpeg завершился с ошибкой").strip())
        raise SpeakerDiarizationError(
            "FFmpeg не смог подготовить аудио для разделения голосов"
        ) from technical_error


def _find_ffmpeg_executable() -> Path | None:
    if FFMPEG_DIRECTORY:
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = FFMPEG_DIRECTORY / name
            if candidate.is_file():
                return candidate.resolve()
    executable = shutil.which("ffmpeg")
    return Path(executable).resolve() if executable else None


def _read_pcm_wav(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frames = wav_file.readframes(wav_file.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise SpeakerDiarizationError("Не удалось прочитать подготовленное аудио") from exc

    if channels != 1 or sample_width != 2 or sample_rate != SAMPLE_RATE:
        raise SpeakerDiarizationError(
            "FFmpeg подготовил аудио в неподдерживаемом формате"
        )
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0:
        raise SpeakerDiarizationError("В медиафайле не найден звук")
    return samples


def _create_diarizer(paths: SpeakerModelPaths, speaker_count: int | None):
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise SpeakerDiarizationError(
            "Компонент разделения голосов не установлен. Запустите install.bat"
        ) from exc

    try:
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=_native_library_path(paths.segmentation)
                ),
                num_threads=DIARIZATION_THREADS,
                provider="cpu",
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=_native_library_path(paths.embedding),
                num_threads=DIARIZATION_THREADS,
                provider="cpu",
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=speaker_count if speaker_count is not None else -1,
                threshold=AUTO_CLUSTER_THRESHOLD,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("sherpa-onnx rejected speaker diarization config")
        return sherpa_onnx.OfflineSpeakerDiarization(config)
    except Exception as exc:
        raise SpeakerDiarizationError(
            "Не удалось загрузить локальную модель разделения голосов"
        ) from exc


def _native_library_path(path: Path) -> str:
    """Возвращает ASCII-путь для Windows-библиотек без поддержки Unicode."""

    value = str(path.resolve())
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        pass

    if os.name != "nt":
        return value

    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetShortPathNameW(value, buffer, len(buffer))
        if 0 < length < len(buffer) and buffer.value:
            buffer.value.encode("ascii")
            return buffer.value
    except (AttributeError, OSError, UnicodeEncodeError):
        pass
    return value


def _normalize_turns(raw_turns: Sequence[object]) -> list[SpeakerTurn]:
    raw_valid_turns: list[tuple[float, float, int]] = []
    for item in raw_turns:
        start = max(0.0, float(getattr(item, "start")))
        end = max(start, float(getattr(item, "end")))
        speaker = int(getattr(item, "speaker"))
        if end > start and speaker >= 0:
            raw_valid_turns.append((start, end, speaker))

    turns = [
        SpeakerTurn(start=start, end=end, speaker=speaker)
        for start, end, speaker in sorted(raw_valid_turns)
    ]
    return _renumber_turns(turns)


def _collapse_tiny_speaker_clusters(turns: Sequence[SpeakerTurn]) -> list[SpeakerTurn]:
    """Убирает короткие ложные кластеры автоматического режима."""

    if len({turn.speaker for turn in turns}) <= 2:
        return list(turns)

    durations: defaultdict[int, float] = defaultdict(float)
    for turn in turns:
        durations[turn.speaker] += max(0.0, turn.end - turn.start)
    total_speech = sum(durations.values())
    minimum_duration = min(3.0, max(1.5, total_speech * 0.03))
    stable_speakers = {
        speaker for speaker, duration in durations.items() if duration >= minimum_duration
    }
    if not stable_speakers or len(stable_speakers) == len(durations):
        return list(turns)

    stable_turns = [turn for turn in turns if turn.speaker in stable_speakers]
    collapsed: list[SpeakerTurn] = []
    for turn in turns:
        if turn.speaker in stable_speakers:
            collapsed.append(turn)
            continue
        nearest = min(
            stable_turns,
            key=lambda candidate: (
                max(candidate.start - turn.end, turn.start - candidate.end, 0.0),
                abs((candidate.start + candidate.end) - (turn.start + turn.end)),
                candidate.start,
            ),
        )
        collapsed.append(
            SpeakerTurn(start=turn.start, end=turn.end, speaker=nearest.speaker)
        )
    return _renumber_turns(collapsed)


def _renumber_turns(turns: Sequence[SpeakerTurn]) -> list[SpeakerTurn]:
    speaker_order: dict[int, int] = {}
    result: list[SpeakerTurn] = []
    for turn in sorted(turns, key=lambda item: (item.start, item.end, item.speaker)):
        if turn.speaker not in speaker_order:
            speaker_order[turn.speaker] = len(speaker_order)
        result.append(
            SpeakerTurn(
                start=turn.start,
                end=turn.end,
                speaker=speaker_order[turn.speaker],
            )
        )
    return result


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise DownloadCancelled("Операция отменена")
