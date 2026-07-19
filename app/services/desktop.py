from __future__ import annotations

import os
import subprocess
from pathlib import Path


IS_WINDOWS = os.name == "nt"


class DesktopRevealUnsupportedError(RuntimeError):
    """Выделение файла не поддерживается текущей операционной системой."""


class DesktopRevealError(RuntimeError):
    """Проводник Windows не удалось открыть безопасным способом."""


def reveal_file(path: Path) -> str:
    """Открывает папку и выделяет конкретный файл в Проводнике Windows."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DesktopRevealError("Сохранённый файл больше недоступен") from exc

    if not resolved.is_file():
        raise DesktopRevealError("Сохранённый файл больше недоступен")
    if not IS_WINDOWS:
        raise DesktopRevealUnsupportedError("Выделение файла поддерживается только в Windows")

    try:
        subprocess.Popen(
            ["explorer.exe", "/select,", str(resolved)],
            close_fds=True,
            shell=False,
        )
        return "selected"
    except OSError as exc:
        # Если конкретная команда выделения недоступна, всё равно открываем
        # пользователю правильную папку как безопасный запасной вариант.
        try:
            os.startfile(str(resolved.parent))
        except OSError as fallback_exc:
            raise DesktopRevealError("Не удалось открыть папку с файлом") from fallback_exc
        return "opened"
