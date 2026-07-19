import re
from pathlib import Path


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_filename(name: str, fallback: str = "video", max_length: int = 180) -> str:
    """Возвращает безопасное для Windows имя без пути и запрещённых символов."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    if not clean:
        clean = fallback

    stem = clean.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        clean = f"_{clean}"

    clean = clean[:max_length].rstrip(" .")
    return clean or fallback


def unique_output_stem(directory: Path, stem: str, extensions: tuple[str, ...] = (".mp4",)) -> Path:
    """Подбирает свободное имя, не перезаписывая существующие файлы."""
    safe_stem = sanitize_filename(stem)
    candidate = directory / safe_stem
    counter = 2
    while any(candidate.with_suffix(extension).exists() for extension in extensions):
        candidate = directory / f"{safe_stem} ({counter})"
        counter += 1
    return candidate
