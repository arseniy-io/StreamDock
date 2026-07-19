import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
APP_VERSION = "0.4.1"
STATIC_DIR = BASE_DIR / "app" / "static"
DOWNLOADS_DIR = BASE_DIR / "downloads"
MODELS_DIR = BASE_DIR / "models"
LOGS_DIR = BASE_DIR / "logs"

# Все модели Hugging Face сохраняются внутри проекта. Xet на части Windows-систем
# может надолго зависать на старте загрузки, обычный HTTP-загрузчик стабильнее.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

APP_HOST = "127.0.0.1"
APP_PORT = 8765

# Умеренно повышенная параллельность ускоряет DASH/HLS, но не занимает все
# соединения и ресурсы компьютера.
YTDLP_CONCURRENT_FRAGMENTS = 8

# На текущем Core Ultra 7 это даёт 12 потоков из 22 логических. На менее
# мощных компьютерах значение автоматически уменьшается и оставляет ресурсы
# Windows и браузеру.
LOGICAL_CPU_COUNT = os.cpu_count() or 4
WHISPER_CPU_THREADS = max(2, min(12, LOGICAL_CPU_COUNT - 4))
WHISPER_CPU_BATCH_SIZE = 4
WHISPER_GPU_BATCH_SIZE = 8
WHISPER_BEAM_SIZE = 1

# GigaAM работает через ONNX Runtime на процессоре. Профиль оставляет системе
# несколько потоков, но заметно ускоряет длинные русскоязычные записи.
GIGAAM_CPU_THREADS = WHISPER_CPU_THREADS
GIGAAM_BATCH_SIZE = 4
GIGAAM_MAX_SEGMENT_SECONDS = 22
GIGAAM_QUANTIZATION = "int8"

MAX_LOCAL_MEDIA_SIZE = 20 * 1024**3
SUPPORTED_LOCAL_MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma",
}


def ensure_directories() -> None:
    """Создаёт только папки, которыми управляет приложение."""
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
