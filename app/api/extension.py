from fastapi import APIRouter, Header, HTTPException

from app.models.schemas import ExtensionStreamDownloadRequest, TaskCreatedResponse
from app.services.extension_downloader import sanitize_extension_headers, validate_public_media_url
from app.services.task_manager import task_manager


router = APIRouter(prefix="/api/extension", tags=["extension"])


@router.post("/download", response_model=TaskCreatedResponse, status_code=202)
def create_extension_download(
    payload: ExtensionStreamDownloadRequest,
    extension_marker: str | None = Header(default=None, alias="X-StreamDock-Extension"),
    legacy_extension_marker: str | None = Header(default=None, alias="X-Save-Video-Extension"),
) -> TaskCreatedResponse:
    # Пользовательская веб-страница не может отправить этот JSON-запрос из-за CORS,
    # а маркер дополнительно отделяет локальное расширение от обычного frontend.
    if extension_marker != "1" and legacy_extension_marker != "1":
        raise HTTPException(status_code=403, detail="Запрос доступен только локальному расширению")

    try:
        stream_url = validate_public_media_url(str(payload.stream_url))
        page_url = validate_public_media_url(str(payload.page_url)) if payload.page_url else None
        headers = sanitize_extension_headers(payload.request_headers, page_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task = task_manager.create_extension_download(
        stream_url,
        title=payload.title,
        stream_kind=payload.stream_kind,
        request_headers=headers,
        client_request_id=payload.client_request_id,
    )
    return TaskCreatedResponse(task_id=task.task_id)
