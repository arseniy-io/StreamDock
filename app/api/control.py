from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import APP_HOST
from app.models.schemas import ShutdownResponse
from app.services import app_control
from app.services.task_manager import task_manager


router = APIRouter(prefix="/api/control", tags=["control"])


@router.post("/shutdown", response_model=ShutdownResponse, status_code=202)
async def shutdown_application(
    request: Request,
    background_tasks: BackgroundTasks,
    control_token: str | None = Header(
        default=None,
        alias=app_control.CONTROL_TOKEN_HEADER,
    ),
) -> ShutdownResponse:
    client_host = request.client.host if request.client else None
    if client_host != APP_HOST:
        raise HTTPException(status_code=403, detail="Команда доступна только локальному помощнику")

    try:
        app_control.validate_control_token(control_token)
    except app_control.InvalidControlTokenError as exc:
        raise HTTPException(status_code=403, detail="Команда управления отклонена") from exc

    cancelled_tasks = task_manager.cancel_all()
    # Starlette запускает BackgroundTasks только после отправки тела ответа.
    background_tasks.add_task(app_control.signal_process_shutdown)
    return ShutdownResponse(cancelled_tasks=cancelled_tasks)
