import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.video import router as video_router
from app.api.tasks import router as tasks_router
from app.api.extension import router as extension_router
from app.api.control import router as control_router
from app.config import APP_HOST, APP_VERSION, LOGS_DIR, STATIC_DIR, ensure_directories
from app.services import app_control


NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def configure_logging() -> None:
    ensure_directories()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    yield


app = FastAPI(
    title="Локальный загрузчик и транскрибатор",
    version=APP_VERSION,
    lifespan=lifespan,
)
app.include_router(video_router)
app.include_router(tasks_router)
app.include_router(extension_router)
app.include_router(control_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_STORE_HEADERS)


@app.get("/styles.css", include_in_schema=False)
async def root_styles() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "styles.css",
        media_type="text/css",
        headers=NO_STORE_HEADERS,
    )


@app.get("/app.js", include_in_schema=False)
async def root_script() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "app.js",
        media_type="text/javascript",
        headers=NO_STORE_HEADERS,
    )


@app.get("/api/health")
async def health(request: Request, response: Response) -> dict[str, str]:
    response.headers["X-StreamDock-App"] = "1"
    response.headers["X-StreamDock-Version"] = APP_VERSION
    if request.client and request.client.host == APP_HOST:
        response.headers[app_control.CONTROL_TOKEN_RESPONSE_HEADER] = app_control.get_control_token()
    return {"status": "ok", "version": APP_VERSION}
