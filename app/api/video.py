import logging

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.models.schemas import (
    QueueAnalysisRequest,
    QueueAnalysisResponse,
    QueueMediaItem,
    VideoInfoRequest,
    VideoInfoResponse,
)
from app.services.downloader import VideoAnalysisError, analyze_video, validate_video_url
from app.services.playlist_analyzer import QueueAnalysisError, analyze_queue


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/video", tags=["video"])


@router.post("/info", response_model=VideoInfoResponse)
async def get_video_info(payload: VideoInfoRequest) -> VideoInfoResponse:
    try:
        url = validate_video_url(str(payload.url))
        return await run_in_threadpool(analyze_video, url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VideoAnalysisError as exc:
        logger.exception("Ошибка анализа видео: %s", payload.url)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/queue", response_model=QueueAnalysisResponse)
async def get_queue_info(payload: QueueAnalysisRequest) -> QueueAnalysisResponse:
    lines = [line.strip() for line in payload.source.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=422, detail="Добавьте хотя бы одну ссылку")

    source: str | list[str] = lines[0] if len(lines) == 1 else lines
    try:
        analysis = await run_in_threadpool(analyze_queue, source)
    except QueueAnalysisError as exc:
        logger.info("Не удалось проанализировать очередь: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Непредвиденная ошибка анализа очереди")
        raise HTTPException(
            status_code=422,
            detail="Не удалось проверить ссылки. Проверьте адреса и подключение к интернету",
        ) from exc

    return QueueAnalysisResponse(
        source_title=analysis.source_title,
        truncated=analysis.truncated,
        items=[
            QueueMediaItem(
                index=item.index,
                url=item.url,
                title=item.title,
                duration_seconds=item.duration_seconds,
                duration_text=item.duration_text,
            )
            for item in analysis.items
        ],
    )
