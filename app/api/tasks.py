import logging
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

from app.config import DOWNLOADS_DIR, MAX_LOCAL_MEDIA_SIZE, SUPPORTED_LOCAL_MEDIA_EXTENSIONS
from app.models.schemas import (
    AudioDownloadRequest,
    QueueTaskRequest,
    TaskCreatedResponse,
    TaskFileInfo,
    TaskItemError,
    TaskStatusResponse,
    TranscriptContentResponse,
    TranscriptUpdateRequest,
    TranscriptionRequest,
    VideoDownloadRequest,
)
from app.services.downloader import validate_video_url
from app.services.desktop import (
    DesktopRevealError,
    DesktopRevealUnsupportedError,
    reveal_file,
)
from app.services.file_manager import sanitize_filename
from app.services.task_manager import TaskRecord, task_manager


router = APIRouter(prefix="/api/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024


def _safe_task_file(task: TaskRecord, file_index: int) -> Path:
    if file_index < 0 or file_index >= len(task.files):
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = task.files[file_index].resolve()
    downloads_root = DOWNLOADS_DIR.resolve()
    if not path.is_file() or not path.is_relative_to(downloads_root):
        raise HTTPException(status_code=404, detail="Файл недоступен")
    return path


def _markdown_task_file(task: TaskRecord) -> Path:
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="Редактировать можно только готовую транскрибацию")
    downloads_root = DOWNLOADS_DIR.resolve()
    for raw_path in task.files:
        path = raw_path.resolve()
        if path.suffix.lower() == ".md" and path.is_file() and path.is_relative_to(downloads_root):
            return path
    raise HTTPException(status_code=404, detail="Markdown-транскрибация не найдена")


def _read_transcript(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise HTTPException(status_code=413, detail="Транскрибация слишком большая для редактора")
        return path.read_text(encoding="utf-8")
    except HTTPException:
        raise
    except (OSError, UnicodeError) as exc:
        logger.exception("Не удалось прочитать транскрибацию %s", path)
        raise HTTPException(status_code=500, detail="Не удалось открыть транскрибацию") from exc


def _parse_custom_terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in re.split(r"[,\r\n]+", value):
        term = " ".join(raw_term.strip().split())
        if not term:
            continue
        if len(term) > 64:
            raise HTTPException(status_code=422, detail="Один термин не может быть длиннее 64 символов")
        if any(ord(character) < 32 for character in term):
            raise HTTPException(status_code=422, detail="Термины содержат недопустимые символы")
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) > 64:
            raise HTTPException(status_code=422, detail="Можно добавить не больше 64 терминов")
    return tuple(terms)


def _task_response(task: TaskRecord) -> TaskStatusResponse:
    files = [
        TaskFileInfo(
            name=path.name,
            size=path.stat().st_size,
            download_url=f"/api/tasks/{task.task_id}/files/{index}",
        )
        for index, path in enumerate(task.files)
        if path.is_file()
    ]
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status,
        stage=task.stage,
        progress=task.progress,
        message=task.message,
        error=task.error,
        processed_seconds=task.processed_seconds,
        total_seconds=task.total_seconds,
        eta_seconds=task.eta_seconds,
        downloaded_bytes=task.downloaded_bytes,
        total_bytes=task.total_bytes,
        speed_bytes_per_second=task.speed_bytes_per_second,
        files=files,
        item_errors=[TaskItemError(**item) for item in task.item_errors],
    )


@router.post("/video", response_model=TaskCreatedResponse, status_code=202)
async def create_video_download(payload: VideoDownloadRequest) -> TaskCreatedResponse:
    try:
        url = validate_video_url(str(payload.url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.start_seconds is None:
        task = task_manager.create_video_download(url, payload.height)
    else:
        task = task_manager.create_video_download(
            url,
            payload.height,
            start_seconds=payload.start_seconds,
            end_seconds=payload.end_seconds,
        )
    return TaskCreatedResponse(task_id=task.task_id)


@router.post("/audio", response_model=TaskCreatedResponse, status_code=202)
async def create_audio_download(payload: AudioDownloadRequest) -> TaskCreatedResponse:
    try:
        url = validate_video_url(str(payload.url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    options = {
        "output_format": payload.format,
        "bitrate_kbps": payload.bitrate_kbps,
    }
    if payload.start_seconds is not None:
        options.update(
            start_seconds=payload.start_seconds,
            end_seconds=payload.end_seconds,
        )
    task = task_manager.create_audio_download(url, **options)
    return TaskCreatedResponse(task_id=task.task_id)


@router.post("/transcription", response_model=TaskCreatedResponse, status_code=202)
async def create_transcription(payload: TranscriptionRequest) -> TaskCreatedResponse:
    try:
        url = validate_video_url(str(payload.url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.engine in {"gigaam", "hybrid"} and payload.language == "en":
        raise HTTPException(
            status_code=422,
            detail="GigaAM предназначена для русской речи. Для английского выберите Whisper",
        )
    formats = tuple(dict.fromkeys(payload.formats))
    options = {
        "engine": payload.engine,
        "model_name": payload.model,
        "language": payload.language,
        "formats": formats,
        "include_timestamps": payload.include_timestamps,
        "paragraphize": payload.paragraphize,
        "remove_short_fragments": payload.remove_short_fragments,
        "text_source": payload.text_source,
        "custom_terms": tuple(payload.custom_terms),
        "diarize_speakers": payload.diarize_speakers,
        "speaker_count": payload.speaker_count,
    }
    if payload.start_seconds is not None:
        options.update(
            start_seconds=payload.start_seconds,
            end_seconds=payload.end_seconds,
        )
    task = task_manager.create_transcription(url, **options)
    return TaskCreatedResponse(task_id=task.task_id)


@router.post("/transcription/file", response_model=TaskCreatedResponse, status_code=202)
async def create_file_transcription(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    engine: Literal["whisper", "gigaam", "hybrid"] = "hybrid",
    model: Literal["tiny", "base", "small", "medium", "large-v3"] = "large-v3",
    language: Literal["auto", "ru", "en"] = "auto",
    formats: str = "md",
    include_timestamps: bool = True,
    paragraphize: bool = True,
    remove_short_fragments: bool = True,
    custom_terms: str = Query(default="", max_length=10_000),
    diarize_speakers: bool = False,
    speaker_count: int | None = Query(default=None, ge=2, le=10),
) -> TaskCreatedResponse:
    if engine in {"gigaam", "hybrid"} and language == "en":
        raise HTTPException(
            status_code=422,
            detail="GigaAM предназначена для русской речи. Для английского выберите Whisper",
        )
    original_name = Path(filename.replace("\\", "/")).name
    safe_name = sanitize_filename(original_name, fallback="media")
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_LOCAL_MEDIA_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_LOCAL_MEDIA_EXTENSIONS))
        raise HTTPException(status_code=422, detail=f"Неподдерживаемый формат файла. Доступны: {supported}")

    selected_formats = tuple(dict.fromkeys(item.strip().lower() for item in formats.split(",") if item.strip()))
    if not selected_formats or any(item not in {"md", "txt", "srt", "vtt"} for item in selected_formats):
        raise HTTPException(status_code=422, detail="Выберите хотя бы один допустимый формат транскрибации")
    selected_terms = _parse_custom_terms(custom_terms)

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_LOCAL_MEDIA_SIZE:
        raise HTTPException(status_code=413, detail="Файл слишком большой. Максимальный размер - 20 ГБ")
    free_space = shutil.disk_usage(tempfile.gettempdir()).free
    if content_length and content_length.isdigit() and free_space < int(content_length) + 512 * 1024**2:
        raise HTTPException(status_code=507, detail="Недостаточно свободного места для временной копии файла")

    upload_directory = Path(tempfile.mkdtemp(prefix="local-media-upload-"))
    upload_path = upload_directory / f"source{suffix}"
    written = 0
    try:
        with upload_path.open("wb") as destination:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_LOCAL_MEDIA_SIZE:
                    raise HTTPException(status_code=413, detail="Файл слишком большой. Максимальный размер - 20 ГБ")
                destination.write(chunk)
        if written == 0:
            raise HTTPException(status_code=422, detail="Выбранный файл пуст")
    except ClientDisconnect as exc:
        shutil.rmtree(upload_directory, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Передача файла была прервана") from exc
    except HTTPException:
        shutil.rmtree(upload_directory, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(upload_directory, ignore_errors=True)
        logger.exception("Не удалось принять локальный медиафайл")
        raise HTTPException(status_code=500, detail="Не удалось подготовить локальный файл") from exc

    task = task_manager.create_file_transcription(
        upload_path,
        safe_name,
        engine=engine,
        model_name=model,
        language=language,
        formats=selected_formats,
        include_timestamps=include_timestamps,
        paragraphize=paragraphize,
        remove_short_fragments=remove_short_fragments,
        custom_terms=selected_terms,
        diarize_speakers=diarize_speakers,
        speaker_count=speaker_count,
    )
    return TaskCreatedResponse(task_id=task.task_id)


@router.post("/queue", response_model=TaskCreatedResponse, status_code=202)
async def create_queue_task(payload: QueueTaskRequest) -> TaskCreatedResponse:
    items: list[dict[str, str | None]] = []
    for item in payload.items:
        try:
            url = validate_video_url(str(item.url))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        items.append({"url": url, "title": item.title})

    if payload.engine in {"gigaam", "hybrid"} and payload.language == "en":
        raise HTTPException(
            status_code=422,
            detail="GigaAM предназначена для русской речи. Для английского выберите Whisper",
        )

    task = task_manager.create_queue_task(
        tuple(items),
        action=payload.action,
        height=payload.height,
        output_format=payload.audio_format,
        bitrate_kbps=payload.bitrate_kbps,
        engine=payload.engine,
        model_name=payload.model,
        language=payload.language,
        formats=tuple(dict.fromkeys(payload.formats)),
        include_timestamps=payload.include_timestamps,
        paragraphize=payload.paragraphize,
        remove_short_fragments=payload.remove_short_fragments,
        text_source=payload.text_source,
        custom_terms=tuple(payload.custom_terms),
        diarize_speakers=payload.diarize_speakers,
        speaker_count=payload.speaker_count,
    )
    return TaskCreatedResponse(task_id=task.task_id)


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str) -> TaskStatusResponse:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return _task_response(task)


@router.get("/{task_id}/transcript", response_model=TranscriptContentResponse)
async def get_task_transcript(task_id: str) -> TranscriptContentResponse:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    path = _markdown_task_file(task)
    return TranscriptContentResponse(filename=path.name, content=_read_transcript(path))


@router.patch("/{task_id}/transcript", response_model=TranscriptContentResponse)
async def update_task_transcript(
    task_id: str,
    payload: TranscriptUpdateRequest,
) -> TranscriptContentResponse:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    path = _markdown_task_file(task)
    encoded = payload.content.encode("utf-8")
    if len(encoded) > MAX_TRANSCRIPT_BYTES:
        raise HTTPException(status_code=413, detail="Транскрибация слишком большая для редактора")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload.content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    except OSError as exc:
        logger.exception("Не удалось сохранить транскрибацию задачи %s", task_id)
        raise HTTPException(status_code=500, detail="Не удалось сохранить изменения") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return TranscriptContentResponse(filename=path.name, content=payload.content)


@router.post("/{task_id}/cancel", response_model=TaskStatusResponse)
async def cancel_task(task_id: str) -> TaskStatusResponse:
    task = task_manager.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return _task_response(task)


@router.delete("/{task_id}", status_code=204)
async def delete_terminal_task(task_id: str) -> Response:
    try:
        task_manager.remove_terminal(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Сначала дождитесь завершения отмены") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Не удалось удалить данные загрузки") from exc
    return Response(status_code=204)


@router.get("/{task_id}/files/{file_index}")
async def download_task_file(task_id: str, file_index: int) -> FileResponse:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    path = _safe_task_file(task, file_index)
    media_type = {
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".srt": "application/x-subrip; charset=utf-8",
        ".vtt": "text/vtt; charset=utf-8",
    }.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, filename=path.name, media_type=media_type)


@router.post("/{task_id}/open-folder")
async def open_task_folder(task_id: str) -> dict[str, str]:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    path = _safe_task_file(task, 0)
    try:
        status = reveal_file(path)
    except DesktopRevealUnsupportedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except DesktopRevealError as exc:
        logger.exception("Не удалось открыть Проводник для файла задачи %s", task_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": status}
