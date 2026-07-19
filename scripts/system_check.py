from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE_MINIMUM = (22, 0, 0)
RUNTIME_DISTRIBUTIONS = (
    "fastapi",
    "uvicorn",
    "yt-dlp",
    "yt-dlp-ejs",
    "faster-whisper",
    "onnx-asr",
    "onnxruntime",
    "sherpa-onnx",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    hint: str | None = None
    fatal: bool = False


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _run_version(executable: Path | str, argument: str) -> str | None:
    try:
        completed = subprocess.run(
            [str(executable), argument],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0] if completed.returncode == 0 and output else None


def find_ffmpeg() -> Path | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return Path(executable).resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    packages_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    candidates = sorted(
        packages_root.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0].resolve() if candidates else None


def check_python() -> CheckResult:
    version = sys.version_info[:2]
    bits = struct.calcsize("P") * 8
    ok = version >= (3, 11) and bits == 64
    return CheckResult(
        "Python",
        ok,
        f"{version[0]}.{version[1]}, {bits}-бит",
        None if ok else "Установите 64-битный Python 3.11 или новее.",
        fatal=not ok,
    )


def check_dependencies() -> CheckResult:
    missing = []
    for distribution in RUNTIME_DISTRIBUTIONS:
        try:
            importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            missing.append(distribution)
    ok = not missing
    return CheckResult(
        "Python-компоненты",
        ok,
        "установлены" if ok else f"не найдены: {', '.join(missing)}",
        None if ok else "Запустите install.bat или update.bat.",
        fatal=not ok,
    )


def check_ffmpeg() -> CheckResult:
    ffmpeg = find_ffmpeg()
    ffprobe = ffmpeg.with_name("ffprobe.exe") if ffmpeg and os.name == "nt" else None
    if ffmpeg and (ffprobe is None or ffprobe.is_file() or shutil.which("ffprobe")):
        version = _run_version(ffmpeg, "-version") or "версия не определена"
        return CheckResult("FFmpeg", True, f"{ffmpeg} ({version})")
    return CheckResult(
        "FFmpeg",
        False,
        "не найден FFmpeg вместе с FFprobe",
        "Установите командой: winget install --id Gyan.FFmpeg -e. Затем перезапустите StreamDock.",
    )


def check_node() -> CheckResult:
    executable = shutil.which("node")
    version_text = _run_version(executable, "--version") if executable else None
    version = parse_version(version_text or "")
    ok = bool(executable and version and version >= NODE_MINIMUM)
    return CheckResult(
        "Node.js",
        ok,
        version_text or "не найден",
        None if ok else "Для стабильного YouTube установите Node.js 22+: winget install --id OpenJS.NodeJS.LTS -e",
    )


def check_directories() -> CheckResult:
    missing_or_read_only = []
    for name in ("downloads", "models", "logs"):
        path = PROJECT_ROOT / name
        if not path.is_dir() or not os.access(path, os.W_OK):
            missing_or_read_only.append(name)
    ok = not missing_or_read_only
    return CheckResult(
        "Рабочие папки",
        ok,
        "доступны для записи" if ok else f"нет доступа: {', '.join(missing_or_read_only)}",
        None if ok else "Переместите проект в обычную папку текущего пользователя и повторите установку.",
        fatal=not ok,
    )


def collect_checks() -> list[CheckResult]:
    return [check_python(), check_dependencies(), check_ffmpeg(), check_node(), check_directories()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверяет готовность Windows к запуску StreamDock.")
    parser.add_argument("--json", action="store_true", help="Вывести результат в JSON.")
    parser.add_argument("--strict", action="store_true", help="Считать отсутствие FFmpeg или Node.js ошибкой.")
    args = parser.parse_args(argv)
    checks = collect_checks()

    if args.json:
        print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    else:
        print("Проверка системы:")
        for item in checks:
            marker = "OK" if item.ok else "ВНИМАНИЕ"
            print(f"  [{marker}] {item.name}: {item.message}")
            if item.hint:
                print(f"          {item.hint}")

    fatal_failure = any(not item.ok and item.fatal for item in checks)
    strict_failure = args.strict and any(not item.ok for item in checks)
    return 1 if fatal_failure or strict_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
